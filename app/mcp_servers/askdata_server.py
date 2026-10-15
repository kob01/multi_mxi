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
import re
import threading
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.analytics import charts, store
from app.config import get_settings
from app.db import dataops
from app.db import sync as dbsync
from app.db.analytics_views import SANDBOX_VIEW_SPECS, sandbox_view_names
from app.db.ast_guard import ASTGuardError, READ_MODE, validate_sql
from app.db.rls import read_role
from app.db.scope import DataScope, ScopeError, resolve_scope_sync
from app.security import masking, spotlight
from app.semantic import breaker
from app.semantic import compiler as dsl_compiler
from app.semantic import link as semantic_link
from app.semantic import registry, resultcache
from app.semantic.dsl import DslError, QuerySpec, parse_dsl
from app.semantic.validate import CallerScope, validate

logger = logging.getLogger(__name__)

mcp = FastMCP("enterprise-askdata-query", host="0.0.0.0", port=8007)

# golden 快照缓存(few-shot 用): 评测集是元数据而不是业务数据, 可以进程级共享。
_GOLDEN_LOCK = threading.Lock()
_GOLDEN: dict[str, Any] = {"rows": [], "at": 0.0}
_GOLDEN_TTL = 120.0

# 表白名单沿用 analytics 域那一份(单一事实源: app/analytics/reports.py)。
_ALLOWED_TABLES: set[str] | None = None


def _allowed_tables() -> set[str]:
    global _ALLOWED_TABLES
    if _ALLOWED_TABLES is None:
        from app.analytics.reports import ALLOWED_TABLES

        _ALLOWED_TABLES = set(ALLOWED_TABLES)
    return _ALLOWED_TABLES


def _sandbox_allowed_tables() -> set[str]:
    """沙箱可查面: 默认只给治理视图, 配置退回时才给基表白名单。

    与编译器用不同两个面是刻意的: 编译器产物引用基表(口径由服务端模板与列策略管),
    而模型自由 SQL 只能碰"已经把机密列拿掉"的那一份投影。用 ``use_views=false`` 退回
    基表面时, 调用方会连带打开星号投影拒(因为那时列黑名单靠点名才生效)。
    """
    if not get_settings().askdata_sandbox_use_views:
        return _allowed_tables()
    return sandbox_view_names()


def _sandbox_denied_columns(role: str) -> set[str]:
    """把语义层 col_policies 汇成沙箱 AST 列黑名单(取不到就空集, 不拿它拦人)。

    为什么按基表并集而不逐视图对账: 宁可多拒一个同名列, 也不能漏放一个机密列。
    列策略取不到(语义层降级)时选"不施加列黑名单"而不是"拒所有": 视图已经把列拿掉了,
    这一层只是可移植的兜底, 它挂了不该把能答的问题卡在门外。
    """
    settings = get_settings()
    if not settings.askdata_sandbox_enforce_col_policies:
        return set()
    try:
        catalog = registry.get_catalog()
    except Exception as exc:  # noqa: BLE001 - 兜底层不可用不能反过来误杀
        logger.warning("[askdata] 列策略读不到(本轮沙箱不施加列黑名单): %s", exc)
        return set()
    denied: set[str] = set()
    for spec in SANDBOX_VIEW_SPECS:
        denied |= set(catalog.denied_columns_for(role, spec.source_table))
    return {str(col or "").strip().lower() for col in denied if str(col or "").strip()}


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


def _execute(
    compiled: dsl_compiler.Compiled, scope: DataScope, *, nl: str, trace_id: str,
    cache_key: str = "",
) -> dict[str, Any]:
    """执行编译产物: 只读角色 + 会话作用域 + 扫描量上限 + 出口治理。"""
    settings = get_settings()
    table = _table_of(compiled.sql)
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
        # 扫描量超限是"正确的拒", 不计入熔断: 否则两个人先后问同一个大表就会把这个
        # 口径对全公司短路掉。
        _audit(trace_id=trace_id, scope=scope, user_id=scope.user_id, role=scope.role,
               nl=nl, generated=compiled.route, final_sql=compiled.sql,
               decision="deny", reason=str(exc))
        return {"error": f"扫描量超限被拒: {exc}", "error_codes": ["E_ROWS_CAP"], "sql": compiled.sql}
    except SQLAlchemyError as exc:
        logger.warning("[askdata] 编译 SQL 执行失败: %s", exc)
        opened = breaker.record_error(compiled.route, table)
        _audit(trace_id=trace_id, scope=scope, user_id=scope.user_id, role=scope.role,
               nl=nl, generated=compiled.route, final_sql=compiled.sql,
               decision="deny", reason=f"执行失败 {type(exc).__name__}")
        codes = ["E_ENGINE"] + (["E_ENGINE_BREAKER"] if opened else [])
        return {"error": f"取数执行失败: {type(exc).__name__}: {str(exc)[:200]}",
                "error_codes": codes, "sql": compiled.sql, "table": table}

    breaker.record_success(compiled.route, table)
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
        "semver": compiled.semver,
        "policies": compiled.policies,
    }
    if cache_key:
        # 只存可序列化的结果体(不存 sql/params: 下一轮会重新编译并重新审计一次)。
        resultcache.put(cache_key, {
            "columns": out["columns"], "column_labels": out["column_labels"],
            "rows": out.get("rows") or [], "rowcount": out.get("rowcount", 0),
            "caliber_notes": out["caliber_notes"], "scope_note": out["scope_note"],
            "route": out["route"], "semver": out["semver"], "policies": out["policies"],
        })
    if suspicious and get_settings().dataops_suspicious_data_guard:
        out[spotlight.SUSPICIOUS_KEY] = suspicious
    return out


def _table_of(sql: str) -> str:
    """从 SQL 里取第一张事实表名(只用来做熔断计数键, 不参与任何安全判定)。"""
    match = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", sql or "", flags=re.I)
    return match.group(1) if match else "?"


def _metric_table(catalog, spec: QuerySpec) -> str:
    """本轮第一个指标的事实表(没登记/没指标时返 "?", 熔断键拿不到表也不能抛穿工具)。"""
    if not spec.metrics:
        return "?"
    metric = catalog.get_metric(spec.metrics[0].id)
    return metric.source_table if metric is not None else "?"


# ---------------------------------------------------------------------------
# 语义层读取与链接
# ---------------------------------------------------------------------------
@mcp.tool()
def list_metrics(
    domain: str = "all", caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """列出语义层登记的指标与维度(**元数据, 不含任何业务数据**)。

    问数之前先看这里: 只有列出来的指标才查得到, 列不出来的口径不存在。
    ``origin`` 说明这份快照来自数据库还是代码内置兜底集; ``semver`` 是定义集指纹;
    ``missing_tables`` 非空说明部分细分面(枚举/兼容规则/行列策略)本轮没读出来。

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
            "semver": catalog.semver,
            "domain": domain,
            "metrics": [],
            "note": f"域 {domain} 没有登记指标; 可用域: {catalog.domains()}",
            "dimensions": catalog.render_dimensions(),
        }
    return {
        "origin": catalog.origin,
        "semver": catalog.semver,
        "domain": domain,
        "summary": catalog.to_payload(),
        "metrics": [
            {
                "id": m.metric_id, "label": m.label, "unit": m.unit, "domain": m.domain,
                "caliber": m.caliber_note(), "compatible_dims": list(m.compatible_dims),
                "calendar": m.calendar, "version": m.version, "owner": m.owner,
            }
            for m in metrics
        ],
        "dimensions": catalog.render_dimensions(),
    }


@mcp.tool()
def link_terms(
    text: str, caller_user_id: str = "", caller_role: str = "", trace_id: str = "",
    phrase_conf_json: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """把用户原话链接到语义层对象(三路召回: 词典精确 / 向量相似 / 枚举值反查)。

    返回按分数降序的候选, 供智能体决定"直接选用"还是"发起选项式澄清"。
    **本工具不替用户拍板**: 分数不够用时去澄清, 不要挑一个看起来差不多的口径硬算。

    Args:
        text: 用户原话(或已消解指代的问题)。
        caller_user_id/caller_role: 网关注入的身份, 决定枚举反查能看到哪些部门/供应商。
        trace_id: 链路号。
        phrase_conf_json: ①抽出的"说法 -> 置信度", 作为打分先验(不影响是否命中)。
    """
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    catalog = registry.get_catalog()
    result = semantic_link.link(
        text, catalog=catalog, scope=scope,
        top_n=max(4, get_settings().askdata_clarify_max_options * 2),
        phrase_conf=_as_dict(phrase_conf_json),
    )
    payload: dict[str, Any] = {
        "query": text,
        "candidates": result.to_payload(),
        "enum_filters": result.enum_filters,
        "catalog_origin": catalog.origin,
        "semver": catalog.semver,
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
        "auto_filters": list(check.auto_filters),
        "resolved_range": check.resolved.to_payload() if check.resolved else {},
        "metrics": [{"id": m.metric_id, "label": m.label, "caliber": m.caliber_note()} for m in check.metrics],
        "semver": catalog.semver,
        "catalog_origin": catalog.origin,
    }


# ---------------------------------------------------------------------------
# 降级链: 指标 API -> DSL 编译 -> 沙箱 SQL
# ---------------------------------------------------------------------------
@mcp.tool()
def similar_cases(
    question: str, limit: int = 3, caller_user_id: str = "", trace_id: str = ""
) -> dict[str, Any]:
    """从自建 golden 集里找几条最相似的问法(dynamic few-shot)。

    为什么拿真问法而不是写死的例子: 静态 few-shot 只能告 DSL 的"形状", 而同义问法的
    真实例子才能告"这类说法应该选哪个口径" —— 这是 DSL 准确率里最便宜的一块提升。
    相似度用字符二元组 Jaccard(中文短句上够用且完全确定), 不用向量: 拿向量做 few-shot
    得先给 golden 建 embedding 并随修订重建, 而这里要的只是"挑几条长得像的"。

    Args:
        question: 本轮用户原话(或已消解指代的问题)。
        limit: 最多返几条(0/负数按 1 处理, 上限 6)。
        caller_user_id/trace_id: 身份与链路号(只读元数据, 不返业务行)。
    """
    cases = _golden_snapshot()
    top_k = max(1, min(6, int(limit or 3)))
    scored = sorted(
        ((similarity(question, str(item.get("question") or "")), item) for item in cases),
        key=lambda pair: (-pair[0], str(pair[1].get("case_id") or "")),
    )
    out = []
    for score, item in scored[:top_k]:
        if score <= 0.05:
            continue
        dsl = item.get("dsl_json") or {}
        if not isinstance(dsl, dict) or not dsl.get("metrics"):
            continue  # 没标 DSL 的用例做不了 few-shot(它只能当结果集比对的原料)
        out.append({"question": item.get("question"), "dsl": dsl, "score": round(score, 3)})
    return {"question": question, "examples": out, "pool_size": len(cases)}


def _golden_snapshot() -> list[dict[str, Any]]:
    """启用中的 golden 用例(带 TTL 缓存): few-shot 不该每轮都全表读一次。"""
    from app.askdata import store as logstore

    now = time.monotonic()
    with _GOLDEN_LOCK:
        if _GOLDEN["rows"] and now - float(_GOLDEN["at"]) <= _GOLDEN_TTL:
            return list(_GOLDEN["rows"])
    try:
        rows = logstore.list_golden(enabled_only=True, limit=2000)
    except Exception as exc:  # noqa: BLE001 - few-shot 拿不到就不带例子, 不能卡住问数
        logger.warning("[askdata] golden 读取失败(few-shot 降级为空): %s", exc)
        rows = []
    with _GOLDEN_LOCK:
        _GOLDEN["rows"] = rows
        _GOLDEN["at"] = now
    return list(rows)


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard(归一后): 0~1, 对中文短句稳定且不依赖模型。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", (text or "").strip().lower())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


@mcp.tool()
def metric_baseline(
    metric_id: str, shape: str = "", samples: int = 20, caller_user_id: str = "",
    caller_role: str = "", trace_id: str = "",
) -> dict[str, Any]:
    """查一个口径最近的已回答结果量级(给结果校验判"这个数健不健康"用)。

    数据源是问数自己的留痕表, 所以它是**自参照基线**: 只用中位数与 MAD(对极端值鲁棒),
    样本不够就直说"没有基线", 并且**只用于标注"数据存疑"而不拦下答案** —— 拿自己的
    历史当尺子去否决本轮结果, 会把一个错数固化成"以后这个数都不许报"。

    Args:
        metric_id: 口径 id。
        shape: 本次结果形状(维度 id 逗号串), 同形状才可比(单值 vs 按月序列不是同一个量级)。
        samples: 取最近几条成功结果参与统计。
        caller_user_id/caller_role: 身份(基线只看同一作用域的人问过的量级)。
        trace_id: 链路号。
    """
    from app.askdata import store as logstore

    stats = logstore.metric_baseline(
        metric_id=metric_id, shape=shape, user_id=caller_user_id,
        role=caller_role, limit=max(5, min(200, int(samples or 20))),
    )
    return {"metric_id": metric_id, "shape": shape, **stats}


@mcp.tool()
def similar_intent(
    question: str, caller_user_id: str = "", caller_role: str = "", trace_id: str = ""
) -> dict[str, Any]:
    """语义缓存(V3): 这个问题与某条已标注问法够像时, 直接复用它的 DSL。

    省的是两次 LLM 调用(槽位抽取 + DSL 生成), **不是**跳过校验: 拿回来的 DSL 仍然走
    clarify_gate 之后的 validate/compile/RLS/审计与结果缓存, 所以它不会绕过任何口径闸。

    三条保守规则:
    1. 只认相似度 ≥ ``askdata_semantic_cache_min_similarity`` 的命中(拿"听起来像"的口径出数最危险);
    2. 只认 **同一版语义层** 算出来的用例(``semver`` 相等): 口径改了旧 DSL 就不能再复用;
    3. 只认**人工批准过可复用**的问法(``cacheable = true``): 批量灌入的合成 golden 不参与,
       否则一是把未经人核对的口径推给真实用户, 二是让 agent 评测题直接拄到标准答案。
    另外不返回别人的问题原文, 只返回 DSL 与相似度。

    Args:
        question: 本轮用户原话。
        caller_user_id/caller_role: 身份(只用于审计; DSL 复用与身份无关, 行权限在引擎层)。
        trace_id: 链路号。
    """
    settings = get_settings()
    catalog = registry.get_catalog()
    if not settings.askdata_semantic_cache_enabled:
        return {"matched": False, "reason": "语义缓存已关闭", "semver": catalog.semver}
    from app.semantic.link import _embed_query_sync

    vector = _embed_query_sync(semantic_link._norm(question))
    if not vector:
        # 没有向量这一路就是"没命中", 不是错: 完整链路本来就能跑。
        return {"matched": False, "reason": "查询向量不可用", "semver": catalog.semver}
    literal = "[" + ",".join(f"{float(x):.7f}" for x in vector) + "]"
    threshold = float(settings.askdata_semantic_cache_min_similarity)
    sql = (
        "SELECT case_id, dsl_json, 1 - (embedding <=> CAST(:q AS vector)) AS similarity "
        "FROM askdata_golden WHERE enabled = true AND embedding IS NOT NULL "
        "AND cacheable = true AND semver = :semver AND dsl_json IS NOT NULL "
        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 1"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            row = conn.execute(text(sql), {"q": literal, "semver": catalog.semver}).first()
    except Exception as exc:  # noqa: BLE001 - 读不出向量就是没命中, 不能卡住问数
        logger.warning("[askdata] 语义缓存检索失败(走完整链路): %s", exc)
        return {"matched": False, "reason": "语义缓存检索不可用", "semver": catalog.semver}
    if row is None:
        return {"matched": False, "reason": "没有同版语义层的可复用用例", "semver": catalog.semver}
    data = dict(row._mapping)
    similarity_value = float(data.get("similarity") or 0.0)
    if similarity_value < threshold:
        return {"matched": False, "similarity": round(similarity_value, 4),
                "reason": f"最高相似度 {similarity_value:.3f} 低于阈值 {threshold:.2f}",
                "semver": catalog.semver}
    dsl = data.get("dsl_json")
    if isinstance(dsl, str):
        try:
            dsl = json.loads(dsl)
        except ValueError:
            return {"matched": False, "reason": "用例 DSL 不是合法 JSON", "semver": catalog.semver}
    _audit(trace_id=trace_id, scope=None, user_id=caller_user_id, role=caller_role,
           nl=question, generated="semantic_cache", final_sql="", decision="allow",
           reason=f"复用 golden 用例 {data.get('case_id')}(相似度 {similarity_value:.3f})")
    return {"matched": True, "dsl": dsl or {}, "similarity": round(similarity_value, 4),
            "case_id": str(data.get("case_id") or ""), "semver": catalog.semver}


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
    dialect: str = "",
) -> dict[str, Any]:
    """降级链第 2 级: 通用编译器把 DSL 编成 SQL 并执行(多维下钻 + 过滤 + 同环比)。

    同环比默认走单扫描条件聚合(事实表只扫一次), 口径形状解不出度量片段时退回双窗口 CTE
    —— 两种形状产出的列名一致。变化率由 SQL 算, 不要自己把两个数相减除一下。
    返回体带 ``sql`` 与 ``caliber_notes``, 回答里必须把口径与统计范围原样转达。

    Args:
        dsl_json: 强类型 DSL。
        caller_user_id/caller_role: 网关注入的身份。
        trace_id: 链路号。
        nl_question: 本轮用户原句。
        dialect: 可选(clickhouse/mysql/hive/spark/trino/duckdb)。给定就额外返
            ``sql_transpiled`` 供**人对比目标方言的编译产物**; 执行始终是 PostgreSQL
            (本仓只接了这一个数据源, 把取数打到别的引擎上是错而不是特性)。
    """
    out = _run_route(
        "compiler", dsl_json, caller_user_id=caller_user_id, caller_role=caller_role,
        trace_id=trace_id, nl_question=nl_question,
    )
    if dialect and dialect != "postgres" and out.get("sql"):
        try:
            out["sql_transpiled"] = dsl_compiler.transpile_sql(str(out["sql"]), dialect)
            out["sql_dialect"] = dialect.strip().lower()
        except dsl_compiler.CompileError as exc:
            # 转译不了不影响已算出的结果: 它是附带能力, 不能反过来拆掉主链路。
            out["sql_dialect_error"] = str(exc)
    elif dialect and dialect != "postgres" and out.get("from_cache"):
        out["sql_dialect_note"] = "结果来自缓存, 本轮没重编译所以没有可转译的 SQL 文本"
    return out


def _run_route(
    route: str,
    dsl_json: Any,
    *,
    caller_user_id: str,
    caller_role: str,
    trace_id: str,
    nl_question: str,
) -> dict[str, Any]:
    """两条编译通道的共同执行体(缓存 -> 熔断判定 -> 校验 -> 编译 -> 执行 -> 审计), 不各写一份。"""
    spec, syntax_denial = _spec_of(dsl_json)
    if syntax_denial is not None:
        return syntax_denial
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    if denial is not None:
        return denial
    catalog = registry.get_catalog()
    settings = get_settings()
    # 结果缓存(B1): key = normalize(DSL) + 身份 + semver。拿不到 semver/没开启时直接不查。
    key = resultcache.cache_key(
        dsl=spec.to_payload(), route=route, user_id=caller_user_id, role=caller_role,
        semver=catalog.semver, scope_note=scope.note(),
    )
    hit = resultcache.get(key)
    if hit is not None:
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=f"cache:{route}", final_sql="",
               decision="allow", reason=f"结果缓存命中({hit.get('rowcount', 0)} 行)")
        return {**hit, "from_cache": True, "sql": "", "params": {}}
    # 熔断(G14): 这一级在窗口内连续报错过就直接不试(不编译也不打库)。
    table = _metric_table(catalog, spec)
    if breaker.is_open(route, table):
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=f"breaker:{route}", final_sql="", decision="deny",
               reason=f"{route} 对表 {table} 处于熔断中")
        return {
            "error": f"{route} 通道对表 {table} 正在熔断(近期连续报错), 本轮不碰库",
            "error_codes": ["E_ENGINE_BREAKER"],
            "next": "exec_sql_sandbox" if route != "sandbox" else "",
            "table": table,
        }
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
            "semver": catalog.semver,
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
    return _execute(compiled, scope, nl=nl_question, trace_id=trace_id, cache_key=key)


@mcp.tool()
def exec_sql_sandbox(
    sql: str, caller_user_id: str = "", caller_role: str = "", trace_id: str = "",
    nl_question: str = "",
) -> dict[str, Any]:
    """降级链第 3 级(最后一级): 在只读沙箱里执行一条你自写的 SELECT。

    只有指标 API 与编译器都覆盖不了时才用(例如语义层还没登记这个口径)。
    沙箱不是许可: 单条 SELECT、**只能碰 v_analytics_* 治理视图**(基表不在可查面内,
    机密列已从视图里拿掉)、行数与扫描量有上限、不允许无关联条件的多表连接、
    能读到哪些行由行级安全决定; 校验被拒时**不要改写成语义层外的表或去掉 WHERE 再试**,
    如实说明这个口径还没被治理过。

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
    catalog = registry.get_catalog()
    semver = catalog.semver
    table = _table_of(sql)
    if breaker.is_open("sandbox", table):
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated="breaker:sandbox", final_sql=sql, decision="deny",
               reason=f"沙箱对表 {table} 处于熔断中")
        return {"error": f"沙箱对表 {table} 正在熔断(近期连续报错)",
                "error_codes": ["E_ENGINE_BREAKER"], "table": table, "semver": semver}
    try:
        safe_sql = validate_sql(
            sql, allowed_tables=_sandbox_allowed_tables(), mode=READ_MODE,
            max_rows=settings.askdata_max_rows,
            # 沙箱专项护栏: 列策略黑名单 + JOIN 数上限 + 笛卡尔拒。
            # 星号投影只在"退回基表可查面"时才拒: 视图本身已不含机密列,
            # 那时拒 * 只会把合法问法拦在门外。
            denied_columns=_sandbox_denied_columns(caller_role),
            deny_star_projection=not settings.askdata_sandbox_use_views,
            max_joins=settings.askdata_sandbox_max_joins,
            reject_cartesian=settings.askdata_sandbox_reject_cartesian,
        )
    except ASTGuardError as exc:
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=sql, final_sql=sql, decision="deny",
               reason=f"AST 拒: {exc}")
        # AST 拒是"这条 SQL 不该跑", 不是引擎故障: 不计入熔断(否则模型写得丑一点就把
        # 整个沙箱对这张表锁掉, 后面所有人都拿不到数)。
        return {"error": f"SQL 校验失败(AST 拒): {exc}", "sql": sql, "semver": semver}
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
        return {"error": f"扫描量超限被拒: {exc}", "sql": safe_sql, "semver": semver}
    except SQLAlchemyError as exc:
        opened = breaker.record_error("sandbox", table)
        _audit(trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
               nl=nl_question, generated=sql, final_sql=safe_sql, decision="deny",
               reason=f"沙箱执行失败 {type(exc).__name__}")
        codes = ["E_ENGINE"] + (["E_ENGINE_BREAKER"] if opened else [])
        return {"error": f"SQL 执行失败: {type(exc).__name__}: {str(exc)[:200]}",
                "sql": safe_sql, "semver": semver, "error_codes": codes, "table": table}
    breaker.record_success("sandbox", table)
    block = payload[0] if payload else {"columns": [], "rows": [], "rowcount": 0}
    block["rows"] = masking.mask_rows(block.get("rows") or [])
    marked = spotlight.mark_payload([block], scope_note=scope.note())
    _audit(
        trace_id=trace_id, scope=scope, user_id=caller_user_id, role=caller_role,
        nl=nl_question, generated=sql, final_sql=safe_sql, decision="allow",
        reason=f"沙箱取数 {block.get('rowcount', 0)} 行",
        cost={"estimated_scan_rows": scan_rows},
    )
    return {**marked[0], "route": "sandbox", "sql": safe_sql, "scope_note": scope.note(),
            "semver": semver}


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

    chart_type: line(时间序列) / pie(占比) / bar(类目对比) / heatmap(两个维度交叉)。
    与 Analyst_Agent 的 render_chart 同一套渲染器与台账, 只是调用方不同。

    Args:
        title: 图表标题(同时用于产物文件名)。
        chart_type: bar / line / pie / heatmap。
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
    semver: str = "",
    catalog_origin: str = "",
    token_cost: int = 0,
    llm_calls: int = 0,
    retry_count: int = 0,
    cache_hit: bool = False,
    metric_summary: dict[str, Any] | str | None = None,
    clarify_pick: str = "",
    clarify_top: str = "",
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
        semver: 本轮用的语义层快照指纹 —— badcase 回溯到当时口径的唯一依据(G1)。
        catalog_origin: db / seed(库里治理过的口径还是代码内置兜底集)。
        token_cost/llm_calls/retry_count: 本轮花了多少模型资源(预算熔断与成本看板用)。
        cache_hit: 本轮结果来自结果缓存(命中率不能拿 route 当判据, 会坏一次通过率分母)。
        metric_summary: 本轮主指标量级({"shape":..., "metrics":...}), 历史基线 z-score 的原料。
        clarify_pick/clarify_top: 本轮是澄清点选时"用户选的口径"与"上一轮最高分口径"
        (两者相等就是误澄清的分子); 不是点选轮就留空。
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
        "semver": semver, "catalog_origin": catalog_origin,
        "token_cost": int(token_cost or 0), "llm_calls": int(llm_calls or 0),
        "retry_count": int(retry_count or 0), "cache_hit": bool(cache_hit),
        "metric_summary": _as_dict(metric_summary),
        "clarify_pick": clarify_pick, "clarify_top": clarify_top,
    }
    query_id = logstore.record_query(payload)
    if not query_id:
        # 留痕失败不能伪装成成功: 归因与看板会因此缺一块, 至少要把它带回给模型与日志。
        return {"status": "degraded", "query_id": "", "note": "问数留痕未能落库(看板将缺这一条)"}
    return {"status": "ok", "query_id": query_id}


@mcp.tool()
def create_ticket(
    question: str, reason: str = "user_request", trace_id: str = "", query_id: str = "",
    dsl_json: dict[str, Any] | str | None = None, semver: str = "",
    caller_user_id: str = "", caller_role: str = "",
) -> dict[str, Any]:
    """开一张人工工单(降级链的最后一出口: 这个问题该由人看, 不是系统再试一次)。

    适合开单的情形: 语义层确实没这个口径(no_metric) / 源头数据缺(no_data) / 超预算(budget_exceeded)
    / 引擎错(engine_error) / 用户主动要求(noanswer->user_request)。不要"为了先给个数"而随手编
    一个近似口径 —— 开单本身就是正确答案的一部分。

    Args:
        question: 用户原话。
        reason: no_metric / no_data / budget_exceeded / engine_error / user_request / bad_answer。
        trace_id/query_id: 链路号与那一轮留痕号(处理工单的人要能回查当时算到哪一步)。
        dsl_json: 已经抽出的查询意图(人看到的就是"用户想要什么")。
        semver: 本轮用的语义层版本。
        caller_user_id/caller_role: 开单人工号与角色。
    """
    from app.askdata import store as logstore

    out = logstore.create_ticket(
        question=question, reason=reason, trace_id=trace_id, query_id=query_id,
        dsl=_as_dict(dsl_json), semver=semver, requested_by=caller_user_id,
    )
    if out.get("status") == "ok":
        _audit(trace_id=trace_id, scope=None, user_id=caller_user_id, role=caller_role,
               nl=question, generated="create_ticket", final_sql="", decision="allow",
               reason=f"工单 {out['ticket_id']}({reason})")
    return out


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
