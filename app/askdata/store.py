"""问数留痕 / 反馈 / 评测集 / 评测结论的同步 DAO。

同步而不是异步有两个理由, 都不是偏好:
1. askdata MCP server 的工具是同步函数(FastMCP 把它们放到 threadpool 里跑),
   留痕必须能在同一个调用栈里写完, 不能"异步工具才能留痕";
2. 网关 REST 复用同一份实现(照 ``app/dataops/router.py`` 的做法用
   ``run_in_threadpool`` 包一层), 于是不存在"两条路径写出两种形状"的风险。

所有写路径都是**尽力而为**: 留痕表写不进去只意味着看板少一条, 绝不能把已经算出来的
答案丢掉(那才是用户能感知的故障)。这一点在返回体里明说(``status=degraded``), 而不是
静默返空 —— 静默失败的观测面本身就是最需要被看见的失败。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.db import sync as dbsync

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

VERDICTS = ("ok", "clarify", "denied", "fail", "degraded")
ROUTES = ("metric_api", "compiler", "sandbox", "clarify", "repair", "degrade")
FEEDBACKS = ("up", "down", "rewritten")
ATTRIBUTIONS = ("intent", "link", "dsl", "exec", "render")

_INSERT = """
INSERT INTO askdata_query_log (
    query_id, trace_id, session_id, tenant_id, user_id, role, question,
    slots_json, candidates_json, dsl_json, route, final_sql, rowcount,
    latency_ms, token_cost, verdict, error_codes, notes, feedback, attribution
) VALUES (
    :query_id, :trace_id, :session_id, :tenant_id, :user_id, :role, :question,
    :slots, :candidates, :dsl, :route, :final_sql, :rowcount,
    :latency_ms, :token_cost, :verdict, :error_codes, :notes, '', ''
)
"""


def _json(value: Any) -> str:
    import json

    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, default=str)


def record_query(payload: dict[str, Any]) -> str:
    """写一条问数留痕, 返回 query_id; 写失败返回空串(调用方据此标降级)。"""
    query_id = uuid.uuid4().hex
    row = {
        "query_id": query_id,
        "trace_id": str(payload.get("trace_id") or "")[:64],
        "session_id": str(payload.get("session_id") or "")[:64],
        "tenant_id": str(payload.get("tenant_id") or "")[:32],
        "user_id": str(payload.get("user_id") or "")[:64],
        "role": str(payload.get("role") or "")[:16],
        "question": str(payload.get("question") or "")[:4000],
        "slots": _json(payload.get("slots") or {}),
        "candidates": _json(payload.get("candidates") or []),
        "dsl": _json(payload.get("dsl") or {}),
        "route": str(payload.get("route") or "")[:24],
        "final_sql": str(payload.get("final_sql") or "")[:4000],
        "rowcount": int(payload.get("rowcount") or 0),
        "latency_ms": int(payload.get("latency_ms") or 0),
        "token_cost": int(payload.get("token_cost") or 0),
        "verdict": str(payload.get("verdict") or "ok")[:16],
        "error_codes": _json(payload.get("error_codes") or []),
        "notes": str(payload.get("notes") or "")[:2000],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(text(_INSERT), row)
    except Exception as exc:  # noqa: BLE001 - 留痕失败不阻断回答, 但要把失败暴露出来
        logger.warning("[askdata] 问数留痕写入失败(本轮看板将缺这一条): %s", exc)
        return ""
    return query_id


def set_feedback(
    query_id: str, kind: str, *, attribution: str = "", corrected_dsl: Any = None,
    note: str = "", user_id: str = "",
) -> dict[str, Any]:
    """记录用户反馈(点赞/点踩/改写)并落归因类别。

    只允许改写**自己**问过的那条: 反馈本身是训练语料与语义层修订的输入, 让别人替你的
    提问下结论会让归因数据失去意义(与 ``list_my_dataops`` 同一口径)。
    """
    if kind not in FEEDBACKS:
        return {"error": f"feedback 只允许 {list(FEEDBACKS)}, 收到 {kind!r}"}
    sql = (
        "UPDATE askdata_query_log SET feedback = :kind, attribution = :attribution, "
        "notes = CASE WHEN :note = '' THEN notes ELSE notes || E'\\n' || :note END "
        "WHERE query_id = :query_id"
    )
    if user_id:
        sql += " AND user_id = :user_id"
    params = {
        "kind": kind, "attribution": (attribution or "")[:16], "note": (note or "")[:1000],
        "query_id": (query_id or "")[:64], "user_id": (user_id or "")[:64],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            result = conn.execute(text(sql), params)
            updated = int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 反馈写入失败: %s", exc)
        return {"error": f"反馈写入失败: {type(exc).__name__}"}
    if not updated:
        return {"error": "没找到这条问数记录(或不属于该用户)", "query_id": query_id}
    if corrected_dsl:
        _append_corrected_dsl(query_id, corrected_dsl)
    return {"status": "ok", "query_id": query_id, "feedback": kind,
            "attribution": attribution or "", "updated": updated}


def _append_corrected_dsl(query_id: str, corrected_dsl: Any) -> None:
    """把用户改写后的 DSL 追加到 notes(它是评测集补录与语义层修订的证据)。"""
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(
                text("UPDATE askdata_query_log SET notes = notes || E'\\n[改写 DSL] ' || :d "
                     "WHERE query_id = :id"),
                {"d": _json(corrected_dsl)[:1500], "id": query_id[:64]},
            )
    except Exception as exc:  # noqa: BLE001 - 证据追加失败不影响反馈本身已落库
        logger.warning("[askdata] 改写 DSL 追加失败: %s", exc)


def get_query(query_id: str) -> dict[str, Any]:
    with dbsync.get_sync_engine().connect() as conn:
        row = conn.execute(text("SELECT * FROM askdata_query_log WHERE query_id = :id"),
                           {"id": query_id[:64]}).first()
    return dict(row._mapping) if row else {}


def list_queries(
    *, user_id: str = "", verdict: str = "", feedback: str = "", days: int = 7, limit: int = 50,
) -> list[dict[str, Any]]:
    """回查问数记录(默认只看最近 7 天, 条数有界)。"""
    limit = max(1, min(200, int(limit or 50)))
    where = ["created_at >= :since"]
    params: dict[str, Any] = {"since": (datetime.now(_CST) - timedelta(days=max(1, days))).isoformat(),
                              "limit": limit}
    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = user_id
    if verdict:
        where.append("verdict = :verdict")
        params["verdict"] = verdict
    if feedback:
        where.append("feedback = :feedback")
        params["feedback"] = feedback
    sql = (
        f"SELECT query_id, trace_id, user_id, role, question, route, verdict, error_codes, "
        f"rowcount, latency_ms, feedback, attribution, notes, created_at "
        f"FROM askdata_query_log WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT :limit"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), params).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 问数记录回查失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def stats(*, days: int = 7) -> dict[str, Any]:
    """指标看板的一次性聚合: 通过率/澄清触发率/降级率/点踩率/P95 延迟 + 最近 EX。

    口径写在这里而不是让前端算: 分母是"进入状态机的问数轮次"(含被澄清与被拒的),
    只统计成功轮次的"一次通过率"毫无意义(它永远是 100%)。
    """
    since = (datetime.now(_CST) - timedelta(days=max(1, days))).isoformat()
    sql = """
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE verdict = 'ok' AND route IN ('metric_api', 'compiler', 'sandbox')) AS first_pass,
       COUNT(*) FILTER (WHERE verdict = 'clarify') AS clarified,
       COUNT(*) FILTER (WHERE route IN ('sandbox', 'degrade', 'repair')) AS degraded,
       COUNT(*) FILTER (WHERE verdict = 'denied' OR verdict = 'fail') AS failed,
       COUNT(*) FILTER (WHERE feedback = 'down') AS thumbs_down,
       COUNT(*) FILTER (WHERE feedback <> '') AS feedbacked,
       COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0) AS p95_ms
FROM askdata_query_log WHERE created_at >= :since
"""
    try:
        with dbsync.get_sync_engine().connect() as conn:
            row = conn.execute(text(sql), {"since": since}).first()
            latest = conn.execute(
                text("SELECT run_id, exec_accuracy, mode, case_count, started_at "
                     "FROM askdata_eval_run ORDER BY started_at DESC LIMIT 1")
            ).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 看板聚合失败: %s", exc)
        return {"error": f"看板聚合失败: {type(exc).__name__}", "window_days": days}
    data = dict(row._mapping) if row else {}
    total = int(data.get("total") or 0)

    def _rate(value: Any) -> float:
        return round(int(value or 0) * 100.0 / total, 1) if total else 0.0

    out = {
        "window_days": days,
        "total": total,
        "first_pass_rate": _rate(data.get("first_pass")),
        "clarify_rate": _rate(data.get("clarified")),
        "degrade_rate": _rate(data.get("degraded")),
        "fail_rate": _rate(data.get("failed")),
        "thumbs_down_rate": (
            round(int(data.get("thumbs_down") or 0) * 100.0 / int(data.get("feedbacked") or 1), 1)
            if int(data.get("feedbacked") or 0) else 0.0
        ),
        "feedback_rate": _rate(data.get("feedbacked")),
        "p95_latency_ms": int(data.get("p95_ms") or 0),
    }
    if latest:
        item = dict(latest._mapping)
        out["latest_eval"] = {
            "run_id": item.get("run_id"),
            "exec_accuracy": float(item.get("exec_accuracy") or 0),
            "mode": item.get("mode"),
            "case_count": int(item.get("case_count") or 0),
            "started_at": str(item.get("started_at") or ""),
        }
    return out


def record_eval_run(summary: dict[str, Any]) -> str:
    """落一条评测结论(由 ``scripts/askdata_eval`` 调用; 逐条明细在报告文件里)。"""
    run_id = str(summary.get("run_id") or uuid.uuid4().hex)[:64]
    sql = """
INSERT INTO askdata_eval_run (run_id, started_at, case_count, exec_accuracy, first_pass_rate,
    clarify_rate, degrade_rate, p95_latency_ms, mode, report_path, summary_json, created_by)
VALUES (:run_id, :started_at, :case_count, :exec_accuracy, :first_pass_rate, :clarify_rate,
    :degrade_rate, :p95_latency_ms, :mode, :report_path, :summary, :created_by)
ON CONFLICT (run_id) DO UPDATE SET
    case_count = EXCLUDED.case_count, exec_accuracy = EXCLUDED.exec_accuracy,
    first_pass_rate = EXCLUDED.first_pass_rate, clarify_rate = EXCLUDED.clarify_rate,
    degrade_rate = EXCLUDED.degrade_rate, p95_latency_ms = EXCLUDED.p95_latency_ms,
    mode = EXCLUDED.mode, report_path = EXCLUDED.report_path,
    summary_json = EXCLUDED.summary_json
"""
    row = {
        "run_id": run_id,
        "started_at": summary.get("started_at") or datetime.now(_CST),
        "case_count": int(summary.get("case_count") or 0),
        "exec_accuracy": float(summary.get("exec_accuracy") or 0),
        "first_pass_rate": float(summary.get("first_pass_rate") or 0),
        "clarify_rate": float(summary.get("clarify_rate") or 0),
        "degrade_rate": float(summary.get("degrade_rate") or 0),
        "p95_latency_ms": int(summary.get("p95_latency_ms") or 0),
        "mode": str(summary.get("mode") or "full")[:32],
        "report_path": str(summary.get("report_path") or "")[:512],
        "summary": _json(summary.get("detail") or {}),
        "created_by": str(summary.get("created_by") or "")[:64],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(text(sql), row)
    except Exception as exc:  # noqa: BLE001 - 评测结论落库失败不毁掉已生成的报告
        logger.warning("[askdata] 评测结论落库失败(报告文件仍在): %s", exc)
        return ""
    return run_id


# ---------------------------------------------------------------------------
# golden 评测集
# ---------------------------------------------------------------------------
def list_golden(*, enabled_only: bool = True, limit: int = 500) -> list[dict[str, Any]]:
    where = "WHERE enabled = true" if enabled_only else ""
    sql = (
        "SELECT case_id, question, dsl_json, expected_sql, expected_result_json, tags, note, "
        f"enabled, created_by, created_at FROM askdata_golden {where} "
        "ORDER BY created_at DESC LIMIT :limit"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {"limit": max(1, min(2000, int(limit)))}).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 评测集读取失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def upsert_golden(
    *, question: str, dsl: Any = None, expected_sql: str = "", expected_result: Any = None,
    tags: str = "", note: str = "", created_by: str = "", enabled: bool = True,
    case_id: str = "",
) -> dict[str, Any]:
    """新增/更新一条 golden 用例(反馈改写来的草案默认 ``enabled=false``, 等人工转正)。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.models import AskDataGolden

    values = {
        "case_id": case_id or uuid.uuid4().hex[:32],
        "question": (question or "")[:4000],
        "dsl_json": dsl or {},
        "expected_sql": (expected_sql or "")[:4000],
        "expected_result_json": expected_result or {},
        "tags": (tags or "")[:128],
        "note": (note or "")[:2000],
        "created_by": (created_by or "")[:64],
        "enabled": bool(enabled),
    }
    stmt = (
        pg_insert(AskDataGolden.__table__).values(**values)
        .on_conflict_do_update(
            index_elements=["case_id"],
            set_={k: values[k] for k in ("question", "dsl_json", "expected_sql",
                                        "expected_result_json", "tags", "note", "enabled")},
        )
    )
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 评测集写入失败: %s", exc)
        return {"error": f"评测集写入失败: {type(exc).__name__}"}
    return {"status": "ok", "case_id": values["case_id"], "enabled": values["enabled"]}


def golden_count() -> int:
    try:
        with dbsync.get_sync_engine().connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM askdata_golden WHERE enabled")).scalar_one() or 0)
    except Exception:  # noqa: BLE001
        return 0
