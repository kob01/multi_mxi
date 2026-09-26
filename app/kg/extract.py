"""文档实体关系抽取: 一次 LLM 调用从一篇文档产出 entities / relations。

范式完全对齐 ``app.memory.extraction``: 进程级惰性 ``json_mode`` 小温度模型单例、
任何异常都退化为"空抽取"、绝不抛出 —— 抽取失败只是这篇文档不进图谱, 不影响入库
主流程与对话。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.assistant.prompts import DOC_KG_EXTRACTION_PROMPT
from app.config import get_settings
from app.llm import get_chat_model

logger = logging.getLogger(__name__)


@dataclass
class DocKG:
    """一篇文档的结构化实体关系抽取结果, 两个字段都可能为空列表。"""

    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.entities or self.relations)


_extraction_llm = None


def _llm():
    """惰性建抽取专用的 json_mode 零温模型实例(进程级单例)。"""
    global _extraction_llm
    if _extraction_llm is None:
        settings = get_settings()
        _extraction_llm = get_chat_model(settings.llm_model, temperature=0, json_mode=True)
    return _extraction_llm


def _parse(raw: str) -> DocKG:
    """解析 LLM 输出的 JSON; 结构不符合预期时退化为空抽取, 不抛出。

    relation 的 src/dst 必须落在本次抽取出的实体名集合内, 否则丢弃该条(与
    ``extraction._parse`` 同样的完整性校验), 防止图里出现悬空关系。
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return DocKG()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return DocKG()
    if not isinstance(data, dict):
        return DocKG()
    entities = [
        {"name": e["name"].strip(), "type": (e.get("type") or "entity").strip() or "entity"}
        for e in data.get("entities", [])
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip()
    ]
    # 同名同类实体去重(保持首次出现顺序)
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for e in entities:
        key = (e["name"], e["type"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    entity_names = {e["name"] for e in deduped}
    relations = [
        {
            "src": r["src"].strip(),
            "dst": r["dst"].strip(),
            "relation": (r.get("relation") or "related").strip() or "related",
        }
        for r in data.get("relations", [])
        if isinstance(r, dict)
        and isinstance(r.get("src"), str) and r.get("src").strip() in entity_names
        and isinstance(r.get("dst"), str) and r.get("dst").strip() in entity_names
        and isinstance(r.get("relation"), str)
    ]
    return DocKG(entities=deduped, relations=relations)


async def extract_doc_graph(title: str, tags: list[str], text: str) -> DocKG:
    """对一篇文档的解析文本做实体关系抽取; 无文本/失败一律退化为空抽取。"""
    text = (text or "").strip()
    if not text:
        return DocKG()
    settings = get_settings()
    excerpt = text[: settings.kg_extraction_max_chars]
    prompt = DOC_KG_EXTRACTION_PROMPT.format(
        title=title or "(无标题)",
        tags="、".join(tags) if tags else "(无)",
        excerpt=excerpt,
    )
    try:
        resp = await _llm().ainvoke(prompt)
        return _parse(str(resp.content))
    except Exception as exc:  # noqa: BLE001 - 抽取失败只是这篇不进图谱, 不抛出
        logger.warning("文档知识图谱抽取失败, 本篇跳过: %s", exc)
        return DocKG()
