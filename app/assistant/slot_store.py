"""问数会话槽位存储: 让"那研发部呢"这类省略追问能接到上一轮的口径上。

为什么单独一个存储而不是复用 Session Memory: 会话记忆是**给人看的文本摘要**,
把它当槽位来源要先解析一次自然语言, 又把确定性变成了概率。这里存的是上一轮
AskData_Agent 明确产出的结构化槽位(``payload["slots"]``), 继承是一条 dict 合并,
零推断 —— 与"复合问法不再由 LLM 拆句"那次决策是同一个取向。

形状与 :mod:`app.assistant.memory` 一致: Redis 为主, 连不上退回进程内有界 LRU。
代价也说清: 退回内存时多 worker 各存一份, 同一会话打到不同进程会丢继承
(表现为"又问了一次时间范围"), 而不是丢正确性 —— 宁可多问, 不要错答。
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any

from app.cache.redis_client import SESSION_PREFIX, get_redis, try_redis
from app.config import get_settings

logger = logging.getLogger(__name__)

_LOCAL_MAX = 500
_TTL_FALLBACK = 1800


class SlotStore:
    """per-session 的"上一轮问数槽位"存储(Redis 优先, 进程内 LRU 降级)。"""

    def __init__(self) -> None:
        self._local: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _redis_or_none(self):
        return get_redis() if get_settings().redis_enabled else None

    def _key(self, session_id: str) -> str:
        return f"{SESSION_PREFIX}:{session_id}:askdata_slots"

    def _ttl(self) -> int:
        return int(get_settings().session_memory_ttl or _TTL_FALLBACK)

    async def get(self, session_id: str) -> dict[str, Any]:
        """取上一轮槽位; 会话号为空或读不出来都返回空 dict(= 不做任何继承)。"""
        if not session_id:
            return {}
        redis = self._redis_or_none()
        if redis is not None:
            raw = await try_redis(
                lambda: redis.get(self._key(session_id)), default=None,
                what="askdata slot read",
            )
            if raw:
                parsed = _loads(raw)
                if parsed is not None:
                    return parsed
        return dict(self._local.get(session_id) or {})

    async def set(self, session_id: str, slots: dict[str, Any]) -> None:
        """写入本轮槽位(空槽位不写, 免得把上一轮的可用继承信息擦干净)。"""
        if not session_id or not slots:
            return
        self._local[session_id] = slots
        self._local.move_to_end(session_id)
        while len(self._local) > _LOCAL_MAX:
            self._local.popitem(last=False)
        redis = self._redis_or_none()
        if redis is None:
            return
        try:
            payload = json.dumps(slots, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:  # noqa: BLE001 - 载荷里有奇怪类型时只走内存路径
            logger.warning("[askdata] 槽位序列化失败(只存进程内): %s", exc)
            return
        await try_redis(
            lambda: redis.set(self._key(session_id), payload, ex=self._ttl()),
            default=None, what="askdata slot write",
        )

    async def clear(self, session_id: str) -> None:
        """会话被删除时一起清掉(否则换话题重用会话号会带上旧口径)。"""
        self._local.pop(session_id, None)
        redis = self._redis_or_none()
        if redis is None:
            return
        await try_redis(lambda: redis.delete(self._key(session_id)), default=None,
                        what="askdata slot delete")


def _loads(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw if isinstance(raw, str) else raw.decode())
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


_store: SlotStore | None = None


def get_slot_store() -> SlotStore:
    """进程级单例(与 ``get_stream_hub`` / ``get_memory_store`` 同一形状)。"""
    global _store
    if _store is None:
        _store = SlotStore()
    return _store
