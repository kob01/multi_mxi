"""Retrieval Cache: 缓存"同一查询 + 同一权限主体 -> 同一批已正文就位的召回子块"。

key 必须包含 ACL 签名(user_id + department + role): 命中时整条检索链路被跳过
(embedding + 两通道召回 + RRF + 回表 + rerank 及其阈值), 等于跳过两道**前置**权限裁剪
(向量 SQL 谓词 / ES bool filter), 故只有同一 principal 能重放到自己那一份结果; 若 key
不含身份签名, 甲用户有权看到的私有文档命中后会被原样复用给无权看到的乙用户, 这是越权
泄露。进 Context Builder 前那道最终授权(``acl.is_allowed``)在调用点照常执行、不受命中
影响 —— 纵深防御始终在位。

失效按**文档粒度**做, 不再 ``invalidate_all()``: 缓存里存的是 ``chunk_id`` 列表, 重入库后
旧 ``chunk_id`` 可能已标陈旧或被新行覆盖, 不失效就会拿到脏数据; 但全清会让一次入库把
整站热查询的缓存一起打掉(缓存击空后全量回源 embedding + 两通道 + rerank)。
这里的做法是给每篇文档一个递增计数 ``docgen:{doc_key}``, 写缓存时把本次命中的文档
版本快照存进 payload, 读时一次 MGET 比对: 任一不符即当未命中并自删该 key。受影响
的只有"真的命中了那篇文档"的查询, 其余继续命中。

``invalidate_all()`` 保留, 只用于运维路径(整库 BM25 重建、存储层迁移后收敛)。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence

from app.cache.redis_client import CACHE_PREFIX, get_redis, try_redis
from app.config import get_settings
from app.schemas import KnowledgeChunk
from app.security.acl import Principal

logger = logging.getLogger(__name__)

# 每篇文档一个递增计数: 发布完成/权限变更/文档删除三个时机 bump 它。
DOCGEN_PREFIX = f"{CACHE_PREFIX}:retrieval:docgen"


def acl_signature(principal: Principal | None) -> str:
    """把身份主体压成一段稳定字符串, 用于参与缓存 key(不含原文, 只作区分)。"""
    if principal is None:
        return "anonymous"
    return f"{principal.user_id or '-'}|{principal.department or '-'}|{principal.role.value}"


def _key(query: str, top_k: int, top_n: int, principal: Principal | None) -> str:
    material = f"{query}|{top_k}|{top_n}|{acl_signature(principal)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:retrieval:{digest}"


def _docgen_key(doc_key: str) -> str:
    return f"{DOCGEN_PREFIX}:{doc_key}"


async def get_doc_gens(doc_keys: list[str]) -> dict[str, int]:
    """一次 MGET 拿回若干文档的当前版本号(缺键=0, 读失败=空 dict 让调用方当未命中)。

    读失败必须归到"未命中"而不是"版本全都等于 0": 后者会让 Redis 抖动期间
    把陈旧 payload 当成有效结果返回(越新越危险的那种降级)。
    """
    redis = get_redis()
    if redis is None or not doc_keys:
        return {}
    uniq = list(dict.fromkeys(doc_keys))

    async def _do() -> dict[str, int]:
        raw = await redis.mget([_docgen_key(k) for k in uniq])
        return {
            k: int(v) if v is not None else 0 for k, v in zip(uniq, raw or [])
        }

    out = await try_redis(_do, default={}, what="retrieval docgen mget")
    return out or {}


async def bump_doc_gen(doc_keys: Sequence[str]) -> None:
    """定向失效: 把这批文档的缓存版本号 +1(等价于"含它们的缓存项下次当未命中")。

    O(文档数) 次 INCR, 不 SCAN 不 KEYS —— Redis 里缓存项上万条时也不会因一篇
    文档入库而扫全库。Redis 不可用时什么都不做: 降级语义是"没失效"而不是
    "报错", TTL 到期会自然收敛(与 try_redis 同一口径)。
    """
    redis = get_redis()
    if redis is None:
        return
    uniq = [k for k in dict.fromkeys(doc_keys) if k]
    if not uniq:
        return

    async def _do() -> None:
        pipe = redis.pipeline()
        for key in uniq:
            pipe.incr(_docgen_key(key))
        await pipe.execute()

    await try_redis(_do, what="retrieval docgen bump")


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
                cached_gens = payload.get("doc_gens") or {}
                # 旧版本 payload(无 doc_gens)一律当未命中: 宁可多一次回源, 也不能
                # 把"升级前写的脏条目"当成已验证结果。
                if cached_gens:
                    current = await get_doc_gens(list(cached_gens))
                    if current and all(current.get(k, 0) == v for k, v in cached_gens.items()):
                        return chunks, payload["score_mode"], True
                    await try_redis(
                        lambda: redis.delete(key), what="retrieval cache invalidate(stale gen)"
                    )
                else:
                    await try_redis(lambda: redis.delete(key), what="retrieval cache invalidate(legacy)")
            except Exception as exc:  # 脏缓存数据不能阻断本次检索
                logger.warning("retrieval cache payload invalid, re-retrieving: %s", exc)
                await try_redis(lambda: redis.delete(key), what="retrieval cache invalidate")

    chunks, score_mode = await invoke()

    if redis is not None:
        doc_ids = list(dict.fromkeys(c.doc_id for c in chunks if c.doc_id))
        # 零命中的结果也值得缓存(带 TTL 的空结果能挡住反复追问同一不存在的知识);
        # 此时 doc_gens 为空, 命中校验直接通过 —— 它的失效只能靠全局 invalidate_all。
        gens = await get_doc_gens(doc_ids) if doc_ids else {}
        payload = json.dumps(
            {
                "chunks": [c.model_dump() for c in chunks],
                "score_mode": score_mode,
                "doc_gens": gens,
            },
            ensure_ascii=False,
        )
        await try_redis(
            lambda: redis.set(key, payload, ex=settings.retrieval_cache_ttl),
            what="retrieval cache set",
        )
    return chunks, score_mode, False


async def invalidate_all() -> None:
    """清空全部 Retrieval Cache —— **仅运维路径使用**(整库 BM25 重建/存储层迁移)。

    入库热路径请用 :func:`bump_doc_gen`: 全清在缓存项上万条时本身就是性能事故。
    """
    redis = get_redis()
    if redis is None:
        return
    prefix = f"{CACHE_PREFIX}:retrieval:*"

    async def _do() -> None:
        # 用 SCAN 而不是 KEYS: KEYS 在大 key 空间上会阻塞 Redis 实例。
        async for k in redis.scan_iter(match=prefix, count=200):  # type: ignore[union-attr]
            await redis.delete(k)

    await try_redis(_do, what="retrieval cache invalidate_all")
