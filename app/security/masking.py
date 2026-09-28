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
