"""评测用例装载: 从 ``askdata_golden`` 读启用中的用例。

刻意只从库里读而不是从 JSON fixture 读: golden 的价值在于它与语义层同源 ——
补词、改口径、badcase 回流都会写进同一张表。评测与语义层各读一份文件, 早晚变成
"评测全绿但线上口径已经改了"的那种自欺。
"""

from __future__ import annotations

import json
from typing import Any

from app.askdata import store


def load_cases(*, limit: int | None = None, tag: str = "") -> list[dict[str, Any]]:
    """读启用中的 golden 用例(按标签过滤时是包含匹配, 逗号分词)。

    没有用例时抛错而不是返回空列表: 空集跑出来的 "EX=0%" 与 "EX=100%" 一样没意义,
    让人以为评测跑过了。
    """
    rows = store.list_golden(enabled_only=True, limit=2000)
    cases: list[dict[str, Any]] = []
    for row in rows:
        if tag and tag not in str(row.get("tags") or ""):
            continue
        expected = _as_dict(row.get("expected_result_json"))
        dsl = _as_dict(row.get("dsl_json"))
        if not expected or not dsl:
            continue  # 缺标准答案/缺 DSL 的行不参与 EX(它多半是待人工转正的草案)
        cases.append({
            "case_id": str(row.get("case_id") or ""),
            "question": str(row.get("question") or ""),
            "dsl": dsl,
            # 期望槽位可以没标(旧用例就是没标): slot-F1 的口径是"有标注的用例里算",
            # 拿没标的当 0 分会把"没标"误报成"模型抽错了"。
            "slots": _as_dict(row.get("slots_json")),
            "expected_sql": str(row.get("expected_sql") or ""),
            "expected": expected,
            "tags": str(row.get("tags") or ""),
            "difficulty": str(row.get("difficulty") or "easy"),
            "intent": str(row.get("intent") or ""),
            "note": str(row.get("note") or ""),
        })
    if limit:
        cases = cases[: max(1, int(limit))]
    if not cases:
        raise RuntimeError(
            "golden 评测集为空: 先跑 uv run python -m scripts.seed_askdata_golden "
            "(库里没有启用中的用例, 或它们的期望结果/DSL 缺失)"
        )
    return cases


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
