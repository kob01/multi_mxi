"""用户画像 (User Memory 的 profile 桶): ``user_profiles`` 表, 一人一条。

画像与其余四个桶最大的差别是"同一个属性只留一个当前值": 用户换了部门, 画像里
应当是新部门顶掉旧部门, 而不是两条并存的事实。所以这里单独建表, 不进
``long_term_memories`` 做向量查重 —— 相似度查重救不了"部门: 研发部 / 部门: 财务部"
这种同键不同值。

但"顶掉"不等于"按听到的先后顺序抹掉": 体重/身高/部门/职位这类是**随时间变化的
观测序列**, 纯覆盖会让"2015 年秋我 64kg"这句历史陈述压掉当前的 70kg(就是这么一个
真实事故)。口径与业界一致 —— 健康数据建模(FHIR ``Observation`` 只追加观测、当前值
按 effective 时间派生)、时序知识图谱(Zep/Graphiti 的 ``valid_at``/``invalid_at`` 双时
态)、OpenAI 个性化记忆("冲突按日期取最新")给的都是同一条, 落到代码上就两点:

- 波动类属性按 ``(值, 生效时间, 记录时间)`` 存成观测序列(``attribute_history`` 列),
  当前值由**生效时间派生**: 在讲过去的陈述只进历史, 不改当前值;
- 每键观测数硬封顶(``profile_history_max_entries``), 画像仍是"一人一条",
  不会长成第二张记忆表。

合并与摘要渲染都是确定性规则, 不调 LLM: 提取侧只负责给出 ``{key, value, valid_at,
explicit}`` 原子(时间口径见 ``app/memory/temporal.py``), 怎么覆盖/累积由本模块按固定
语义决定。每轮对话把整份画像全量注入 prompt(体量由 ``profile_max_chars`` 硬性封顶),
因此不需要 embedding; 历史值不进 prompt, 只在"我的记忆"页可见。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import UserProfileRow
from app.db.session import get_session_factory
from app.memory.temporal import (
    TimedValue,
    as_datetime,
    format_month,
    history_rank,
    parse_embedded_date,
    utcnow,
)

logger = logging.getLogger(__name__)

# 波动类属性(观测序列): 同一键的新旧值按生效时间派生当前值, 被顶掉的进有界历史。
# 身体/组织属性都归这里 —— 它们是"当前态", 会随时间变, 但绝不能被一句历史陈述改写。
_MEASURE_KEYS = {
    "体重", "weight",
    "身高", "height",
    "BMI", "体脂率", "体脂", "body_fat",
    "年龄", "age",
    "所在地", "城市", "location", "city",
    "部门", "department", "dept",
    "职位", "岗位", "position", "title",
    "职级", "level", "grade",
    "汇报对象", "manager", "leader",
    "入职时间", "hire_date",
}

# 身份类属性(覆盖即修正): 这类值是"当时写错了现在改对", 留历史只会让画像越翻越脏。
_CORRECTION_KEYS = {
    "姓名", "name",
    "工号", "employee_id", "emp_id",
    "出生日期", "birthday", "birth_date", "date_of_birth",
    "学历", "education",
    "毕业院校", "school",
    "邮箱", "email",
    "电话", "phone",
}

# 同义键归一: LLM 一会儿写 "department" 一会儿写"部门", 不归一就会变成两个槽,
# 画像里同一个事实出现两次就没法"覆盖"了(只会越写越乱)。左边用 lowercase + 下划线
# 后的形式匹配, 中文键直接原字写。目标集与提取 prompt 里的固定 key 集合一致。
_KEY_ALIASES = {
    "name": "姓名",
    "employee_id": "工号",
    "emp_id": "工号",
    "登录账号": "工号",
    "账号": "工号",
    "department": "部门",
    "dept": "部门",
    "所在部门": "部门",
    "团队": "部门",
    "position": "职位",
    "title": "职位",
    "职务": "职位",
    "level": "职级",
    "grade": "职级",
    "manager": "汇报对象",
    "leader": "汇报对象",
    "直属上级": "汇报对象",
    "location": "所在地",
    "city": "所在地",
    "常驻城市": "所在地",
    "hire_date": "入职时间",
    "职责": "负责事务",
    "负责事项": "负责事务",
    "负责内容": "负责事务",
    "工作内容": "负责事务",
    "技能特长": "技能",
    "能力": "技能",
    "birthday": "出生日期",
    "birth_date": "出生日期",
    "date_of_birth": "出生日期",
    "生日": "出生日期",
    "age": "年龄",
    "height": "身高",
    "weight": "体重",
    "education": "学历",
    "教育背景": "学历",
    "school": "毕业院校",
    "毕业学校": "毕业院校",
    "母校": "毕业院校",
}


# 空骨架: 读路径无行/读失败时返回它, 调用方不必判 None。
_EMPTY_ATTRIBUTES: dict[str, list[str]] = {}
_EMPTY_HISTORY: dict[str, list[dict]] = {}


def _empty_profile() -> dict[str, Any]:
    return {
        "attributes": dict(_EMPTY_ATTRIBUTES),
        "history": dict(_EMPTY_HISTORY),
        "summary": "",
        "updated_at": None,
    }


def _normalize_key(key: str) -> str:
    """键归一: 去首尾空白 + 已知英文别名映射为中文槽名(未知键保留原样)。"""
    raw = (key or "").strip()
    probe = raw.lower().replace(" ", "_")
    return _KEY_ALIASES.get(probe, raw)


def _in_pool(key: str, pool: Iterable[str]) -> bool:
    """容错比对: 下划线/大小写抹平后与集内任一项同名即算同一槽。"""
    probe = key.lower().replace("_", "")
    return any(probe == s.lower().replace("_", "") for s in pool)


def is_measurement(key: str) -> bool:
    """该键是不是"随时间变的观测"(当前值按生效时间派生, 被顶掉的进历史)。"""
    return _in_pool(key, _MEASURE_KEYS)


def is_correction(key: str) -> bool:
    """该键是不是"身份类修正"(新值直接顶掉旧值, 不保留历史)。"""
    return _in_pool(key, _CORRECTION_KEYS)


def _clean(value: Any, max_len: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_len]


def _as_list(value: Any) -> list[Any]:
    """把"单值或列表"抹平成一个列表(旧行可能存的是裸字符串, 不是列表)。"""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _observation(raw: Any, *, fallback_time: datetime | None) -> TimedValue | None:
    """画像 JSON 里的一行 -> 一条观测; 既吃新格式(dict), 也吃老格式(纯字符串)。

    老库里的值常常是 ``64kg（2015年秋）`` 这种把时间写进 value 的形式, 这里顺手把时间
    挑回生效时间轴、并把记录时间定为 ``fallback_time``(画像行的更新时间) —— 升级后
    不必清库, 用户重新说一句"现在 70kg"就能把当前态修正回来。
    """
    base = (
        TimedValue.from_dict(raw, fallback_time=fallback_time)
        if isinstance(raw, dict)
        else TimedValue(value=_clean(raw), recorded_at=fallback_time)
    )
    if base is None or not base.value:
        return None
    value, stamp = parse_embedded_date(base.value)
    if not value:
        return None
    valid_at = base.valid_at or stamp
    return TimedValue(
        value=value,
        valid_at=valid_at,
        recorded_at=base.recorded_at,
        explicit=base.explicit or (stamp is not None and valid_at is stamp),
    )


def _ordered(
    observations: Iterable[TimedValue], *, now: datetime, grace_days: int
) -> list[TimedValue]:
    """观测按"当前态在前"排序: 历史组沉底, 组内时间新的在前, 完全同时按听说顺序后者在前。"""
    items = list(observations)
    ranked = sorted(
        enumerate(items),
        key=lambda pair: (history_rank(pair[1], now=now, grace_days=grace_days), pair[0]),
        reverse=True,
    )
    return [item for _, item in ranked]


def merge_attributes(
    old: dict[str, Any] | None,
    items: list[dict],
    *,
    history: dict[str, Any] | None = None,
    now: datetime | None = None,
    stored_at: datetime | None = None,
    max_entries: int | None = None,
    grace_days: int | None = None,
) -> tuple[dict[str, list[str]], dict[str, list[dict]], int, int, int]:
    """把提取到的 ``[{key, value, valid_at, explicit}]`` 合并进既有画像(纯函数, 便于单测)。

    返回 ``(当前值画像, 观测序列, 新增槽位数, 更新次数, 被拦下的历史陈述数)``。规则:

    - 英文同义键先归一(如 department -> 部门), 避免同一事实占两个槽;
    - 波动类键(体重/身高/部门/职位/职级/汇报对象/所在地/年龄/入职时间): 每个值是
      一条观测, 当前值按**生效时间**派生 —— 在讲过去的陈述(明说了时间且已在
      ``grace_days`` 之前)只进历史、不改当前值(用户先说"现在 70kg", 后又说"2015 年秋
      64kg", 画像仍得是 70kg); 没标时间的按本轮日期当下生效, 同一槽位上后听到的赢;
      "从上个月起…"这类近期起始日期归入"当下生效"一类(它描述的是持续到现在的变更),
      不会因为起始日比另一句陈述早就被当成历史;
    - 身份修正类键(姓名/工号/出生日期/学历/毕业院校/邮箱/电话): 值变了就整体替换, 不留历史;
    - 其余键(多值槽): 并入集合去重, 只增不删 —— 画像不做"猜测式遗忘",
      删旧值留给用户在前端自服务;
    - 空 key / 空 value 一律忽略, 不用无意义值污染画像。

    ``history`` 是行上已存的观测序列(老库首次升级时为空, 此时由 ``old`` 的字符串自愈);
    ``now`` 是本轮听到新陈述的时间, ``stored_at`` 是老画像上次更新的时间(已存值没有任
    何时间线索时按它当记录时间, 不至于把陈年旧值当成"刚刚说的"); ``max_entries``
    与这两个时间都只在测试里需要注入, 业务调用不传。
    """
    now = now or utcnow()
    stored_at = stored_at or now
    max_entries = max_entries or get_settings().profile_history_max_entries
    grace_days = grace_days or get_settings().profile_current_grace_days
    stored = history if isinstance(history, dict) else {}
    observations: dict[str, list[TimedValue]] = {}
    plain: dict[str, list[str]] = {}
    order: list[str] = []
    added = updated = superseded = 0

    def _touch(key: str) -> None:
        if key not in order:
            order.append(key)

    def _absorb(key: str, raw: Any) -> None:
        item = _observation(raw, fallback_time=stored_at)
        if item is None:
            return
        bucket = observations.setdefault(key, [])
        if any(_same_observation(o, item) for o in bucket):
            return  # 同一生效时间的同一值又说了一遍: 不产生新观测
        bucket.append(item)

    # 1) 既有观测序列 + 既有当前值(老库白字符串在这里自愈)全部归位成观测。
    for key, values in (old or {}).items():
        norm = _normalize_key(key)
        if not norm:
            continue
        _touch(norm)
        if is_measurement(norm):
            entries = _as_list(stored.get(norm))
            # 已有观测序列时**不再吃 attributes**: 那份字符串只是当前值的物化视图,
            # 再吸收一遍会被当成"上次更新那天听到的"新观测, 把时间戳抬高到今天,
            # 于是任何补说历史的陈述都追不上它, 当前值也再也换不动。
            for raw in entries if entries else _as_list(values):
                _absorb(norm, raw)
        else:
            kept = [_clean(v) for v in _as_list(values) if _clean(v)]
            if kept:
                plain[norm] = kept

    # 2) 本轮提取的原子按三类语义落盘。
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _normalize_key(str(item.get("key", "")))
        value = _clean(item.get("value"))
        if not key or not value:
            continue
        _touch(key)
        existed = True
        if is_measurement(key):
            bucket = observations.setdefault(key, [])
            incoming = TimedValue(
                value=value,
                valid_at=as_datetime(item.get("valid_at")) or now,
                recorded_at=as_datetime(item.get("recorded_at")) or now,
                explicit=bool(item.get("explicit", False)),
            )
            if any(_same_observation(o, incoming) for o in bucket):
                continue  # 同值同时间再说一遍: 既不重复入列, 也不计更新
            head = _ordered(bucket, now=now, grace_days=grace_days)[0] if bucket else None
            if head is not None and head.value == value and not incoming.explicit:
                # 没带新时间的重复陈述(隔几天又说"我还是 70kg"): 只把当前那条的时间
                # 推新, 不另开观测 —— 否则历史里会堆出一串同一个值的"重说记录"。
                bucket[bucket.index(head)] = replace(head, valid_at=incoming.valid_at, recorded_at=incoming.recorded_at)
                continue
            existed = bool(bucket)
            bucket.append(incoming)
            if _ordered(bucket, now=now, grace_days=grace_days)[0].value != value:
                # 这句讲的是过去(生效时间早于当前态): 只入历史, 当前值不动。
                superseded += 1
        elif is_correction(key):
            if plain.get(key) == [value]:
                continue
            existed = bool(plain.get(key))
            plain[key] = [value]
        else:
            current = plain.get(key, [])
            if value in current:
                continue
            existed = bool(current)
            plain[key] = [*current, value]
        # 从无到有算新增, 只在"顶掉/追加到已有槽"时才算更新。
        if existed:
            updated += 1
        else:
            added += 1

    # 3) 派生当前值: 观测封顶后第一条即当前态; 属性顺序沿用老画像, 新键追加在后。
    attrs: dict[str, list[str]] = {}
    history_out: dict[str, list[dict]] = {}
    for key in order:
        bucket = observations.get(key)
        if bucket is not None:
            keep = _ordered(bucket, now=now, grace_days=grace_days)[: max(1, int(max_entries))]
            observations[key] = keep
            attrs[key] = [keep[0].value]
            history_out[key] = [o.to_dict() for o in keep]
        elif key in plain:
            attrs[key] = plain[key]
    return attrs, history_out, added, updated, superseded


def _same_observation(a: TimedValue, b: TimedValue) -> bool:
    """同一观测的判据: 值相同且生效时间相同。

    只看这两项而不比 ``recorded_at``: 记录时间只是"何时听到的"属量, 隔几天把
    "2020 年我汇报给王总"再说一遍不应多出一条历史(不然历史会重复堆品)。同值
    不同生效时间算一次新观测(同一个体重在不同时期各观测一次), 未标时间的两次陈述
    按各自本轮日期计(当天重说自然归一)。
    """
    return a.value == b.value and a.valid_at == b.valid_at


def _current_observation(entries: Any) -> TimedValue | None:
    """已存观测序列里的当前态那一条(写回时按"当前在前"排序, 取第一条能解析的)。"""
    for raw in _as_list(entries):
        item = _observation(raw, fallback_time=None)
        if item is not None:
            return item
    return None


def render_summary(
    attrs: dict[str, Any], max_chars: int | None = None, *, history: dict[str, Any] | None = None
) -> str:
    """把画像渲染成一行式 prompt 文本(模板拼接, 不调 LLM), 超长截断。

    波动类属性只有在**用户明说过生效时间**时才带上"（自 YYYY-MM）": 未标时间一律按
    当下生效, 逐个挂上兜底日期既挤占 ``profile_max_chars`` 又会让模型把系统兼听时间
    当成事实; 历史值不进 prompt(当前值已是按生效时间派生的那一条)。
    """
    max_chars = max_chars or get_settings().profile_max_chars
    stamps: dict[str, str] = {}
    for key, entries in (history or {}).items():
        top = _current_observation(entries)
        values = _as_list((attrs or {}).get(key))
        if top is None or not top.explicit or top.valid_at is None:
            continue
        if not values or _clean(values[0]) != top.value:
            continue
        stamps[str(key)] = format_month(top.valid_at)
    parts: list[str] = []
    for key, values in (attrs or {}).items():
        cleaned = [_clean(v) for v in _as_list(values) if _clean(v)]
        if not cleaned:
            continue
        suffix = f"（自{stamps[key]}）" if stamps.get(key) else ""
        parts.append(f"{key}：{'、'.join(cleaned)}{suffix}")
    text = "；".join(parts)
    return text[:max_chars]


class UserProfileStore:
    """画像的异步 DAO; 构造期不做任何 I/O, DB 不可用一律静默降级。"""

    def __init__(self) -> None:
        self._factory: async_sessionmaker[AsyncSession] | None = None

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            self._factory = get_session_factory()
        return self._factory

    async def get(self, user_id: str) -> dict[str, Any]:
        """读一份画像; 无记录/DB 不可用时返回空骨架(不抛出)。

        ``history`` 只装**被顶掉的**观测(观测序列第一条就是当前值, 已经在
        ``attributes`` 里), 给"我的记忆"页展开用; 它不进 prompt。
        """
        if not user_id:
            return _empty_profile()
        try:
            async with self._sessions()() as session:
                row = (
                    await session.execute(
                        select(UserProfileRow).where(UserProfileRow.user_id == user_id)
                    )
                ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 - 画像读不到只是少了这轮的画像注入
            logger.warning("用户画像读取失败, 本轮无画像: %s", exc)
            return _empty_profile()
        if row is None:
            return _empty_profile()
        attrs = row.attributes if isinstance(row.attributes, dict) else {}
        stored = row.attribute_history if isinstance(row.attribute_history, dict) else {}
        history = {
            str(key): _as_list(entries)[1:]
            for key, entries in stored.items()
            if len(_as_list(entries)) > 1
        }
        return {
            "attributes": attrs,
            "history": history,
            "summary": row.summary or render_summary(attrs, history=stored),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    async def merge(self, user_id: str, items: list[dict]) -> dict[str, int]:
        """合并画像原子并落库, 返回 ``{added, updated, superseded}``(失败返回全 0, 不抛出)。

        ``superseded`` 是"本轮这句讲的比当前态更早, 只入了历史、没改当前值"的条数 ——
        它正是"体重被十年前的陈述覆盖"这类事故的直接可观测指标(进审计)。
        """
        zero = {"added": 0, "updated": 0, "superseded": 0}
        if not user_id or not items:
            return zero
        try:
            async with self._sessions()() as session:
                async with session.begin():
                    row = (
                        await session.execute(
                            select(UserProfileRow).where(UserProfileRow.user_id == user_id)
                        )
                    ).scalar_one_or_none()
                    old = row.attributes if (row and isinstance(row.attributes, dict)) else {}
                    stored = row.attribute_history if (row and isinstance(row.attribute_history, dict)) else {}
                    attrs, history, added, updated, superseded = merge_attributes(
                        old,
                        items,
                        history=stored,
                        stored_at=(row.updated_at if row and row.updated_at else None),
                    )
                    # 没任何变动就不写(不推 updated_at, 不拿同一份内容反复 upsert);
                    # 但老行尚未存过观测序列时要补一次写, 让历史自愈落库。
                    if not added and not updated and (stored or not history):
                        return {**zero, "superseded": superseded}
                    summary = render_summary(attrs, history=history)
                    payload = {"attributes": attrs, "summary": summary, "attribute_history": history}
                    if row is None:
                        # create_all 不会给已存在表加列, 但新表本身由它建;
                        # upsert 用 PG 方言的 on_conflict(通用 insert 不支持)。
                        await session.execute(
                            pg_insert(UserProfileRow)
                            .values(user_id=user_id, **payload)
                            .on_conflict_do_update(index_elements=["user_id"], set_=payload)
                        )
                    else:
                        await session.execute(
                            update(UserProfileRow)
                            .where(UserProfileRow.user_id == user_id)
                            .values(**payload)
                        )
            return {"added": added, "updated": updated, "superseded": superseded}
        except Exception as exc:  # noqa: BLE001 - 画像写失败只是这轮的属性丢了
            logger.warning("用户画像合并失败, 本轮跳过: %s", exc)
            return zero

    async def clear(self, user_id: str) -> bool:
        """清空画像(删除整行); 画像没有"逐条删除"的粒度, 只能整体重来。"""
        if not user_id:
            return False
        try:
            async with self._sessions()() as session:
                async with session.begin():
                    await session.execute(
                        delete(UserProfileRow).where(UserProfileRow.user_id == user_id)
                    )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("用户画像清空失败: %s", exc)
            return False

    async def get_last_reflected_at(self, user_id: str) -> datetime | None:
        """上次情节蒸馏的时间点(无记录返回 None, 视为"从未蒸馏过")。"""
        if not user_id:
            return None
        try:
            async with self._sessions()() as session:
                row = (
                    await session.execute(
                        select(UserProfileRow.last_reflected_at).where(
                            UserProfileRow.user_id == user_id
                        )
                    )
                ).first()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取画像蒸馏水位线失败, 按未蒸馏处理: %s", exc)
            return None
        value = row[0] if row else None
        return value if isinstance(value, datetime) else None

    async def mark_reflected(self, user_id: str, *, at: datetime | None = None) -> None:
        """推进情节蒸馏水位线; 画像行不存在时补一行(只带水位线)。"""
        if not user_id:
            return
        stamp = at or datetime.now(timezone.utc)
        try:
            async with self._sessions()() as session:
                async with session.begin():
                    await session.execute(
                        pg_insert(UserProfileRow)
                        .values(user_id=user_id, attributes={}, summary="", last_reflected_at=stamp)
                        .on_conflict_do_update(
                            index_elements=["user_id"],
                            set_={"last_reflected_at": stamp},
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("推进画像蒸馏水位线失败(下轮会重试): %s", exc)


_profile_store: UserProfileStore | None = None


def get_profile_store() -> UserProfileStore:
    """进程级单例画像存储。"""
    global _profile_store
    if _profile_store is None:
        _profile_store = UserProfileStore()
    return _profile_store
