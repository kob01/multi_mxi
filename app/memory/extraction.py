"""长期记忆提取: 一次 LLM 调用同时产出 facts / entities / relations。

Vector 通道(长期事实文本)与 Graph 通道(实体 + 关系三元组)共用这一份提取
结果, 不做两次 LLM 调用 —— 每轮对话结束都触发一次提取本身就是额外开销, 拆成
两次纯属浪费。

调用点(``AssistantOrchestrator.persist_memory``)负责决定这一轮要不要提取
(跳过闲聊 / 拒答轮次), 本模块只负责"给一段对话 -> 出一份结构化记忆", 并且
任何异常都退化为"空提取", 绝不阻断对话主流程。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.assistant.prompts import MEMORY_EXTRACTION_PROMPT
from app.config import get_settings
from app.llm import get_chat_model

logger = logging.getLogger(__name__)


@dataclass
class MemoryExtraction:
    """一轮对话的结构化长期记忆提取结果, 三个字段都可能为空列表。"""

    facts: list[str] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.facts or self.entities or self.relations)


_extraction_llm = None


def _llm():
    """惰性建提取专用的 json_mode 小温度模型实例(进程级单例)。"""
    global _extraction_llm
    if _extraction_llm is None:
        settings = get_settings()
        _extraction_llm = get_chat_model(settings.llm_model, temperature=0, json_mode=True)
    return _extraction_llm


def _parse(raw: str) -> MemoryExtraction:
    """解析 LLM 输出的 JSON; 结构不符合预期时退化为空提取, 不抛出。"""
    data = json.loads(raw)
    if not isinstance(data, dict):
        return MemoryExtraction()
    facts = [f for f in data.get("facts", []) if isinstance(f, str) and f.strip()]
    entities = [
        e for e in data.get("entities", [])
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip()
    ]
    entity_names = {e["name"] for e in entities}
    relations = [
        r for r in data.get("relations", [])
        if isinstance(r, dict)
        and r.get("src") in entity_names and r.get("dst") in entity_names
        and isinstance(r.get("relation"), str) and r["relation"].strip()
    ]
    return MemoryExtraction(facts=facts, entities=entities, relations=relations)


async def extract_memories(message: str, answer: str) -> MemoryExtraction:
    """对一轮 ``(user, assistant)`` 对话做长期记忆提取。"""
    prompt = MEMORY_EXTRACTION_PROMPT.format(message=message, answer=answer)
    try:
        resp = await _llm().ainvoke(prompt)
        return _parse(str(resp.content))
    except Exception as exc:  # noqa: BLE001 - 提取失败只是这一轮不写长期记忆, 不影响对话
        logger.warning("长期记忆提取失败, 本轮跳过: %s", exc)
        return MemoryExtraction()
