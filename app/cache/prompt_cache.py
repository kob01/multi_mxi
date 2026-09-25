"""Prompt Cache: 缓存"完全相同的 prompt -> LLM 文本回复"这一映射。

只用于无状态、不含实时权限判定的调用点(查询改写 / 意图 LLM 兜底 / 闲聊)。
严禁用于知识库生成(``kb_generate``)——那条链路带文档级 ACL 与实时检索结果,
把它的输出缓存进 Redis 等价于把"某次权限裁剪后的答案"共享给后续可能无权的
其它查询, 是越权风险而不是性能优化。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable

from app.cache.redis_client import CACHE_PREFIX, get_redis, try_redis
from app.config import get_settings

logger = logging.getLogger(__name__)


def _key(model: str, temperature: float, prompt: str) -> str:
    digest = hashlib.sha256(f"{model}|{temperature}|{prompt}".encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:prompt:{digest}"


async def cached_llm_call(
    model: str,
    temperature: float,
    prompt: str,
    invoke: Callable[[], Awaitable[str]],
) -> str:
    """查 Prompt Cache, 未命中则 ``await invoke()`` 走真实 LLM 调用并回填缓存。

    ``invoke`` 由调用方传入(闭包持有自己的 LLM 实例/降级逻辑), 本函数不关心
    底层是 DeepSeek 还是 Ollama, 也不关心失败重试 —— 这些都在调用点决定。
    """
    settings = get_settings()
    redis = get_redis() if settings.cache_enabled else None
    key = _key(model, temperature, prompt)

    if redis is not None:
        hit = await try_redis(lambda: redis.get(key), what="prompt cache get")
        if hit is not None:
            logger.debug("prompt cache hit: %s", key[-12:])
            return hit

    result = await invoke()

    if redis is not None and result:
        await try_redis(
            lambda: redis.set(key, result, ex=settings.prompt_cache_ttl),
            what="prompt cache set",
        )
    return result
