"""badcase 归因路由: 一条被点踩/改写的问数, 责任落在链路的哪一段, 该怎么处置。

为什么是规则而不是 LLM: 归因结论要驱动**写动作**(补词、改语义层、补评测集),
让模型判断"这错在链接还是错在 DSL"等于把改口径的权限交给一次概率输出。
留痕里本来就有确定证据(错误码、候选分数、verdict、route), 用它们做判定就够了。

五类归因与处置(刻意不是"所有 badcase 都进训练"):
- ``intent`` 槽位抽错(置信不足却被拿去算)      -> 待标注清单 + prompt 修订建议(只出报告)
- ``link``   链接错/歧义(候选分数接近或太低)     -> 补词草案(``sem_terms`` status=draft)
- ``dsl``    DSL 结构错(语法/兼容性/口径冲突)    -> 评测集补录草案(enabled=false)
- ``exec``   取数被拒或算不出(权限/成本/编译)    -> 运维待办(记 notes, 不动口径)
- ``render`` 数对但呈现错(图型/叙述)             -> 规则引擎待办(记 notes)

草案一律写成"未生效"状态: 归因只能**提出**修订, 生效必须过人 —— 否则点踩的人
与改口径的人可以是同一个念头。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DISPOSITION_TERM = "draft_term"          # 补词草案
DISPOSITION_GOLDEN = "draft_golden"      # 评测集补录草案
DISPOSITION_REVIEW = "manual_review"     # 只登记, 人来判
DISPOSITION_NONE = "none"

# 错误码 -> 归因类别(与 app/semantic/validate 的错误码同一份词表, 新增码要同步这里)。
_CODE_TO_CATEGORY = {
    "E_SCHEMA": "dsl",
    "E_UNKNOWN_METRIC": "link",
    "E_UNKNOWN_DIM": "link",
    "E_INCOMPATIBLE_DIM": "dsl",
    "E_TABLE_CONFLICT": "dsl",
    "E_CALIBER_CONFLICT": "dsl",
    "E_TIME_INVALID": "intent",
    "E_ENUM_INVALID": "link",
    "E_NO_PERMISSION": "exec",
    "E_ROWS_CAP": "exec",
    "E_COMPILE": "exec",
}


def attribute(row: dict[str, Any]) -> tuple[str, str]:
    """由留痕推出 ``(类别, 处置)``。

    判定顺序是**从最确定的证据到最弱的信号**: 有错误码就按码, 没码看 verdict,
    最后才看候选分数分布。反过来先猜"看着像链接错"会把 DSL 的结构性错误
    误归到补词上, 那才是真正让人找不到根因的漂移。
    """
    codes = _as_list(row.get("error_codes"))
    verdict = str(row.get("verdict") or "").lower()
    route = str(row.get("route") or "").lower()
    candidates = _as_list(row.get("candidates"))
    scores = [float(c.get("score") or 0) for c in candidates if isinstance(c, dict)]

    for code in codes:
        category = _CODE_TO_CATEGORY.get(str(code))
        if category:
            return category, _disposition(category, row)
    if verdict == "clarify":
        return "link", _disposition("link", row)
    if verdict in ("denied", "fail"):
        return "exec", _disposition("exec", row)
    if route in ("sandbox", "degrade"):
        return "exec", _disposition("exec", row)
    if len(scores) >= 2 and scores[0] - scores[1] < 0.1:
        # 前两名分差过小 = 系统自己也没把握, 却仍然算出了数: 这是链接段的过度自信。
        return "link", _disposition("link", row)
    if scores and scores[0] <= 0:
        return "intent", _disposition("intent", row)
    return "render", _disposition("render", row)


def _disposition(category: str, row: dict[str, Any]) -> str:
    if category == "link":
        return DISPOSITION_TERM
    if category == "dsl":
        return DISPOSITION_GOLDEN
    if category == "intent":
        return DISPOSITION_GOLDEN if row.get("feedback") == "rewritten" else DISPOSITION_REVIEW
    return DISPOSITION_REVIEW


def apply_disposition(
    row: dict[str, Any], *, category: str, disposition: str, corrected_dsl: Any = None
) -> dict[str, Any]:
    """把归因结论落成草案(或只登记), 全部标成"未生效"。"""
    result = {"category": category, "disposition": disposition, "written": ""}
    if disposition == DISPOSITION_TERM:
        term, target = _pick_term(row)
        if term and target and _draft_term(term, target):
            result["written"] = f"sem_terms:draft:{term}->{target}"
        return result
    if disposition == DISPOSITION_GOLDEN:
        dsl = corrected_dsl or row.get("dsl_json") or {}
        if isinstance(dsl, str):
            dsl = {"raw": dsl[:1500]}
        from app.askdata import store

        out = store.upsert_golden(
            question=str(row.get("question") or ""),
            dsl=dsl,
            expected_sql="",
            tags="badcase," + category,
            note=f"由 badcase 归因自动生成(类别={category}), 需人工核对口径后再启用",
            created_by=str(row.get("user_id") or "") or "askdata-badcase",
            enabled=False,
        )
        if out.get("status") == "ok":
            result["written"] = f"askdata_golden:draft:{out['case_id']}"
        return result
    return result


def _pick_term(row: dict[str, Any]) -> tuple[str, str]:
    """取出 ``(要补的说法, 目标指标 id)``: 说法用用户原话, 目标用当时最高分候选。"""
    question = str(row.get("question") or "").strip()
    candidates = _as_list(row.get("candidates"))
    top = next((c for c in candidates if isinstance(c, dict) and c.get("kind") == "metric"), None)
    if top is None:
        # 一个候选都没命中: 补词没有目标可以指向, 这种情形只能人来判。_draft_term 会拒。
        return (question[:64] if question else ""), ""
    return (question[:64] if question else ""), str(top.get("id") or "")


def _draft_term(term: str, target: str) -> bool:
    """写一条 ``sem_terms`` 草案(status=draft, 不参与召回), 成功返回 True。

    重复写入靠 (term, term_type, target_id) 唯一约束: 同一个说法被点踩十次也只
    留一条待办, 否则草案表会被重复噪声埋掉。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import sync as dbsync
    from app.db.models import TermAlias

    if not term or not target:
        return False
    stmt = pg_insert(TermAlias.__table__).values(
        term=term, term_type="metric", target_id=target, weight=0.6,
        source="badcase", status="draft",
    ).on_conflict_do_nothing(index_elements=["term", "term_type", "target_id"])
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(stmt)
    except Exception as exc:  # noqa: BLE001 - 草案写不进去只意味着这条 badcase 没有产出待办
        logger.warning("[askdata] 补词草案写入失败: %s", exc)
        return False
    # 草案是 draft 状态, 不会进召回集, 因此不需要 invalidate; 只有人工转正时才需要。
    return True


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    try:
        import json

        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
