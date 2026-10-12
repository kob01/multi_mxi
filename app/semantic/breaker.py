"""引擎连续错误熔断: 同一张表在短时间内反复报错时, 直接短路这一级取数路径。

为什么需要(逐轮降级不够): 库里某张表坏了(缺列/锁死/统计异常)时, 每一轮问数都会
重新撞一次同一个错误 —— 用户等到超时才拿到"取数执行失败", 而引擎在被持续加压。
熔断把这件事从"每次都试"变成"这段时间不试", 并且**降级到下一级**而不是整体拒答
(编译器挂了还可以走沙箱; 都挂了才给拒答与人工工单)。

三个口径:
- 只数**引擎错**(执行异常/超时), 不数扫描量超限与权限拒: 后两者是正确的拒绝,
  把它们计入熔断会让"多人问同一个大表"变成"这个口径全公司都不给查";
- 计数键是 ``(级别, 事实表)``, 不是全局: 一张表坏了不该让别的域也问不出来;
- 半开恢复: 窗口过期后第一次请求会放行, 成功则清零, 失败则立刻重新打开。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from app.config import get_settings

LEVEL_COMPILER = "compiler"
LEVEL_SANDBOX = "sandbox"
LEVEL_METRIC_API = "metric_api"

_LOCK = threading.Lock()
# key -> [连续错误数, 首次错误时间, 打开到何时(0=未打开)]
_STATE: dict[str, list[float]] = {}

# 给 /metrics 与看板用的累计计数(不参与放行判断, 只是"熔断过几次")
TRIPS = 0


def _ttl() -> float:
    return max(5.0, float(get_settings().askdata_engine_breaker_ttl or 60))


def _threshold() -> int:
    return max(2, int(get_settings().askdata_engine_breaker_threshold or 3))


def key_for(level: str, table: str) -> str:
    return f"{level or '?'}|{table or '?'}"


def is_open(level: str, table: str) -> bool:
    """这一级现在是不是被熔断着(打开期间直接跳过, 不碰库)。"""
    if int(get_settings().askdata_engine_breaker_threshold or 0) <= 0:
        return False  # 阈值 0/负 = 显式关掉熔断
    with _LOCK:
        state = _STATE.get(key_for(level, table))
        if not state:
            return False
        until = float(state[2] or 0.0)
        if until and time.monotonic() < until:
            return True
        if until:
            # 窗口已过: 清成"半开"(下一次成功就全清, 失败则再次打开)。
            state[2] = 0.0
        return False


def record_error(level: str, table: str) -> bool:
    """记一次引擎错误; 返回本次是否**刚刚打开**。"""
    global TRIPS
    threshold = _threshold()
    if threshold <= 0:
        return False
    key = key_for(level, table)
    now = time.monotonic()
    ttl = _ttl()
    with _LOCK:
        count, first_at, until = _STATE.get(key) or (0.0, now, 0.0)
        if until and now < until:
            return False  # 已经开着: 不应重复报"刚打开", 否则 trips 会被同一件事多次计数
        if count and now - float(first_at) > ttl:
            count, first_at = 0.0, now  # 冷却窗口外的旧错误不累计
        count += 1.0
        just_opened = False
        until = 0.0
        if count >= threshold:
            until = now + ttl
            TRIPS += 1
            just_opened = True
        _STATE[key] = [count, float(first_at), until]
        return just_opened


def record_success(level: str, table: str) -> None:
    """一次成功就清零(熔断只针对"持续"错误, 不针对偶发)。"""
    with _LOCK:
        _STATE.pop(key_for(level, table), None)


def snapshot() -> dict[str, Any]:
    """当前熔断状态(给 /metrics 与运维看: 哪些级别/表正在被短路)。"""
    now = time.monotonic()
    with _LOCK:
        opened = {
            key: round(float(state[2]) - now, 1)
            for key, state in _STATE.items()
            if float(state[2] or 0.0) > now
        }
        counts = {key: int(state[0] or 0) for key, state in _STATE.items()}
    return {"opened": opened, "consecutive_errors": counts, "trips_total": TRIPS}


def reset() -> None:
    """清空状态(测试与运维手动恢复用)。"""
    global TRIPS
    with _LOCK:
        _STATE.clear()
        TRIPS = 0
