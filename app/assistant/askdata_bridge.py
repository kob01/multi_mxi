"""AskData_Agent 回复里的结构化载荷: 剥离、校验与转成网关 metadata。

契约(A2A 只传文本, 所以载荷走"正文 + 末尾 fenced JSON"的形式):

    <给人看的正文>

    ```askdata
    {"kind": "result|clarify|degraded|refuse", ...}
    ```

为什么不改 A2A 协议本身加 DataPart: 网关侧 ``a2a_client._extract_text`` 只取 TextPart,
多智能体并发分节合并也按纯文本工作 —— 换协议形状要同时动委派、合并、留档三条路径,
收益只是"少一个 fenced block"。这里改成**网关单点剥离**, 让下游所有既有逻辑继续
只看纯文本, 影响面最小。

正文本身永远自洽(澄清选项与结果表都在正文里), 所以旧版页面/CLI 用户看不到面板也能读懂。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

FENCE_NAME = "askdata"
_FENCE_RE = re.compile(
    rf"\n*(?:```|~~~)\s*{FENCE_NAME}\s*\n(?P<body>.*?)\n\s*(?:```|~~~)\s*$",
    re.S | re.I,
)
# 载荷里进 metadata 的字段: 刻意**不含 rows 与 sql**。
# 表格正文已经在 answer 里(前端 Markdown 渲染), 再把 50 行数据塞进每个 SSE 事件与
# 会话历史等于把同一份数据传两遍; SQL 是排障用的, 已经在 askdata_query_log 里留痕。
_VIEW_KEYS = (
    "kind", "query_id", "question", "route", "verdict", "range", "columns",
    "column_labels", "rowcount", "caliber_notes", "scope_note", "chart",
    "clarify", "metrics", "times", "dims", "reason", "lead", "allow_none",
    "error_codes", "attempts", "catalog_origin", "vector_channel", "trace_id",
)


def split_askdata_payload(text: str) -> tuple[str, dict[str, Any] | None]:
    """把智能体回复拆成 ``(干净正文, 载荷或 None)``。

    解析失败**不改变正文**: 原样返回并只留 WARNING。载荷是给面板用的加分项,
    把它失败变成"用户看不到答案"是拿加分换底线。
    """
    if not text or FENCE_NAME not in text:
        return text, None
    match = None
    for candidate in _FENCE_RE.finditer(text):
        match = candidate  # 取最后一个: 正文里可能引用过 ```askdata 说明文字
    if match is None:
        return text, None
    raw = match.group("body").strip()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        logger.warning("[askdata] 结构化载荷不是合法 JSON(只保留正文): %s", exc)
        return text, None
    if not isinstance(payload, dict) or not payload.get("kind"):
        logger.warning("[askdata] 载荷形状不符(缺 kind), 忽略面板: %s", type(payload).__name__)
        return text, None
    clean = (text[: match.start()] + text[match.end():]).strip()
    return clean or _fallback_text(payload), payload


def to_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """转成前端要的视图(裁掉重复大字段, 保留面板与反馈需要的全部标识)。"""
    view = {key: payload[key] for key in _VIEW_KEYS if key in payload}
    if "rows" in payload:
        view["rowcount"] = view.get("rowcount", len(payload.get("rows") or []))
    return view


def artifacts_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """图表产物 -> 网关 artifacts(前端已有的产物按钮位, 不新造渲染分支)。"""
    chart = payload.get("chart") or {}
    url = chart.get("url") or ""
    if not url:
        return []
    return [{
        "name": chart.get("name") or "",
        "url": url,
        "title": chart.get("title") or f"{chart.get('chart_type', 'chart')} 图表",
        "kind": "chart",
    }]


def slots_of(payload: dict[str, Any]) -> dict[str, Any]:
    """取下一轮要做确定性继承用的槽位(没有就空 dict, 空 dict 等于"不继承")。"""
    slots = payload.get("slots")
    return slots if isinstance(slots, dict) else {}


def _fallback_text(payload: dict[str, Any]) -> str:
    """正文被剥离成空时的兜底(极端情况: 智能体只回了载荷没回正文)。"""
    kind = payload.get("kind")
    if kind == "clarify":
        return payload.get("reason") or "请确认要统计的口径与区间。"
    return "问数智能体没有返回可读内容, 请稍后重试或换一种说法。"


# 供测试与实现核对: 正文里不该残留围栏标记。
_LEFTOVER_RE = re.compile(rf"```{FENCE_NAME}", re.I)
