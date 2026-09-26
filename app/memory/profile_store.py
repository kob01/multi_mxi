"""用户画像 (User Memory 的 profile 桶): ``user_profiles`` 表, 一人一条。

画像与其余四个桶最大的差别是"覆盖而非追加": 用户换了部门, 画像里应当是新值
顶掉旧值, 而不是两条并存的事实。所以这里单独建表, 不进 ``long_term_memories``
做向量查重 —— 相似度查重救不了"部门: 研发部 / 部门: 财务部"这种同键不同值。

合并与摘要渲染都是确定性规则, 不调 LLM: 提取侧只负责给出 ``{key, value}`` 原
子, 怎么覆盖/累积由本模块按固定语义决定。每轮对话把整份画像全量注入 prompt
(体量由 ``profile_max_chars`` 硬性封顶), 因此不需要 embedding。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import UserProfileRow
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

# 单值槽位: 新值直接顶掉旧值(身份类属性不会同时成立两条)。
_SINGLE_VALUE_KEYS = {
    "姓名", "name",
    "工号", "employee_id", "emp_id",
    "部门", "department", "dept",
    "职位", "岗位", "position", "title",
    "职级", "level", "grade",
    "汇报对象", "manager", "leader",
    "所在地", "城市", "location", "city",
    "入职时间", "hire_date",
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
}


# 空骨架: 读路径无行时返回它, 调用方不必判 None。
_EMPTY_ATTRIBUTES: dict[str, list[str]] = {}


def _normalize_key(key: str) -> str:
    """键归一: 去首尾空白 + 已知英文别名映射为中文槽名(未知键保留原样)。"""
    raw = (key or "").strip()
    probe = raw.lower().replace(" ", "_")
    return _KEY_ALIASES.get(probe, raw)


def _is_single_value(key: str) -> bool:
    probe = key.lower().replace("_", "")
    return any(probe == s.lower().replace("_", "") for s in _SINGLE_VALUE_KEYS)


def _clean(value: Any, max_len: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_len]


def merge_attributes(
    old: dict[str, Any] | None, items: list[dict]
) -> tuple[dict[str, list[str]], int, int]:
    """把提取到的 ``[{key, value}]`` 合并进既有画像(纯函数, 便于单测)。

    返回 ``(新画像, 新增槽位数, 更新次数)``。规则:

    - 英文同义键先归一(如 department -> 部门), 避免同一事实占两个槽;
    - 身份类键(单值槽): 值变了就整体替换, 相同则跳过(不算更新);
    - 其余键(多值槽): 并入集合去重, 只增不删 —— 画像不做"猜测式遗忘",
      删旧值留给用户在前端自服务;
    - 空 key / 空 value 一律忽略, 不用无意义值污染画像。
    """
    attrs: dict[str, list[str]] = {
        _normalize_key(k): [_clean(v) for v in vals if _clean(v)]
        for k, vals in (old or {}).items()
        if _normalize_key(k)
    }
    added = updated = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _normalize_key(str(item.get("key", "")))
        value = _clean(item.get("value"))
        if not key or not value:
            continue
        current = attrs.get(key, [])
        existed = bool(current)
        if _is_single_value(key):
            if current == [value]:
                continue
            attrs[key] = [value]
        else:
            if value in current:
                continue
            attrs[key] = [*current, value]
        # 从无到有算新增, 只在"顶掉/追加到已有槽"时才算更新。
        if existed:
            updated += 1
        else:
            added += 1
    return attrs, added, updated


def render_summary(attrs: dict[str, Any], max_chars: int | None = None) -> str:
    """把画像渲染成一行式 prompt 文本(模板拼接, 不调 LLM), 超长截断。"""
    max_chars = max_chars or get_settings().profile_max_chars
    parts = [
        f"{key}：{'、'.join(str(v) for v in values if _clean(v))}"
        for key, values in (attrs or {}).items()
        if any(_clean(v) for v in (values if isinstance(values, list) else [values]))
    ]
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
        """读一份画像; 无记录/DB 不可用时返回空骨架(不抛出)。"""
        if not user_id:
            return {"attributes": dict(_EMPTY_ATTRIBUTES), "summary": "", "updated_at": None}
        try:
            async with self._sessions()() as session:
                row = (
                    await session.execute(
                        select(UserProfileRow).where(UserProfileRow.user_id == user_id)
                    )
                ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 - 画像读不到只是少了这轮的画像注入
            logger.warning("用户画像读取失败, 本轮无画像: %s", exc)
            return {"attributes": dict(_EMPTY_ATTRIBUTES), "summary": "", "updated_at": None}
        if row is None:
            return {"attributes": dict(_EMPTY_ATTRIBUTES), "summary": "", "updated_at": None}
        attrs = row.attributes if isinstance(row.attributes, dict) else {}
        return {
            "attributes": attrs,
            "summary": row.summary or render_summary(attrs),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    async def merge(self, user_id: str, items: list[dict]) -> dict[str, int]:
        """合并画像原子并落库, 返回 ``{added, updated}``(失败返回全 0, 不抛出)。"""
        if not user_id or not items:
            return {"added": 0, "updated": 0}
        try:
            async with self._sessions()() as session:
                async with session.begin():
                    row = (
                        await session.execute(
                            select(UserProfileRow).where(UserProfileRow.user_id == user_id)
                        )
                    ).scalar_one_or_none()
                    old = row.attributes if (row and isinstance(row.attributes, dict)) else {}
                    attrs, added, updated = merge_attributes(old, items)
                    if not added and not updated:
                        return {"added": 0, "updated": 0}
                    summary = render_summary(attrs)
                    payload = {"attributes": attrs, "summary": summary}
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
            return {"added": added, "updated": updated}
        except Exception as exc:  # noqa: BLE001 - 画像写失败只是这轮的属性丢了
            logger.warning("用户画像合并失败, 本轮跳过: %s", exc)
            return {"added": 0, "updated": 0}

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
