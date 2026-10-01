"""个人记忆提取: 一次 LLM 调用同时产出全部记忆桶。

画像与关系额外带**生效时间**(口径见 ``app/memory/temporal.py``): 用户讲过去的状态
("2015 年秋我 64kg")时, 时间必须落到 ``valid_at`` 而不是混进值里 —— 否则十年前的
旧值会按"最后听到"顶掉当前态。未标时间即本轮日期当下生效, 因此这里必须把"今天"
一并交给模型并自己算好兜底值。

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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.assistant.prompts import MEMORY_EXTRACTION_PROMPT
from app.config import get_settings
from app.llm import get_chat_model
from app.memory import graph_vocab
from app.memory.temporal import parse_embedded_date, parse_event_time, today_cst
from app.memory.taxonomy import is_conversation_product

logger = logging.getLogger(__name__)

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

    ``facts`` 是引入分桶前的遗留字段, 独立返回(老通道仍按 fact 落盘)。
    ``knowledge`` 不再由每轮提取产出: 知识桶只由显式"记一下"指令写入
    (personal.remember_knowledge 复用 ``KnowledgeRecord`` 这个类型)。
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


def _parse_profile(raw: object, *, today: datetime) -> list[dict]:
    """画像原子 -> ``[{key, value, valid_at, explicit}]``。

    时间是这一桶最容易丢的信息: 用户说"2015 年秋我 64kg", 只留 value 就会让十年前的
    旧值顶掉当前态, 所以话里的时间必须拆出来落到 ``valid_at``(现实轴), value 只留纯值。
    ``at`` 缺失或解析不出时按本轮日期兜底("现在说的即当下生效"), 并用 ``explicit``
    区分"用户明说的时间"与"系统兜底的时间" —— 只有前者会在画像摘要里渲染成生效日期。
    """
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # 键名也可能被写成"体重（2015年）": 剥掉时间修饰, 否则同一个属性会开成两个槽。
        key, key_stamp = parse_embedded_date(_clean_str(entry.get("key"), 40))
        value, value_stamp = parse_embedded_date(_clean_str(entry.get("value"), 120))
        if not (key and value):
            continue
        valid_at = parse_event_time(entry.get("at")) or value_stamp or key_stamp
        items.append(
            {
                "key": key,
                "value": value,
                "valid_at": valid_at or today,
                "explicit": valid_at is not None,
            }
        )
    return items


def _parse_relations(raw: object, entity_names: set[str], *, today: datetime) -> list[dict]:
    """关系三元组, 额外带 ``valid_at``(现实轴, 缺省为本轮日期)。

    沿用"src/dst 必须已在 entities 出现"的校验(图里不允许凭空节点); 时间用来让写入侧
    分辨"当前态变更"与"用户在讲过去的关系", 解析不出就按听到这句话的时间兜底。

    端点名与 entities 走同一个归一(_clean_str): 不先归一再比对, 带空白的写法就会被
    判成"端点不存在"而丢整条关系(或凭空补出一个另一写法的节点)。
    """
    items: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        src = _clean_str(entry.get("src"), 60)
        dst = _clean_str(entry.get("dst"), 60)
        if not src or not dst or src not in entity_names or dst not in entity_names:
            continue
        relation = _clean_str(entry.get("relation"), 40)
        if not relation:
            continue
        items.append(
            {**entry, "src": src, "dst": dst,
             "relation": relation, "valid_at": parse_event_time(entry.get("valid_at")) or today}
        )
    return items


# 情节只装用户现实生活中的经历。本轮对话的产物 —— "用户要求生成表格/导出报告/
# 搭看板页面""助手生成了 X 文件并提供下载""助手未找到相关文档" —— 不是一段人生
# 事件: 会话记录里本来就有, 写进情节桶只会挤掉真正有价值的经历。prompt 里已经
# 写了这条口径, 但小模型仍会把"助手做了某事"当成事件输出, 所以解析时再兜一层
# 确定性拦截。口径本身(特征词表)与图谱写入共用 taxonomy 里那一份。


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
        if is_conversation_product(title, what, outcome):
            logger.debug("情节桶跳过本轮对话产物: %s", title or what or outcome)
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


def _parse(raw: str, *, today: datetime | None = None) -> MemoryExtraction:
    """解析 LLM 输出的 JSON; 结构不符合预期时退化为空提取, 不抛出。

    单个字段坏掉(类型不对/元素不是 dict)只让该字段为空, 不影响其它桶 —— 一份
    画像提取失败不该把同一轮的情节和实体关系一起丢掉。

    ``today`` 是画像/关系的时间兜底基准(本轮对话日期): 调用方可注入以便离线测试,
    不传则取东八区今天。
    """
    stamp = today or today_cst()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return MemoryExtraction()
    # 实体名先归一再入库: 图侧唯一约束是 (user_id, name, type), 带尾空格的写法与干净
    # 写法是两个节点, 而且关系端点的比对会因写法差异错判。
    entities = []
    raw_entities = data.get("entities")
    for entry in raw_entities if isinstance(raw_entities, list) else []:
        if not isinstance(entry, dict):
            continue
        name = _clean_str(entry.get("name"), 60)
        if not name:
            continue
        entities.append({**entry, "name": name})
    entity_names = {e["name"] for e in entities}
    raw_relations = data.get("relations")
    # 关系端点没被单独列进 entities 是小模型常见写法(如 src="我" 只出现在关系里):
    # 补一个 other 型实体把它挂上图, 否则整条关系会被"端点必须已存在"的校验丢掉,
    # 个人图谱就永远只有节点没有边(双时态关系失效也就无从发生)。
    for entry in raw_relations if isinstance(raw_relations, list) else []:
        if not isinstance(entry, dict):
            continue
        for raw_name in (entry.get("src"), entry.get("dst")):
            name = _clean_str(raw_name, 60)
            if name and name not in entity_names:
                entity_names.add(name)
                entities.append({"name": name, "type": "other"})
    relations = _parse_relations(raw_relations, entity_names, today=stamp)
    # 遗留 facts 独立返回: 知识桶已改为只由显式"记一下"写入, 不再拿每轮
    # 提取兜底; 老通道(personal_memory_enabled=false)照旧按 fact 落盘。
    legacy_facts = _str_list(data.get("facts"))
    return MemoryExtraction(
        profile=_parse_profile(data.get("profile"), today=stamp),
        preferences=_str_list(data.get("preferences")),
        habits=_str_list(data.get("habits")),
        episodes=_parse_episodes(data.get("episodes")),
        entities=entities,
        relations=relations,
        facts=legacy_facts,
    )


def _format_existing(preferences: Sequence[str], habits: Sequence[str]) -> str:
    """把已存的偏好/习惯拼成提取 prompt 的"已记住"小节。

    提取器本身无状态, 不告诉它"已经记过什么"就会每轮重复抽同一件事的
    不同说法(语义查重阈值 0.92 卡不住改写的近义句)。这里只列文本, 让模型
    自己判断本轮是否已被覆盖; 两个桶都为空时给一个占位行, 避免 prompt 里出现
    空小节让模型误解为"没有已存信息"。
    """
    lines: list[str] = []
    lines.extend(f"- [偏好] {text}" for text in preferences if text.strip())
    lines.extend(f"- [习惯] {text}" for text in habits if text.strip())
    return "\n".join(lines) if lines else "(无)"


async def extract_memories(
    message: str,
    answer: str,
    *,
    existing_preferences: Sequence[str] = (),
    existing_habits: Sequence[str] = (),
) -> MemoryExtraction:
    """对一轮 ``(user, assistant)`` 对话做个人记忆提取。

    ``existing_preferences`` / ``existing_habits`` 是该用户已存的对应桶文本,
    供提取器判重(不传则退化为旧行为: 只看本轮, 容易重复写入)。
    """
    _today = today_cst()  # 东八区今天: 既是 prompt 里的"今天是几号", 也是未标时间属性的生效时间
    prompt = MEMORY_EXTRACTION_PROMPT.format(
        message=message,
        answer=answer,
        today=_today.strftime("%Y-%m-%d"),
        existing=_format_existing(existing_preferences, existing_habits),
        # 实体类型与关系词表由图侧口径单点渲染: 改词表只改 graph_vocab, 提示词跟着走。
        entity_types=graph_vocab.entity_type_hint(),
        relation_words=graph_vocab.relation_hint(),
    )
    try:
        resp = await _llm().ainvoke(prompt)
        return _parse(str(resp.content), today=_today)
    except Exception as exc:  # noqa: BLE001 - 提取失败只是这一轮不写长期记忆, 不影响对话
        logger.warning("长期记忆提取失败, 本轮跳过: %s", exc)
        return MemoryExtraction()
