"""智能问数 MCP Server (FastMCP, streamable-http): 语义层 + 指标 API + DSL 编译 + 沙箱取数。

它是 AskData_Agent 唯一的"手": 智能体进程不持数据库凭据(层 0), 所有取数都经过这里的
工具。与 analytics MCP 的分工要说清, 否则会出现两条都能查数、谁都不负责口径的局面:

- **analytics(Analyst_Agent)**: 自由 Text2SQL —— 模型写 SELECT, 服务端只做拦截;
  适合"口径本身没登记过"的探索式分析与成文报告。
- **askdata(AskData_Agent)**: NL2DSL —— 模型只能选语义层登记过的指标与维度, SQL 由
  服务端模板编译; 适合"要拿准数"的问数, 同环比/占比一律交给引擎算。

治理出口与 analytics 同一口径, 不复写第二份: 作用域解析(默认拒) -> AST 校验 ->
只读角色 + 会话作用域事务 -> EXPLAIN 扫描量上限 -> 出口 DLP 打码 -> spotlight 数据标记
-> 结构化审计(sql_audit_records 与 audit.jsonl 双写)。

Run:
    python -m app.mcp_servers.askdata_server   # serves http://0.0.0.0:8007/mcp
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import SQLAlchemyError

from app.analytics import charts, store
from app.config import get_settings
from app.db import dataops
from app.db import sync as dbsync
from app.db.ast_guard import ASTGuardError, READ_MODE, validate_sql
from app.db.rls import read_role
from app.db.scope import DataScope, ScopeError, resolve_scope_sync
from app.security import masking, spotlight
from app.semantic import compiler as dsl_compiler
from app.semantic import link as semantic_link
from app.semantic import registry
from app.semantic.dsl import DslError, QuerySpec, parse_dsl
from app.semantic.validate import CallerScope, validate

logger = logging.getLogger(__name__)

mcp = FastMCP("enterprise-askdata-query", host="0.0.0.0", port=8007)

# 表白名单沿用 analytics 域那一份(单一事实源: app/analytics/reports.py)。
_ALLOWED_TABLES: set[str] | None = None


def _allowed_tables() -> set[str]:
    global _ALLOWED_TABLES
    if _ALLOWED_TABLES is None:
        from app.analytics.reports import ALLOWED_TABLES

        _ALLOWED_TABLES = set(ALLOWED_TABLES)
    return _ALLOWED_TABLES


def _scope_of_caller(caller_user_id: str, caller_role: str) -> tuple[DataScope | None, dict | None]:
    """把网关注入的身份换成数据作用域; 解不出时返回一个拒用错误体(默认拒)。"""
    try:
        return resolve_scope_sync(caller_user_id, caller_role), None
    except ScopeError as exc:
        return None, {"error": f"无法确定数据作用域: {exc}", "forbidden": True}
    except Exception as exc:  # noqa: BLE001 - 主数据查不到同样是"解不出作用域"
        logger.warning("[askdata] 作用域解析异常(caller=%s): %s", caller_user_id, exc)
        return None, {"error": "无法确定数据作用域(员工主数据不可用)", "forbidden": True}


def _spec_of(dsl_json: dict | str) -> tuple[QuerySpec | None, dict | None]:
    """把模型交上来的 DSL 解析成强类型对象(语法错在这里就拦掉)。"""
    try:
        return parse_dsl(dsl_json), None
    except DslError as exc:
        return None, {"error": f"DSL 结构不合法: {exc}", "error_codes": ["E_SCHEMA"]}


def _audit(
    *, trace_id: str, scope: DataScope | None, user_id: str, role: str,
    nl: str, generated: str, final_sql: str, decision: str, reason: str = "",
    cost: dict | None = None,
) -> None:
    """问数的每一次取数都留结构化审计(被拒的问法同样是信号, 不能只记成功)。"""
    try:
        dataops.record_audit(
            trace_id=trace_id or "", scope=scope, user_id=user_id, role=role,
            nl_question=nl, generated=generated, final_sql=final_sql,
            decision=decision, reason=reason, cost=cost,
        )
    except Exception as exc:  # noqa: BLE001 - 留痕不能阻断取数
        logger.warning("[askdata] 审计写入失败(忽略): %s", exc)


def _execute(compiled: dsl_compiler.Compiled, scope: DataScope, *, nl: str, trace_id: str) -> dict[str, Any]:
    """执行编译产物: 只读角色 + 会话作用域 + 扫描量上限 + 出口治理。"""
    settings = get_settings()
    try:
        payload, scan_rows = dbsync.execute_scoped_select(
            compiled.sql,
            compiled.params,
            db_role=read_role(),
            session_settings=scope.session_settings(),
            max_scan_rows=settings.sqlguard_max_scan_rows,
            timeout_ms=settings.askdata_statement_timeout_ms,
        )
    except dbsync.ScanCostExceeded as exc:
        _audit(trace_id=trace_id, scope=scope, user_id=scope.user_id, role=scope.role,
               nl=nl, generated=compiled.route, final_sql=compiled.sql,
               decision="deny", reason=str(exc))
        return {"error": f"扫描量超限被拒: {exc}", "error_codes": ["E_ROWS_CAP"], "sql": compiled.sql}
    except SQLAlchemyError as exc:
        logger.warning("[askdata] 编译 SQL 执行失败: %s", exc)
        _audit(trace_id=trace_id, scope=scope, user_id=scope.user_id, role=scope.role,
               nl=nl, generated=compiled.route, final_sql=compiled.sql,
               decision="deny", reason=f"执行失败 {type(exc).__name__}")
        return {"error": f"取数执行失败: {type(exc).__name__}: {str(exc)[:200]}", "sql": compiled.sql}

    block = payload[0] if payload else {"columns": [], "rows": [], "rowcount": 0}
    block["rows"] = masking.mask_rows(block.get("rows") or [])
    marked = spotlight.mark_payload([block], scope_note=scope.note())
    suspicious = spotlight.find_suspicious(marked)
    _audit(
        trace_id=trace_id, scope=scope, user_id=scope.user_id, role=scope.role,
        nl=nl, generated=compiled.route, final_sql=compiled.sql,
        decision="allow", reason=f"返回 {block.get('rowcount', 0)} 行(route={compiled.route})",
        cost={"estimated_scan_rows": scan_rows, "params_keys": sorted(compiled.params)},
    )
    out: dict[str, Any] = {
        **marked[0],
        "route": compiled.route,
        "sql": compiled.sql,
        "columns": compiled.columns,
        "column_labels": compiled.labels,
        "caliber_notes": compiled.caliber_notes,
        "scope_note": scope.note(),
    }
    if suspicious and get_settings().dataops_suspicious_data_guard:
        out[spotlight.SUSPICIOUS_KEY] = suspicious
    return out


# ---------------------------------------------------------------------------
# 语义层读取与链接
# ---------------------------------------------------------------------------
@mcp.tool()
def list_metrics(
    domain: str = "all", caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """列出语义层登记的指标与维度(**元数据, 不含任何业务数据**)。

    问数之前先看这里: 只有列出来的指标才查得到, 列不出来的口径不存在。
    ``origin`` 说明这份快照来自数据库还是代码内置兜底集。

    Args:
        domain: all / expense / budget / hr / leave / purchase / contract / supplier / employee。
        caller_user_id: 网关注入的调用者工号(用于回显可见范围)。
        caller_role: 网关注入的调用者角色。
    """
    catalog = registry.get_catalog()
    metrics = catalog.metrics_in_domain(domain)
    if not metrics:
        return {
            "origin": catalog.origin,
            "domain": domain,
            "metrics": [],
            "note": f"域 {domain} 没有登记指标; 可用域: {catalog.domains()}",
            "dimensions": catalog.render_dimensions(),
        }
    return {
        "origin": catalog.origin,
        "domain": domain,
        "metrics": [
            {
                "id": m.metric_id, "label": m.label, "unit": m.unit, "domain": m.domain,
                "caliber": m.caliber_note(), "compatible_dims": list(m.compatible_dims),
                "calendar": m.calendar,
            }
            for m in metrics
        ],
        "dimensions": catalog.render_dimensions(),
    }


@mcp.tool()
def link_terms(
    text: str, caller_user_id: str = "", caller_role: str = "", trace_id: str = ""
) -> dict[str, Any]:
    """把用户原话链接到语义层对象(三路召回: 词典精确 / 向量相似 / 枚举值反查)。

    返回按分数降序的候选, 供智能体决定"直接选用"还是"发起选项式澄清"。
    **本工具不替用户拍板**: 分数不够用时去澄清, 不要挑一个看起来差不多的口径硬算。

    Args:
        text: 用户原话(或已消解指代的问题)。
        caller_user_id/caller_role: 网关注入的身份, 决定枚举反查能看到哪些部门/供应商。
        trace_id: 链路号。
    """
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    catalog = registry.get_catalog()
    result = semantic_link.link(
        text, catalog=catalog, scope=scope,
        top_n=max(4, get_settings().askdata_clarify_max_options * 2),
    )
    payload: dict[str, Any] = {
        "query": text,
        "candidates": result.to_payload(),
        "enum_filters": result.enum_filters,
        "catalog_origin": catalog.origin,
        "vector_channel": "ok" if result.vector_ok else "unavailable",
    }
    if denial is not None:
        payload.update(denial)
        return payload
    payload["scope_note"] = scope.note()
    _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
           nl=text, generated="link_terms", final_sql="", decision="allow",
           reason=f"候选 {len(result.candidates)} 条, 向量通道{'可用' if result.vector_ok else '不可用'}")
    return payload


@mcp.tool()
def validate_dsl(
    dsl_json: dict[str, Any] | str,
    caller_user_id: str = "",
    caller_role: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """对 DSL 跑语义级 + 权限级校验(语法级在智能体本地已经跑过一遍)。

    返回 ``{ok, errors:[{code, message, field}], notices:[...]}``。错误码是确定的
    (E_UNKNOWN_METRIC / E_INCOMPATIBLE_DIM / E_TIME_INVALID / E_ENUM_INVALID /
    E_TABLE_CONFLICT / E_CALIBER_CONFLICT / E_NO_PERMISSION / E_ROWS_CAP),
    依据 code + message 修正 DSL 后重试一次; 修不出来就如实告诉用户不能算, 不要硬编。

    Args:
        dsl_json: 强类型 DSL(dict 或 JSON 字符串)。
        caller_user_id/caller_role: 网关注入的身份(权限级校验看它, 不看 DSL 里的任何字段)。
        trace_id: 链路号。
    """
    spec, syntax_denial = _spec_of(dsl_json)
    if syntax_denial is not None:
        return {"ok": False, **syntax_denial}
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    if denial is not None:
        return denial
    catalog = registry.get_catalog()
    check = validate(
        spec, catalog=catalog, scope=CallerScope.from_scope(scope),
        max_time_span_days=get_settings().askdata_max_time_span_days,
        max_rows=get_settings().askdata_max_rows,
    )
    _audit(
        trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
        nl="", generated=spec.to_payload().__str__()[:500], final_sql="",
        decision="allow" if check.ok else "deny",
        reason=check.as_text() or "校验通过",
    )
    return {
        "ok": check.ok,
        "errors": [e.to_dict() for e in check.errors],
        "notices": [n.to_dict() for n in check.notices],
        "resolved_range": check.resolved.to_payload() if check.resolved else {},
        "metrics": [{"id": m.metric_id, "label": m.label, "caliber": m.caliber_note()} for m in check.metrics],
    }


# ---------------------------------------------------------------------------
# 降级链: 指标 API -> DSL 编译 -> 沙箱 SQL
# ---------------------------------------------------------------------------
@mcp.tool()
def query_metric_api(
    dsl_json: dict[str, Any] | str,
    caller_user_id: str = "",
    caller_role: str = "",
    trace_id: str = "",
    nl_question: str = "",
) -> dict[str, Any]:
    """降级链第 1 级: 按语义层固定口径取数(首选, 口径确定性最高)。

    只接受"不含自定义过滤条件、不做同环比、单指标"的 DSL —— 这条通道的 SQL 完全由
    语义层定义决定, 模型没有施加任何谓词。不接受时返回 ``{error, next: "compile_dsl"}``,
    请改调 :func:`compile_dsl`。

    Args:
        dsl_json: 强类型 DSL。
        caller_user_id/caller_role: 网关注入的身份。
        trace_id: 链路号。
        nl_question: 本轮用户原句(进审计, 便于回答"这个数是谁问出来的")。
    """
    return _run_route(
        "metric_api", dsl_json, caller_user_id=caller_user_id, caller_role=caller_role,
        trace_id=trace_id, nl_question=nl_question,
    )


@mcp.tool()
def compile_dsl(
    dsl_json: dict[str, Any] | str,
    caller_user_id: str = "",
    caller_role: str = "",
    trace_id: str = "",
    nl_question: str = "",
) -> dict[str, Any]:
    """降级链第 2 级: 通用编译器把 DSL 编成 SQL 并执行(多维下钻 + 过滤 + 同环比)。

    同环比走两条同形子查询做对比, 变化率由 SQL 算 —— 不要自己把两个数相减除一下。
    返回体带 ``sql`` 与 ``caliber_notes``, 回答里必须把口径与统计范围原样转达。

    Args:
        dsl_json: 强类型 DSL。
        caller_user_id/caller_role: 网关注入的身份。
        trace_id: 链路号。
        nl_question: 本轮用户原句。
    """
    return _run_route(
        "compiler", dsl_json, caller_user_id=caller_user_id, caller_role=caller_role,
        trace_id=trace_id, nl_question=nl_question,
    )


def _run_route(
    route: str,
    dsl_json: Any,
    *,
    caller_user_id: str,
    caller_role: str,
    trace_id: str,
    nl_question: str,
) -> dict[str, Any]:
    """两条编译通道的共同执行体(校验 -> 编译 -> 执行 -> 审计), 不各写一份。"""
    spec, syntax_denial = _spec_of(dsl_json)
    if syntax_denial is not None:
        return syntax_denial
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    if denial is not None:
        return denial
    catalog = registry.get_catalog()
    settings = get_settings()
    check = validate(
        spec, catalog=catalog, scope=CallerScope.from_scope(scope),
        max_time_span_days=settings.askdata_max_time_span_days,
        max_rows=settings.askdata_max_rows,
    )
    if not check.ok:
        return {
            "error": check.as_text(),
            "error_codes": check.codes,
            "next": "revise_dsl_or_clarify",
        }
    try:
        if route == "metric_api":
            compiled = dsl_compiler.compile_metric_api(
                spec, catalog=catalog, scope=scope, check=check, max_rows=settings.askdata_max_rows,
            )
        else:
            compiled = dsl_compiler.compile_analytical(
                spec, catalog=catalog, scope=scope, check=check, max_rows=settings.askdata_max_rows,
            )
    except dsl_compiler.CompileError as exc:
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=str(dsl_json)[:500], final_sql="",
               decision="deny", reason=f"编译失败({route}): {exc}")
        fallback = "compile_dsl" if route == "metric_api" else "exec_sql_sandbox"
        return {"error": f"{route} 通道编不出来: {exc}", "error_codes": ["E_COMPILE"], "next": fallback}
    return _execute(compiled, scope, nl=nl_question, trace_id=trace_id)


@mcp.tool()
def exec_sql_sandbox(
    sql: str, caller_user_id: str = "", caller_role: str = "", trace_id: str = "",
    nl_question: str = "",
) -> dict[str, Any]:
    """降级链第 3 级(最后一级): 在只读沙箱里执行一条你自写的 SELECT。

    只有指标 API 与编译器都覆盖不了时才用(例如语义层还没登记这个口径)。
    沙箱不是许可: 单条 SELECT、只碰白名单表、行数与扫描量有上限、能读到哪些行由
    行级安全决定; 校验被拒时**不要改写成语义层外的表或去掉 WHERE 再试**, 如实说明
    这个口径还没被治理过。

    Args:
        sql: 一条只读 PostgreSQL SELECT(或 WITH ... SELECT)。
        caller_user_id/caller_role: 网关注入的身份。
        trace_id: 链路号。
        nl_question: 本轮用户原句。
    """
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    if denial is not None:
        _audit(trace_id=trace_id, scope=None, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=sql, final_sql=sql, decision="deny",
               reason=denial.get("error", ""))
        return denial
    settings = get_settings()
    try:
        safe_sql = validate_sql(
            sql, allowed_tables=_allowed_tables(), mode=READ_MODE,
            max_rows=settings.askdata_max_rows,
        )
    except ASTGuardError as exc:
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=sql, final_sql=sql, decision="deny",
               reason=f"AST 拒: {exc}")
        return {"error": f"SQL 校验失败(AST 拒): {exc}", "sql": sql}
    try:
        payload, scan_rows = dbsync.execute_scoped_select(
            safe_sql, None, db_role=read_role(),
            session_settings=scope.session_settings(),
            max_scan_rows=settings.sqlguard_max_scan_rows,
            timeout_ms=settings.askdata_statement_timeout_ms,
        )
    except dbsync.ScanCostExceeded as exc:
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=sql, final_sql=safe_sql, decision="deny",
               reason=str(exc))
        return {"error": f"扫描量超限被拒: {exc}", "sql": safe_sql}
    except SQLAlchemyError as exc:
        return {"error": f"SQL 执行失败: {type(exc).__name__}: {str(exc)[:200]}", "sql": safe_sql}
    block = payload[0] if payload else {"columns": [], "rows": [], "rowcount": 0}
    block["rows"] = masking.mask_rows(block.get("rows") or [])
    marked = spotlight.mark_payload([block], scope_note=scope.note())
    _audit(
        trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
        nl=nl_question, generated=sql, final_sql=safe_sql, decision="allow",
        reason=f"沙箱取数 {block.get('rowcount', 0)} 行",
        cost={"estimated_scan_rows": scan_rows},
    )
    return {**marked[0], "route": "sandbox", "sql": safe_sql, "scope_note": scope.note()}


# ---------------------------------------------------------------------------
# 可视化与留痕
# ---------------------------------------------------------------------------
@mcp.tool()
def render_chart(
    title: str,
    chart_type: str = "bar",
    categories: list[Any] | None = None,
    series: list[dict[str, Any]] | None = None,
    values: list[Any] | None = None,
    caller_user_id: str = "",
) -> dict[str, Any]:
    """把问数结果画成图并落盘, 返回可展示的 URL(图型由调用方按结果 shape 决定)。

    chart_type: line(时间序列) / pie(占比) / bar(类目对比)。
    与 Analyst_Agent 的 render_chart 同一套渲染器与台账, 只是调用方不同。

    Args:
        title: 图表标题(同时用于产物文件名)。
        chart_type: bar / line / pie。
        categories: 类目标签。
        series: 多系列数据 [{name, data}]。
        values: 单系列数值。
        caller_user_id: 网关注入的调用者工号(写进产物台账)。
    """
    payload = charts.render(chart_type, title, categories or [], series, values)
    if "error" in payload:
        return {"error": payload["error"]}
    name = store.stamp("askdata", ".svg", title)
    written = store.write_text(
        name, payload["svg"], created_by=caller_user_id or "", title=title,
        params={"chart_type": payload["chart_type"], "categories": (categories or [])[:50],
                "series_names": payload["series_names"]},
    )
    if "error" in written:
        return written
    out: dict[str, Any] = {
        **written,
        "chart_type": payload["chart_type"],
        "categories_count": payload["categories_count"],
        "markdown": f"![{title}]({written['url']})",
    }
    try:
        png = charts.render_png(chart_type, title, categories or [], series, values)
        if "png" in png:
            png_written = store.write_bytes(
                store.stamp("askdata", ".png", title), png["png"], title=title,
                params={"chart_type": payload["chart_type"]},
            )
            if "url" in png_written:
                out["png_url"] = png_written["url"]
    except Exception as exc:  # noqa: BLE001 - PNG 只是"能进 office"的加分项
        logger.debug("[askdata] png 生成跳过: %s", exc)
    return out


@mcp.tool()
def log_query(
    *,
    trace_id: str = "",
    question: str = "",
    slots_json: dict[str, Any] | str | None = None,
    candidates_json: list[dict[str, Any]] | None = None,
    dsl_json: dict[str, Any] | str | None = None,
    route: str = "",
    final_sql: str = "",
    rowcount: int = 0,
    latency_ms: int = 0,
    verdict: str = "ok",
    error_codes: list[str] | None = None,
    notes: str = "",
    session_id: str = "",
    caller_user_id: str = "",
    caller_role: str = "",
) -> dict[str, Any]:
    """写一条问数全链路留痕(看板指标与 badcase 归因的原料)。

    状态机每轮结束都要调它, 包括**被澄清拦住和被拒的那一轮** —— 只记成功的链路
    算不出一次通过率, 也归不出因。

    Args:
        trace_id: 链路号。
        question: 用户原句。
        slots_json/candidates_json/dsl_json: 各阶段中间产物(原样存, 便于回放)。
        route: metric_api / compiler / sandbox / clarify / repair / degrade。
        final_sql: 真正执行的 SQL(未执行则留空)。
        rowcount/latency_ms: 结果规模与耗时。
        verdict: ok / clarify / denied / fail / degraded。
        error_codes: 校验错误码列表。
        notes: 口径与降级说明。
        session_id: 会话号(供多轮槽位继承回查)。
        caller_user_id/caller_role: 网关注入的身份。
    """
    from app.askdata import store as logstore

    payload = {
        "trace_id": trace_id, "question": question,
        "slots": _as_dict(slots_json), "candidates": candidates_json or [],
        "dsl": _as_dict(dsl_json), "route": route, "final_sql": final_sql,
        "rowcount": int(rowcount or 0), "latency_ms": int(latency_ms or 0),
        "verdict": verdict, "error_codes": error_codes or [], "notes": notes,
        "session_id": session_id, "user_id": caller_user_id, "role": caller_role,
    }
    query_id = logstore.record_query(payload)
    if not query_id:
        # 留痕失败不能伪装成成功: 归因与看板会因此缺一块, 至少要把它带回给模型与日志。
        return {"status": "degraded", "query_id": "", "note": "问数留痕未能落库(看板将缺这一条)"}
    return {"status": "ok", "query_id": query_id}


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {"raw": str(value)[:2000]}
    return parsed if isinstance(parsed, dict) else {"raw": str(parsed)[:2000]}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
