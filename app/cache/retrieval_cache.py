"""Retrieval Cache: 缓存"同一查询 + 同一权限主体 -> 同一批召回子块"。

key 必须包含 ACL 签名(user_id + department + role)——命中缓存时会直接跳过
``build_sql_filter`` / ``is_allowed`` 两道权限裁剪, 若 key 不含身份签名, 甲用户
有权看到的私有文档命中后会被原样复用给无权看到的乙用户, 这是越权泄露。

必须在文档重新入库时 ``invalidate_all()``: 缓存里存的是 ``chunk_id`` 列表,
单文档重入库会先删后建(``delete_by_doc``), 旧 ``chunk_id`` 可能已不存在或指向
重建后的新块, 不失效就会拿到脏数据 —— 这一时机已经由
``AssistantOrchestrator.refresh_knowledge()`` 在 ``app/docs/service.py`` 的
入库路径上触发, 无需另设失效通道。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable

from app.cache.redis_client import CACHE_PREFIX, get_redis, try_redis
from app.config import get_settings
from app.schemas import KnowledgeChunk
from app.security.acl import Principal

logger = logging.getLogger(__name__)


def acl_signature(principal: Principal | None) -> str:
    """把身份主体压成一段稳定字符串, 用于参与缓存 key(不含原文, 只作区分)。"""
    if principal is None:
        return "anonymous"
    return f"{principal.user_id or '-'}|{principal.department or '-'}|{principal.role.value}"


def _key(query: str, top_k: int, top_n: int, principal: Principal | None) -> str:
    material = f"{query}|{top_k}|{top_n}|{acl_signature(principal)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:retrieval:{digest}"


async def cached_retrieve(
    query: str,
    top_k: int,
    top_n: int,
    principal: Principal | None,
    invoke: Callable[[], Awaitable[tuple[list[KnowledgeChunk], str]]],
) -> tuple[list[KnowledgeChunk], str, bool]:
    """查 Retrieval Cache; 未命中则 ``await invoke()`` 真实检索并回填。

    Returns ``(chunks, score_mode, cache_hit)`` —— ``cache_hit`` 供调用点写审计,
    方便在 LangSmith / audit.jsonl 里核对缓存是否按预期生效/失效。
    """
    settings = get_settings()
    redis = get_redis() if settings.cache_enabled else None
    key = _key(query, top_k, top_n, principal)

    if redis is not None:
        raw = await try_redis(lambda: redis.get(key), what="retrieval cache get")
        if raw:
            try:
                payload = json.loads(raw)
                chunks = [KnowledgeChunk.model_validate(c) for c in payload["chunks"]]
                return chunks, payload["score_mode"], True
            except Exception as exc:  # 脏缓存数据不能阻断本次检索
                logger.warning("retrieval cache payload invalid, re-retrieving: %s", exc)
                await try_redis(lambda: redis.delete(key), what="retrieval cache invalidate")

    chunks, score_mode = await invoke()

    if redis is not None:
        payload = json.dumps(
            {"chunks": [c.model_dump() for c in chunks], "score_mode": score_mode},
            ensure_ascii=False,
        )
        await try_redis(
            lambda: redis.set(key, payload, ex=settings.retrieval_cache_ttl),
            what="retrieval cache set",
        )
    return chunks, score_mode, False


async def invalidate_all() -> None:
    """清空全部 Retrieval Cache (文档重新入库 / BM25 索引重建时调用)。"""
    redis = get_redis()
    if redis is None:
        return
    prefix = f"{CACHE_PREFIX}:retrieval:*"

    async def _do() -> None:
        # 用 SCAN 而不是 KEYS: KEYS 在大 key 空间上会阻塞 Redis 实例。
        async for k in redis.scan_iter(match=prefix, count=200):  # type: ignore[union-attr]
            await redis.delete(k)

    await try_redis(_do, what="retrieval cache invalidate_all")
