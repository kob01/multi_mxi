"""缓存层共享的 Redis 连接与降级工具。

设计原则与项目里 Elasticsearch / pgvector / rerank 完全一致 —— "能降级就降级,
绝不让缓存问题阻断对话":
- 进程级单例, 惰性建连(import 期不做任何网络 IO);
- ``redis_enabled=false`` 时 ``get_redis()`` 直接返回 ``None``, 调用方必须自带
  "None -> 走真实调用" 的分支;
- Redis 对象本身连不上时, ``redis.asyncio`` 是在第一条命令执行时才报错, 不是在
  ``from_url`` 时 —— 所以真正的降级必须发生在每条命令上, 由 ``try_redis()``
  统一兜底(捕获后只 WARNING, 返回默认值), 而不是只在建连处判断一次。
- 所有缓存 key 统一带 ``mxi:cache`` 前缀, 与 langgraph-checkpoint-redis 自身
  的 key 布局(RedisJSON/RediSearch 索引)互不干扰, 也便于按前缀整体失效。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from redis.asyncio import Redis

from app.config import get_settings

logger = logging.getLogger(__name__)

CACHE_PREFIX = "mxi:cache"
SESSION_PREFIX = "mxi:session"

T = TypeVar("T")

_redis: Redis | None = None


def get_redis() -> Redis | None:
    """进程级单例; ``redis_enabled=false`` 时返回 ``None``(调用方必须容忍降级)。

    这里不做 ping 探活: ``redis.asyncio`` 本身是惰性连接, 探活反而会在启动
    路径上引入一次不必要的阻塞。真正的连通性问题由 ``try_redis()`` 在每条
    命令执行时兜住。
    """
    global _redis
    settings = get_settings()
    if not settings.redis_enabled:
        return None
    if _redis is None:
        _redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis


async def try_redis(
    op: Callable[[], Awaitable[T]],
    *,
    default: T | None = None,
    what: str = "redis op",
) -> T | None:
    """执行一条 Redis 命令, 任何异常(连接失败/超时/序列化问题)都降级为 ``default``。

    缓存的正确性语义是"未命中"而不是"报错", 因此这里吞掉所有异常并只记
    WARNING —— 与 ``app/assistant/intent.py`` 里 embedding 层失败下沉 LLM
    的降级策略同源。
    """
    try:
        return await op()
    except Exception as exc:  # noqa: BLE001 - 缓存永不阻断主流程
        logger.warning("%s 失败, 本次降级为不走缓存: %s", what, exc)
        return default


async def close_redis() -> None:
    """释放连接池(供 FastAPI lifespan 关闭时调用, 不做也不影响功能)。"""
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("closing redis client failed: %s", exc)
        _redis = None
