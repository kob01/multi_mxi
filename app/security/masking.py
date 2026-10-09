"""Sensitive-data masking for financial / HR payloads and logs."""

from __future__ import annotations

import re
from collections.abc import Sequence
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
# 可逆 PII 脱敏往返(合同初审送 LLM 前的净化, 出口还原)。
# 与上面的单向打码不同: 这里用**稳定占位符**替换敏感串并保留还原映射, 使金额/
# 账号/证件/电话不进 LLM 上下文与日志, 而模型仍能在占位符原位做条款定位与语义
# 判断(引用到的就是占位符, 出口再还原回原句)。纯正则实现, 不引 NER 模型 ——
# 与仓内轻依赖约定一致; 识别不了的"核心技术参数"由调用方经 extra_patterns 传入。
#
# 关键口径: 规则红线判定与原文定位必须用**未脱敏原文**(金额阈值、账号一致性只有
# 看真实值才判得对), 只有送 LLM 的那一份走本函数; 见 contract_agent 工作流的
# original_text / masked_text 双轨。
# ---------------------------------------------------------------------------
_PII_RULES: list[tuple[str, re.Pattern[str]]] = [
    # 顺序即优先级: 先吃 18 位身份证(含末位 X), 再吃 16-19 位银行/收款账号,
    # 然后手机号与中文金额串; 前面替换成占位符后, 后面的正则不会再命中([] 定界)。
    ("ID", re.compile(r"\b\d{17}[\dXx]\b")),
    ("ACCOUNT", re.compile(r"\b\d{16,19}\b")),
    ("PHONE", re.compile(r"\b1[3-9]\d{9}\b")),
    ("AMOUNT", re.compile(r"(?:人民币|¥|￥)?\s*\d[\d,]*(?:\.\d+)?\s*(?:万元|亿|元)")),
]


def mask_round_trip(
    text: str,
    *,
    extra_patterns: Sequence[tuple[str, re.Pattern[str]]] | None = None,
) -> tuple[str, dict[str, str]]:
    """把敏感串替换为稳定占位符, 返回 ``(masked_text, restore_map)``。

    - 同一原值映射到同一占位符(合同里重复出现的同一金额可一致还原、且模型看得出是同一值);
    - URL/下载链接段原样保留(与 :func:`mask_text` 同口径, 打码会撕坏链接);
    - ``restore_map`` 为 ``{占位符: 原值}``, 交给 :func:`restore` 出口还原。
    """
    if not text:
        return text, {}
    rules = list(_PII_RULES)
    if extra_patterns:
        rules = rules + list(extra_patterns)
    fwd: dict[str, str] = {}          # 原值 -> 占位符
    restore: dict[str, str] = {}       # 占位符 -> 原值
    counters: dict[str, int] = {}

    def _mask_segment(segment: str) -> str:
        for label, pattern in rules:
            def _repl(match: re.Match[str], label: str = label) -> str:
                raw = match.group(0)
                ph = fwd.get(raw)
                if ph is None:
                    counters[label] = counters.get(label, 0) + 1
                    ph = f"[{label}_{counters[label]}]"
                    fwd[raw] = ph
                    restore[ph] = raw
                return ph

            segment = pattern.sub(_repl, segment)
        return segment

    out: list[str] = []
    pos = 0
    for span in _URL_SPAN.finditer(text):
        out.append(_mask_segment(text[pos : span.start()]))
        out.append(span.group(0))
        pos = span.end()
    out.append(_mask_segment(text[pos:]))
    return "".join(out), restore


def restore(text: str, restore_map: dict[str, str]) -> str:
    """把 :func:`mask_round_trip` 产出的占位符还原回原值。

    按占位符长度降序替换, 避免 ``[AMOUNT_1]`` 被 ``[AMOUNT_10]`` 之类前缀误伤
    (``[]`` 定界本已足够, 降序只是稳妥冗余)。
    """
    if not text or not restore_map:
        return text
    for ph in sorted(restore_map, key=len, reverse=True):
        text = text.replace(ph, restore_map[ph])
    return text


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
