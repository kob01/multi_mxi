"""Loki 日志推送通道(可选): 把问数的结构化事件按 label 推给 Loki。

三条口径, 都是为了让"接可观测"不变成"给业务加依赖":

1. **默认关闭**: ``LOKI_URL`` 留空就是彻底不做事(不建连接、不起线程、不占时延)。
2. **尽力而为**: 推送失败只记一条 debug, 绝不把已经算好的答案换成报错; 留痕表才是事实源,
   Loki 只是"能按 label 聚合查日志"的那一份副本。
3. **不推内容只推标识**: 问句原文、SQL、结果行都不进 Loki(链路/日志系统通常比数据库宽松),
   只推 verdict/route/semver/错误码/耗时这类**运维要看、业务不该看**的字段。

行格式用 Loki 的 JSON entry(line 本身是一段 JSON), 这样在 Grafana 里可以直接
`json_extract` 出字段做表格与告警, 不必再配一套 logql 正则。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_CLIENT_TIMEOUT = 2.0
_unreachable_until = 0.0
_lock = threading.Lock()


def enabled() -> bool:
    return bool((get_settings().loki_url or "").strip())


def push(event: str, fields: dict[str, Any], *, labels: dict[str, str] | None = None) -> bool:
    """推一条日志(同步, 调用方在 threadpool 里跑它)。返回是否已发出。

    连不上时 60 秒内不再试: Loki 挂了不该让每次反馈都多等一个超时。
    """
    global _unreachable_until
    url = (get_settings().loki_url or "").strip()
    if not url:
        return False
    with _lock:
        if _unreachable_until and time.time() < _unreachable_until:
            return False
    line = json.dumps({"event": event, **fields}, ensure_ascii=False, default=str)
    merged = {"app": "mxi", "domain": "askdata", "event": event}
    merged.update(labels or {})
    body = {"streams": [{"labels": merged,
                         "values": [[str(int(time.time() * 1e9)), line]]}]}
    try:
        import httpx

        resp = httpx.post(url, json=body, timeout=_CLIENT_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        return True
    except Exception as exc:  # noqa: BLE001 - 日志通道不可用不影响问数本身
        with _lock:
            _unreachable_until = time.time() + 60
        logger.debug("[askdata] Loki 推送失败(60s 内不再尝试): %s", exc)
        return False


def push_async(event: str, fields: dict[str, Any], *, labels: dict[str, str] | None = None) -> None:
    """后台线程推一条(不占调用方时延)。daemon 线程保证进程关停不被它拖住。"""
    if not enabled():
        return
    threading.Thread(target=push, args=(event, fields), kwargs={"labels": labels},
                     daemon=True).start()
