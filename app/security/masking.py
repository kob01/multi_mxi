"""Sensitive-data masking for financial / HR payloads and logs."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ID card (18 or 15 digits)
    (re.compile(r"\b(\d{6})\d{8,9}(\d{3}[\dXx])\b"), r"\1********\2"),
    # Bank card (16-19 digits)
    (re.compile(r"\b(\d{4})\d{8,11}(\d{4})\b"), r"\1********\2"),
    # Mainland mobile
    (re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b"), r"\1****\2"),
]

_AMOUNT_KEYWORDS = ("salary", "薪酬", "工资", "amount")

# URL 段(计划验收第 6 项): 绝对链接与站内下载路径里的数字串(令牌/端口/IP)不是敏感数据,
# 却可能被上面的位数规则击中(比如全数字 hex 令牌恰好凑成 16 位) —— 打码会把 download_url
# 撕成坏链。所以先按 URL 切段, URL 原样保留, 只对段外文本打码。
_URL_SPAN = re.compile(r"(?:https?://|/api/)\S+")


def _mask_plain(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def mask_text(text: str) -> str:
    """Mask ID-card / bank-card / phone patterns; URL/下载链接段原样保留。"""
    if not text:
        return text
    out: list[str] = []
    pos = 0
    for span in _URL_SPAN.finditer(text):
        out.append(_mask_plain(text[pos : span.start()]))
        out.append(span.group(0))
        pos = span.end()
    out.append(_mask_plain(text[pos:]))
    return "".join(out)


def mask_sensitive(data: Any) -> Any:
    """Recursively mask sensitive values in a nested dict/list structure."""
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for k, v in data.items():
            if any(kw in k.lower() for kw in _AMOUNT_KEYWORDS) and isinstance(v, (int, float)):
                masked[k] = "***"
            else:
                masked[k] = mask_sensitive(v)
        return masked
    if isinstance(data, list):
        return [mask_sensitive(item) for item in data]
    if isinstance(data, str):
        return mask_text(data)
    return data


# ---------------------------------------------------------------------------
# 结果出口 DLP(层 5-C 最后一条): 列黑名单 + 行数限制, 防"把所有手机号列出来"这类渗出。
# 与上面的文本打码不同: 这里按**列名**整列处理, 因为 Text2SQL 的结果里手机号可能是
# 干净的 11 位数字串(没有上下文就命不中上面的正则)。
# ---------------------------------------------------------------------------
def dlp_columns() -> set[str]:
    """配置里的敏感列名集合(解析不出来 = 不打码, 但启动日志会告警)。"""
    from app.config import get_settings

    raw = (get_settings().dlp_mask_columns or "").lower()
    return {p.strip() for p in raw.split(",") if p.strip()}


def mask_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按列黑名单打码行数据(只改值, 不改列集合: 列消失了模型会自己编一个说辞)。"""
    cols = dlp_columns()
    if not cols:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        item = dict(row)
        for key in list(item.keys()):
            if key.lower() in cols and item[key] not in (None, ""):
                item[key] = "[已隐]"
        out.append(item)
    return out
