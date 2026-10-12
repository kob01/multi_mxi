"""问数结果缓存: 同一个"查询意图 + 同一个身份 + 同一版口径"在 TTL 内不重复打库。

key 里为什么必须有作用域与 user_id(方案的三段式是 ``normalize(dsl)+role+semver``):
本系统的可见行集是"部门作用域"决定的, 两个同为 manager 角色的人能读到的行**不一样**。
按方案的三段做键, 第一个人的结果会被第二个人拿到 —— 那是一次跨部门的数据泄露,
而且它在看板上完全看不出来(数字是对的, 只是对别人而言)。所以这里把作用域与工号也
放进键里, 代价是命中率低一些: 缓存省的是毫秒, 泄露赔的是信任。

其余口径:
- **只缓存成功结果**: 失败/被拒的一次错误不该在 TTL 内反复糊在用户脸上;
- **Redis 优先, 进程内 TTL 降级**: 与 ``slot_store`` / ``memory`` 同一形状 —— Redis 抖动
  只让缓存命中率掉下来, 不影响正确性;
- **semver 进键**: 口径被修订的同一秒旧结果就失效(不需要显式清缓存), 这正是 G1 的副产品。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = "askdata:res:"
_LOCAL_MAX = 256

_LOCK = threading.Lock()
_LOCAL: dict[str, tuple[float, dict[str, Any]]] = {}
_client = None
_client_failed_at = 0.0


def _ttl() -> int:
    return max(1, int(get_settings().askdata_result_cache_ttl or 300))


# 语义缓存命中时写进留痕注记里的那个标:
# 写入方(状态机)与统计方(store.stats)共用一个常量 —— 两边各自手拼一个中文串,
# 早晚会出现"注记改了但统计条件没改", 结果是命中率永远 0 而没人知道。
SEMANTIC_CACHE_MARKER = "复用了已标注问法"


def cache_key(
    *, dsl: Any, route: str, user_id: str, role: str, semver: str, scope_note: str = "",
) -> str:
    """归一化后的缓存键(dsl 的键序与空字段不影响结果)。"""
    normalized = json.dumps(_prune(dsl), ensure_ascii=False, sort_keys=True, default=str)
    material = "\n".join([normalized, route or "", user_id or "", role or "", semver or "", scope_note or ""])
    return _PREFIX + hashlib.sha1(material.encode("utf-8")).hexdigest()


def _prune(value: Any) -> Any:
    """递归剥掉空串/空列表: ``{"limit": ""}`` 与没有这个键是同一个查询。"""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            cleaned = _prune(item)
            if cleaned in ("", [], {}, None):
                continue
            out[key] = cleaned
        return out
    if isinstance(value, (list, tuple)):
        return [_prune(item) for item in value]
    return value


def enabled() -> bool:
    return bool(get_settings().askdata_result_cache_enabled)


def get(key: str) -> dict[str, Any] | None:
    """取缓存; 任何异常都当"没命中"(缓存不是正确性依赖)。"""
    if not enabled():
        return None
    now = time.monotonic()
    with _LOCK:
        hit = _LOCAL.get(key)
        if hit and now - hit[0] <= _ttl():
            return hit[1]
        if hit:
            _LOCAL.pop(key, None)
    client = _redis_client()
    if client is not None:
        try:
            raw = client.get(key)
        except Exception as exc:  # noqa: BLE001 - Redis 不可用就退进程内
            logger.debug("[askdata] 结果缓存读失败(退进程内): %s", exc)
            raw = None
        if raw:
            parsed = _loads(raw)
            if parsed is not None:
                with _LOCK:
                    _LOCAL[key] = (now, parsed)
                return parsed
    return None


def put(key: str, payload: dict[str, Any]) -> None:
    """写缓存(带 rows 的结果体, 只走 JSON 可序列化的字段)。"""
    if not enabled() or not payload or payload.get("error"):
        return
    with _LOCK:
        _LOCAL[key] = (time.monotonic(), payload)
        while len(_LOCAL) > _LOCAL_MAX:
            _LOCAL.pop(next(iter(_LOCAL)))
    client = _redis_client()
    if client is None:
        return
    try:
        client.setex(key, _ttl(), json.dumps(payload, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001 - 写失败只意味着下一轮再算一次
        logger.debug("[askdata] 结果缓存写失败(忽略): %s", exc)


def _redis_client():
    """惰性建同步 Redis 客户端; 连不上时 60 秒内不再试(别每轮问数都撞一次超时)。"""
    global _client, _client_failed_at
    settings = get_settings()
    if not settings.redis_enabled or not settings.redis_url:
        return None
    with _LOCK:
        if _client is not None:
            return _client
        if _client_failed_at and time.time() - _client_failed_at < 60:
            return None
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            decode_responses=True,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 没装/连不上都按"只有进程内缓存"处理
        with _LOCK:
            _client_failed_at = time.time()
        logger.debug("[askdata] 结果缓存不接 Redis(只用进程内): %s", exc)
        return None
    with _LOCK:
        _client = client
    return client


def _loads(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw if isinstance(raw, str) else raw.decode())
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
