"""个人记忆提取: 一次 LLM 调用同时产出全部记忆桶。

Profile / Preference / Habit / Episode / Knowledge 与 entities/relations 共用的
是这一份提取结果, 不做多次 LLM 调用 —— 每轮对话结束都触发一次提取本身就是额外
开销, 拆成六七次纯属浪费(桶的定义见 ``app/memory/taxonomy.py``)。

调用点(``AssistantOrchestrator._write_personal_memory``)负责决定这一轮要不要提取
(跳过闲聊 / 拒答轮次), 本模块只负责"给一段对话 -> 出一份结构化记忆", 并且
任何异常都退化为"空提取", 绝不阻断对话主流程。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.assistant.prompts import MEMORY_EXTRACTION_PROMPT
from app.config import get_settings
from app.llm import get_chat_model

logger = logging.getLogger(__name__)

# 内网统一东八区(与调度图的平台时钟同源): 模型需要"今天是几号"才能换算相对时间。
_CST = timezone(timedelta(hours=8))
# occurred_at 的合理区间: 晚于未来 1 天或早于三年前的值一律当作模型猜错。
_FUTURE_TOLERANCE = timedelta(days=1)
_PAST_TOLERANCE = timedelta(days=365 * 3)


def _parse_date(raw: object) -> datetime | None:
    """把 LLM 给的日期文本解析成带时区的 datetime; 解析不出/明显猜错就 None。

    先试 ISO 解析(保留时间部分), 再退回只有日期的写法(容错 "2026-09-26" 这种分隔符);
    naive 值统一按 UTC 处理 —— 容器默认 UTC, 但本地跑在 +8 机器上时
    ``astimezone`` 会把日期错归到前一天, 所以不能依赖它。

    超出合理区间的日期直接丢成 None: 错年份比缺失更有害 —— 情节召回的时间窗会
    把这条记忆整条排除在外(丢成 None 后调用方回退到记录写入时间)。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.strptime(text[:10].replace("/", "-"), "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed > now + _FUTURE_TOLERANCE or parsed < now - _PAST_TOLERANCE:
        return None
    return parsed


def _clean_str(raw: object, max_len: int = 500) -> str:
    text = " ".join(str(raw or "").split())
    return text[:max_len]


def _str_list(raw: object, max_len: int = 500) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [text for item in raw if (text := _clean_str(item, max_len))]


@dataclass
class EpisodeRecord:
    """一条情节记忆(何时发生了什么、结果如何), 时间锚点可缺省。"""

    title: str
    content: str
    occurred_at: datetime | None = None


@dataclass
class KnowledgeRecord:
    """一条可复用的个人知识要点。"""

    topic: str
    content: str


@dataclass
class MemoryExtraction:
    """一轮对话的结构化个人记忆提取结果, 各字段都可能为空列表。

    ``facts`` 是引入分桶前的遗留字段: 解析时并入 ``knowledge``, 老调用方仍可读。
    """

    profile: list[dict] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    habits: list[str] = field(default_factory=list)
    episodes: list[EpisodeRecord] = field(default_factory=list)
    knowledge: list[KnowledgeRecord] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.profile
            or self.preferences
            or self.habits
            or self.episodes
            or self.knowledge
            or self.entities
            or self.relations
        )


_extraction_llm = None


def _llm():
    """惰性建提取专用的 json_mode 小温度模型实例(进程级单例)。"""
    global _extraction_llm
    if _extraction_llm is None:
        settings = get_settings()
        _extraction_llm = get_chat_model(settings.llm_model, temperature=0, json_mode=True)
    return _extraction_llm


def _parse_profile(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = _clean_str(entry.get("key"), 40)
        value = _clean_str(entry.get("value"), 120)
        if key and value:
            items.append({"key": key, "value": value})
    return items


def _parse_episodes(raw: object) -> list[EpisodeRecord]:
    if not isinstance(raw, list):
        return []
    episodes: list[EpisodeRecord] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = _clean_str(entry.get("title"), 120)
        what = _clean_str(entry.get("what"))
        outcome = _clean_str(entry.get("outcome"))
        if not (what or outcome or title):
            continue
        content = what if not outcome else f"{what}(结果: {outcome})" if what else outcome
        episodes.append(
            EpisodeRecord(
                title=title or content[:20],
                content=content,
                occurred_at=_parse_date(entry.get("occurred_at")),
            )
        )
    return episodes


def _parse_knowledge(raw: object) -> list[KnowledgeRecord]:
    if not isinstance(raw, list):
        return []
    items: list[KnowledgeRecord] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content = _clean_str(entry.get("content"))
        if not content:
            continue
        items.append(KnowledgeRecord(topic=_clean_str(entry.get("topic"), 60), content=content))
    return items


def _parse(raw: str) -> MemoryExtraction:
    """解析 LLM 输出的 JSON; 结构不符合预期时退化为空提取, 不抛出。

    单个字段坏掉(类型不对/元素不是 dict)只让该字段为空, 不影响其它桶 —— 一份
    画像提取失败不该把同一轮的情节和实体关系一起丢掉。
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        return MemoryExtraction()
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
    # 遗留 facts 并入 knowledge: 老库里的 fact 记录仍在召回, 新提取也不该丢这部分信息。
    legacy_facts = _str_list(data.get("facts"))
    knowledge = _parse_knowledge(data.get("knowledge"))
    knowledge.extend(KnowledgeRecord(topic="", content=text) for text in legacy_facts)
    return MemoryExtraction(
        profile=_parse_profile(data.get("profile")),
        preferences=_str_list(data.get("preferences")),
        habits=_str_list(data.get("habits")),
        episodes=_parse_episodes(data.get("episodes")),
        knowledge=knowledge,
        entities=entities,
        relations=relations,
        facts=legacy_facts,
    )


async def extract_memories(message: str, answer: str) -> MemoryExtraction:
    """对一轮 ``(user, assistant)`` 对话做个人记忆提取。"""
    prompt = MEMORY_EXTRACTION_PROMPT.format(
        message=message,
        answer=answer,
        today=datetime.now(_CST).strftime("%Y-%m-%d"),
    )
    try:
        resp = await _llm().ainvoke(prompt)
        return _parse(str(resp.content))
    except Exception as exc:  # noqa: BLE001 - 提取失败只是这一轮不写长期记忆, 不影响对话
        logger.warning("长期记忆提取失败, 本轮跳过: %s", exc)
        return MemoryExtraction()
