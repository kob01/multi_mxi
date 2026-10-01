"""能力域用量闸门: 按"调用者 × 能力域 × 自然日"计数限流。

为什么需要它: ``web`` / ``docgen`` 是进程内能力域(见 ``app/tools``), 不走 MCP 的
角色×工具矩阵, 改动前连域级检查都没有 —— 而 ``docgen`` 每次调用都会落一个磁盘文件、
``web`` 每次都会打外网, 两者都消耗 LLM 配额。没有上限时, 一句"把这份数据导成 pdf"
反复重试就能把磁盘和配额吃干净。

降级口径(与全项目"能降级就降级, 但降级必须可观测"一致):
- Redis 可用: ``INCR`` + 当天末过期, 计数是全局的;
- Redis 不可用/命令失败: 退到进程内计数(单副本 dev 足够; 多副本时上限会偏松,
  因为每个副本只看见自己那份流量), 并记 WARNING —— 绝不因为"拿不到计数"就当作无限。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.cache.redis_client import get_redis, try_redis
from app.config import get_settings

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
QUOTA_PREFIX = "mxi:quota"
# 进程内降级表的条目硬顶: 键里带日期, 昨天的键永不再命中, 所以可以直接清掉。
_LOCAL_MAX = 5000
_local: dict[str, int] = {}


def _day() -> str:
    return datetime.now(_CST).strftime("%Y%m%d")


def _seconds_to_day_end() -> int:
    now = datetime.now(_CST)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return max(1, int((end - now).total_seconds()) + 1)


def resolve_daily_limit() -> int:
    """当前配置的自然日调用上限(至少 1 次, 免得配 0 被当成" unlimited")。"""
    return max(1, int(get_settings().capability_daily_limit))


async def check_capability_quota(kind: str, subject: str, limit: int | None = None) -> tuple[bool, int]:
    """记一次用量并判定是否放行。

    Args:
        kind: 能力域名(``web`` / ``docgen``)。
        subject: 计数主体, 传调用者工号; 匿名时传空串(按 ``anonymous`` 归并)。
        limit: 显式上限; 留空取 ``settings.capability_daily_limit``。

    Returns:
        ``(allowed, used)`` —— ``used`` 含本次调用, 便于答复里写"今日还剩 N 次"。
    """
    cap = limit if limit is not None else resolve_daily_limit()
    redis = get_redis()
    if redis is not None:
        key = f"{QUOTA_PREFIX}:{kind}|{_day()}|{subject or 'anonymous'}"
        used = await try_redis(lambda: redis.incr(key), default=None, what="quota incr")
        if used is not None:
            used = int(used)
            if used == 1:
                await try_redis(
                    lambda: redis.expire(key, _seconds_to_day_end()), what="quota expire"
                )
            return used <= cap, used
        logger.warning("能力域配额计数失败(redis 未应答), 本次降级为进程内计数")

    lkey = f"{kind}|{_day()}|{subject or 'anonymous'}"
    today = _day()
    if len(_local) >= _LOCAL_MAX:
        for stale in [k for k in _local if k.split("|", 2)[1] != today]:
            _local.pop(stale, None)
        while len(_local) >= _LOCAL_MAX:
            _local.pop(next(iter(_local)))
    used = _local.get(lkey, 0) + 1
    _local[lkey] = used
    return used <= cap, used


def remaining(used: int, limit: int) -> int:
    """剩余可用次数(展示用, 不为负)。"""
    return max(0, limit - int(used))
