"""数据洞察 MCP Server (FastMCP, streamable-http transport): NL2SQL -> 图表 -> 报告 -> 受治理写。

设计要点(与前两个 MCP server 的差别):
- **跨域读 + 作用域隔离**: 表白名单横跨 hr_/fin_/proc_, 但每一次取数都先由服务端的
  调用者身份解出 ``DataScope``, 然后在"analytics 只读角色 + 会话作用变量"的事务里执行。
  隔离的真防线是数据库的 RLS(见 app/db/rls.py), 不是"模型被叮嘱过要小心"。
- **写不再是自由 SQL**: 模型只能交结构化意图(plan_data_op), 本 server 查名单、注入
  域谓词、参数绑定、跑预演(dry-run)、按影响行数分档后进 pending。真正的执行需要
  发起人二次确认(confirm_data_op)或审批台批准。
- **图表/报告是产物, 不是文本**: 出图落 data/reports 并回 URL(SVG 由浏览器渲染,
  中文不需要服务端字体), 报告同理。把 400 行 SVG 塞进对话既烧 token 又画不出来。
  图要进 office 文档时另取同一次调用返回的 png_url(位图才能被 docx/pptx/pdf 嵌)。
- **不新增绘图依赖**: 图表用 app/analytics/charts.py 的纯 Python SVG 渲染, PNG 走
  既有依赖 Pillow —— 不引 matplotlib/cairosvg(镜像体积与容器 CJK 字体的双重代价)。
- **口径写死在 SQL 里**: 周报的固定指标由 app/analytics/reports.py 产出, LLM 只
  读数值写结论, 不负责定义统计口径 —— 防"两版周报对不上账"。
- **结果也是不可信输入**: 读回来的行数据统一下发 untrusted_data 标记与声明(层 5-C),
  敏感列在出口打码, 文本里出现指令样式的单元格会被标出并留痕。

Run:
    python -m app.mcp_servers.analytics_server   # serves http://0.0.0.0:8005/mcp
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import SQLAlchemyError

from app.analytics import charts, reports, store
from app.config import get_settings
from app.db import dataops
from app.db import sync as dbsync
from app.db.ast_guard import ASTGuardError, READ_MODE, validate_sql
from app.db.rls import read_role
from app.db.schema_docs import ANALYTICS_SCHEMA_DDL
from app.db.scope import DataScope, ScopeError, resolve_scope_sync
from app.db.sql_guard import SQLGuardError
from app.security import masking, spotlight

logger = logging.getLogger(__name__)

mcp = FastMCP("enterprise-data-analytics", host="0.0.0.0", port=8005)

_CST = timezone(timedelta(hours=8))

# 跨域只读表白名单(单一事实来源: 与 ANALYTICS_SCHEMA_DDL 描述的表一一对应)。
ALLOWED_TABLES = reports.ALLOWED_TABLES

# 按业务域裁剪的 schema 说明: 让 Agent 按需取一份, 而不是一次吞下全量字段。
_DOMAIN_DOCS: dict[str, str] = {
    "all": ANALYTICS_SCHEMA_DDL,
    "expense": ANALYTICS_SCHEMA_DDL,
}


def _today() -> date:
    return datetime.now(_CST).date()


def _scope_of_caller(caller_user_id: str, caller_role: str) -> tuple[DataScope | None, dict[str, Any] | None]:
    """把网关注入的身份换成数据作用域; 解不出时返回一个拒用错误体。

    默认拒: 解不到调用者(网关未注入, 或直连本 server)绝不退化成"不过滤=看全部"。
    """
    try:
        return resolve_scope_sync(caller_user_id, caller_role), None
    except ScopeError as exc:
        return None, [{"error": f"无法确定数据作用域: {exc}", "forbidden": True}]
    except Exception as exc:  # noqa: BLE001 - 查不到主数据也是"解不出作用域"
        logger.warning("作用域解析异常(caller=%s): %s", caller_user_id, exc)
        return None, [{"error": "无法确定数据作用域(员工主数据不可用)", "forbidden": True}]


def _parse_date(value: str, fallback: date) -> date:
    """宽松解析 YYYY-MM-DD; 解析不出来用 fallback(不让格式问题毁掉一次查询)。"""
    text = (value or "").strip()
    if not text:
        return fallback
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return fallback


@mcp.tool()
def run_sql(
    sql: str, caller_user_id: str = "", caller_role: str = "", trace_id: str = ""
) -> list[dict[str, Any]]:
    """数据洞察 Text2SQL: 在**当前调用者可读的数据范围**内执行一条 SELECT。

    可查表: hr_employees / hr_tickets / hr_leave_records / fin_reimbursements /
    fin_department_budgets / proc_orders / proc_contracts / proc_suppliers。
    仅允许单条 SELECT(或 WITH ... SELECT); 禁止写操作、DDL、系统目录与服务端函数;
    结果最多 50 行。能读到哪些行由服务端的 RLS 决定, 不要自己写部门条件去"绕过"。

    Args:
        sql: 一条只读 PostgreSQL SELECT。
        caller_user_id: 网关注入的调用者工号(不由你填, 传了也会被覆盖)。
        caller_role: 网关注入的调用者角色。
        trace_id: 链路号(网关注入, 用于把拒因归到同一轮对话)。

    Returns:
        结果列表, 形如 ``[{columns, rows, rowcount, untrusted_data, data_notice,
        scope_note}]``; 校验/执行失败时返回 ``[{error, sql}]`` —— 把错误原样交回,
        由 Agent 依据 error 修正 SQL 重试一次。
    """
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    if denial is not None:
        _audit_read(trace_id, None, caller_user_id, caller_role, sql, "deny",
                    denied_reason(denial))
        return denial
    settings = get_settings()
    try:
        safe_sql = validate_sql(
            sql, allowed_tables=ALLOWED_TABLES, mode=READ_MODE,
            max_rows=settings.sqlguard_max_rows,
        )
    except ASTGuardError as exc:
        _audit_read(trace_id, scope, caller_user_id, caller_role, sql, "deny", str(exc))
        return [{"error": f"SQL 校验失败(AST 拒): {exc}", "sql": sql}]
    try:
        payload, scan_rows = dbsync.execute_scoped_select(
            safe_sql,
            None,
            db_role=read_role(),
            session_settings=scope.session_settings(),
            max_scan_rows=settings.sqlguard_max_scan_rows,
        )
    except dbsync.ScanCostExceeded as exc:
        _audit_read(trace_id, scope, caller_user_id, caller_role, safe_sql, "deny", str(exc))
        return [{"error": f"SQL 成本预估被拒: {exc}", "sql": safe_sql}]
    except SQLAlchemyError as exc:
        return [{"error": f"SQL 执行失败: {exc.__class__.__name__}: {str(exc)[:200]}", "sql": safe_sql}]

    # 出口治理: 敏感列打码(DLP) + 数据标记(spotlighting) + 可疑单元格留痕。
    for block in payload:
        block["rows"] = masking.mask_rows(block.get("rows") or [])
    marked = spotlight.mark_payload(payload, scope_note=scope.note())
    suspicious = spotlight.find_suspicious(marked)
    if suspicious and settings.dataops_suspicious_data_guard:
        for block in marked:
            block[spotlight.SUSPICIOUS_KEY] = suspicious
        _audit_read(
            trace_id, scope, caller_user_id, caller_role, safe_sql, "allow",
            f"数据里发现 {len(suspicious)} 处指令样式单元格(已标记, 未执行)",
        )
    _audit_read(
        trace_id, scope, caller_user_id, caller_role, safe_sql, "allow",
        f"返回 {marked[0].get('rowcount') if marked else 0} 行",
        cost={"estimated_scan_rows": scan_rows},
    )
    return marked


def denied_reason(denial: list[dict[str, Any]]) -> str:
    """从错误体里取一句理由(审计只存结论, 不存整叠结构)。"""
    return (denial[0].get("error") if denial and isinstance(denial[0], dict) else "") or "作用域解析失败"


def _audit_read(
    trace_id: str,
    scope: DataScope | None,
    user_id: str,
    role: str,
    sql: str,
    decision: str,
    reason: str = "",
    cost: dict[str, Any] | None = None,
) -> None:
    """只读路径也进结构化审计: "被拒的查询"是发现注入尝试的主要信号。"""
    try:
        dataops.record_audit(
            trace_id=trace_id or "", scope=scope, user_id=user_id, role=role,
            nl_question="", generated=sql, final_sql=sql, decision=decision,
            reason=reason, cost=cost,
        )
    except Exception as exc:  # noqa: BLE001 - 留痕不能阻断取数
        logger.warning("run_sql 审计写入失败(忽略): %s", exc)


@mcp.tool()
def describe_tables(
    domain: str = "all", caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """返回可用于 Text2SQL 的业务表字段说明与 SQL 方言要求(只读元数据, 不含数据)。

    Args:
        domain: 目前仅 "all"(跨域全量字段说明); 保留参数以便后续按域裁剪。
        caller_user_id: 网关注入的调用者工号(用于回显你能看到的范围)。
        caller_role: 网关注入的调用者角色。

    Returns:
        {domain, ddl_note, scope_note}; 未知域回退到 all 并在 note 中说明。
    """
    key = (domain or "all").strip().lower()
    note = _DOMAIN_DOCS.get(key)
    scope, _denial = _scope_of_caller(caller_user_id, caller_role)
    scope_note = scope.note() if scope else "未能确定数据作用域(只能看元数据)"
    if note is None:
        return {"domain": "all", "ddl_note": ANALYTICS_SCHEMA_DDL,
                "note": f"未知域 {key}, 已返回全量", "scope_note": scope_note}
    return {"domain": key, "ddl_note": note, "scope_note": scope_note}


@mcp.tool()
def get_metrics_snapshot(
    period: str = "week",
    start: str = "",
    end: str = "",
    caller_user_id: str = "",
    caller_role: str = "",
) -> dict[str, Any]:
    """取一段周期内的固定口径经营指标(费用/预算/工单/请假/采购/合同)。

    指标 SQL 写死在服务端, 保证每次输出口径一致 —— 需要"数字"时优先用本工具,
    不要为了一个总数让 LLM 现写一条聚合 SQL。

    本工具的隔离靠 RLS(它不逐条手写域谓词, 因为 SQL 本身就是服务端的), 所以
    **部门级调用者只在 RLS 开启时可用**; RLS 关掉后固定口径只对全员角色开放。

    Args:
        period: week / month / quarter(默认本周, 无法识别时按月)。
        start: 显式区间起点 YYYY-MM-DD, 提供后覆盖 period。
        end: 显式区间终点 YYYY-MM-DD(左闭右开)。
        caller_user_id: 网关注入的调用者工号。
        caller_role: 网关注入的调用者角色。

    Returns:
        结构化指标 + range_label(区间口径) + scope_note(数据范围口径);
        某域查询失败只让该域为空, 不影响整体。
    """
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    if denial is not None:
        return denial[0]
    if not get_settings().rls_enabled and not scope.all_depts:
        return {
            "error": "RLS 已关闭(RLS_ENABLED=false), 固定口径指标只能由全员角色使用; "
            "否则本工具会交出全员数字。",
            "forbidden": True,
        }
    today = _today()
    manual = bool(start or end)
    start_d = _parse_date(start, today - timedelta(days=7)) if manual else None
    end_d = _parse_date(end, today + timedelta(days=1)) if manual else None
    if manual:
        start_d = start_d or (end_d - timedelta(days=7))  # type: ignore[operator]
        end_d = end_d or (today + timedelta(days=1))
        label = f"{start_d.isoformat()} ~ {end_d.isoformat()}(自定义区间)"
    else:
        start_d, end_d, label = reports.resolve_range(period)
    metrics = reports.collect_metrics(start_d, end_d, scope)
    return {"range_label": label, "scope_note": scope.note(), **metrics}


@mcp.tool()
def render_chart(
    title: str,
    chart_type: str = "bar",
    categories: list[Any] | None = None,
    series: list[dict[str, Any]] | None = None,
    values: list[Any] | None = None,
    caller_user_id: str = "",
) -> dict[str, Any]:
    """把查询结果画成图并落盘, 返回可直接展示的 URL(SVG, 浏览器渲染中文无字体问题)。

    chart_type: bar(类目对比) / line(时间趋势) / pie(构成占比)。
    数据两种形态任选: categories=["研发部","市场部"] + values=[1200,800];
    或多系列 categories=[...] + series=[{"name":"金额","data":[...]},
    {"name":"笔数","data":[...]}]。饼图只用第一个系列。

    Args:
        title: 图表标题, 同时用于生成文件名 slug。
        chart_type: bar / line / pie。
        categories: 类目标签列表(或 [标签, 数值] 成对列表)。
        series: 多系列数据, 每项 {name, data}。
        values: 单系列数值, 与 categories 对齐。
        caller_user_id: 网关注入的调用者工号(写进产物台账, 供"只看自己产物"回查)。

    Returns:
        {name, url, png_url?, chart_type, categories_count} ; 失败返回 {error}。
        SVG url 用于展示; 需要把图嵌进 Word/PPT/PDF 时取 png_url(位图才能进 office)。
    """
    payload = charts.render(chart_type, title, categories or [], series, values)
    if "error" in payload:
        return {"error": payload["error"]}
    name = store.stamp("chart", ".svg", title)
    written = store.write_text(
        name,
        payload["svg"],
        created_by=caller_user_id or "",
        title=title,
        params={
            "chart_type": payload["chart_type"],
            "categories": (categories or [])[:50],
            "series_names": payload["series_names"],
        },
    )
    if "error" in written:
        return written
    # 同步产一份 PNG(仅位图能进 docx/pptx/pdf): Pillow 不可用或出错只丢 png_url, 不影响 SVG 主图。
    png_ref: dict[str, Any] = {}
    try:
        png_payload = charts.render_png(chart_type, title, categories or [], series, values)
        if "png" in png_payload:
            png_name = store.stamp("chart", ".png", title)
            png_written = store.write_bytes(
                png_name, png_payload["png"], title=title,
                params={"chart_type": payload["chart_type"]},
            )
            if "url" in png_written:
                png_ref = {"png_name": png_written["name"], "png_url": png_written["url"]}
    except Exception as exc:  # noqa: BLE001 - PNG 是加分项
        logger.debug("chart png emission skipped: %s", exc)
    return {
        **written,
        "chart_type": payload["chart_type"],
        "categories_count": payload["categories_count"],
        "dropped_points": payload["dropped_points"],
        "markdown": f"![{title}]({written['url']})",
        **png_ref,
    }


@mcp.tool()
def write_weekly_report(
    period: str = "week",
    start: str = "",
    end: str = "",
    focus_department: str = "",
    caller_user_id: str = "",
    caller_role: str = "",
) -> dict[str, Any]:
    """生成一份周期经营报告: 图表(SVG) + Markdown 正文落盘, 返回可打开的 URL。

    固定指标由服务端 SQL 产出(口径稳定), 结论段由模型基于指标撰写; 指标为空时
    仍出报告并如实标注"本期无数据", 不编造数字。

    报告里的数字同样受 RLS 约束: 部门级角色生成的是一份**只统计到自己部门**的报告。

    Args:
        period: week / month / quarter。
        start: 自定义区间起点 YYYY-MM-DD。
        end: 自定义区间终点 YYYY-MM-DD。
        focus_department: 关注部门(如 研发部), 会在结论里单列其费用与预算对比。
        caller_user_id: 网关注入的调用者工号(进作用域与产物台账)。
        caller_role: 网关注入的调用者角色。

    Returns:
        {name, url, charts, metrics, scope_note} 或 {error}; charts 为内嵌图表的 URL 列表。
    """
    scope, denial = _scope_of_caller(caller_user_id, caller_role)
    if denial is not None:
        return denial[0]
    if not get_settings().rls_enabled and not scope.all_depts:
        return {
            "error": "RLS 已关闭(RLS_ENABLED=false), 固定口径报告只能由全员角色生成; "
            "否则报告会交出全员数字。",
            "forbidden": True,
        }
    manual = bool(start or end)
    if manual:
        start_d = _parse_date(start, _today() - timedelta(days=7))
        end_d = _parse_date(end, _today() + timedelta(days=1))
        label = f"{start_d.isoformat()} ~ {end_d.isoformat()}(自定义区间)"
    else:
        start_d, end_d, label = reports.resolve_range(period)

    metrics = reports.collect_metrics(start_d, end_d, scope)
    embedded: list[tuple[str, str]] = []
    links: list[str] = []

    daily = [
        {"name": "每日费用", "data": [row.get("total", 0) for row in metrics.get("expense_daily") or []]}
    ]
    daily_cats = [row.get("day", "") for row in metrics.get("expense_daily") or []]
    if daily_cats:
        chart = render_chart(f"费用日趋势 · {label}", "line", daily_cats, daily, None,
                             caller_user_id=caller_user_id)
        if "url" in chart:
            embedded.append(("费用日趋势", chart["name"]))
            links.append(chart["url"])

    cat_rows = metrics.get("expense_by_category") or []
    if cat_rows:
        chart = render_chart(
            "费用类别占比",
            "pie",
            [r.get("category", "") for r in cat_rows],
            None,
            [r.get("total", 0) for r in cat_rows],
            caller_user_id=caller_user_id,
        )
        if "url" in chart:
            embedded.append(("费用类别占比", chart["name"]))
            links.append(chart["url"])

    dept_rows = metrics.get("expense_by_department") or []
    if dept_rows:
        chart = render_chart(
            "部门费用对比",
            "bar",
            [r.get("department", "") for r in dept_rows],
            [
                {"name": "金额", "data": [r.get("total", 0) for r in dept_rows]},
                {"name": "笔数", "data": [r.get("cnt", 0) for r in dept_rows]},
            ],
            None,
            caller_user_id=caller_user_id,
        )
        if "url" in chart:
            embedded.append(("部门费用对比", chart["name"]))
            links.append(chart["url"])

    body = reports.build_markdown(metrics, label, embedded)
    summary = _summarize(metrics, label, focus_department)
    body = body.replace(
        "## 六、口径说明",
        f"## 六、结论与关注点\n\n{summary}\n\n## 七、口径说明",
        1,
    )
    title = f"经营周期报告 {label}"
    name = store.stamp("report", ".md", focus_department or "weekly")
    # 把数据范围写进报告本体: 一份"只看得到本部门"的报告必须在正文里说清口径,
    # 否则下次有人拿它跟全量报告对账时会当成矛盾。
    body = body.rstrip() + f"\n\n> 数据范围: {scope.note()}(由服务端行级安全判定)。\n"
    written = store.write_text(
        name,
        body,
        created_by=caller_user_id or "",
        title=title,
        params={"period": period, "start": start_d.isoformat(), "end": end_d.isoformat(),
                "focus_department": focus_department, "scope": scope.note()},
    )
    if "error" in written:
        return {"error": written["error"], "metrics": metrics}
    return {"name": written["name"], "url": written["url"], "charts": links,
            "summary": summary, "metrics": metrics, "scope_note": scope.note(),
            "markdown_bytes": written["bytes"]}


@mcp.tool()
def list_artifacts(
    limit: int = 10, caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """列出最近生成的分析产物(图表/报告), 便于回答"上周那份报告在哪"。

    只列**本人**生成的产物: 台账里的标题本身就会泄内容("各部门费用对比"这种),
    把别人的产物列表交出去等于一次元数据泄露。

    Args:
        limit: 返回条数上限(1~50)。
        caller_user_id: 网关注入的调用者工号(按它过滤台账)。
        caller_role: 网关注入的调用者角色(仅回显用)。

    Returns:
        {artifacts: [{name, kind, title, url, created_at}]}; DB 不可用时为空列表。
    """
    return {
        "artifacts": store.recent_artifacts(
            (caller_user_id or "").strip(), limit=max(1, min(50, int(limit or 10)))
        )
    }


# ---------------------------------------------------------------------------
# 层 2/4: 受治理的写通道(模型只交结构化意图, 不写 SQL)
# ---------------------------------------------------------------------------
@mcp.tool()
def plan_data_op(
    plan: dict[str, Any] | str,
    caller_user_id: str = "",
    caller_role: str = "",
    caller_intent_text: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """提交一个数据变更**计划**(不执行): 用结构化 JSON 描述要改哪张表、改什么、按什么条件。

    不要写 UPDATE/DELETE 语句 —— 本工具根本不接受 SQL。服务端会:查实体与字段白名单、
    注入租户/部门谓词、参数绑定、用同一份条件跑预演(COUNT)、按影响行数决定去向。

    plan 的形状::

        {"action": "delete", "entity": "proc_orders",
         "filters": [{"field": "status", "op": "eq", "value": "CANCELLED"},
                     {"field": "created_at", "op": "lt", "value": "2026-01-01"}],
         "reason": "清理半年前的取消订单"}

    action: update(配 sets 改字段) / delete(软删除, 不物理删行)。
    op: eq / ne / lt / lte / gt / gte / in / is_null。不能写 tenant_id/dept_id 条件。

    返回体里的 ``preview`` 必须**原样回显给用户**并请其明确回复确认, 然后才能在下一轮
    调 :func:`confirm_data_op`; 影响行数大的计划会进审批台, 不由你执行。

    Args:
        plan: 上述结构化意图(dict 或 JSON 字符串)。
        caller_user_id: 网关注入的调用者工号(发起人)。
        caller_role: 网关注入的调用者角色(写权限角色才能发起)。
        caller_intent_text: 网关注入的本轮用户原句(用于服务端核对真的要改数据)。
        trace_id: 链路号。

    Returns:
        {status, op_id, est_rows, preview, note, scope_note, expires_at, next_action}
        或 {error, denied/forbidden}。
    """
    if isinstance(plan, str):
        payload: Any = plan
    else:
        payload = json.dumps(plan, ensure_ascii=False)
    try:
        return dataops.plan_data_op(
            payload,
            caller_user_id=caller_user_id,
            caller_role=caller_role,
            nl_question=caller_intent_text,
            trace_id=trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - 不把堆栈透给模型, 但拒因要如实
        logger.exception("plan_data_op 失败")
        return {"error": f"写计划处理失败: {exc.__class__.__name__}: {str(exc)[:200]}"}


@mcp.tool()
def confirm_data_op(
    op_id: str,
    caller_user_id: str = "",
    caller_role: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """确认执行一个**由同一发起人提交且仍在自动档内**的写计划。

    只有在用户看过 ``preview`` 并明确说"确认"之后才能调本工具。服务端会复核:
    发起人必须与计划里同一个、令牌未过期、重跑预演后影响行数仍在自动档(跨了就转审批)。
    需要人工审批的计划不能由本工具自行执行。

    Args:
        op_id: :func:`plan_data_op` 返回的计划号。
        caller_user_id: 网关注入的调用者工号。
        caller_role: 网关注入的调用者角色。
        trace_id: 链路号。

    Returns:
        {status: EXECUTED, op_id, rows_affected, preview, rollback_hint} 或 {error}。
    """
    try:
        return dataops.confirm_data_op(
            op_id, caller_user_id=caller_user_id, caller_role=caller_role, trace_id=trace_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("confirm_data_op 失败")
        return {"error": f"写计划确认失败: {exc.__class__.__name__}: {str(exc)[:200]}"}


@mcp.tool()
def list_my_dataops(
    status: str = "", limit: int = 10, caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """回查自己提交过的写计划(含待确认/待审批/已执行/被拒)。

    Args:
        status: 只看某个状态(留空 = 全部)。
        limit: 条数上限(1~50)。
        caller_user_id: 网关注入的调用者工号。
        caller_role: 网关注入的调用者角色。

    Returns:
        {dataops: [{op_id, status, preview, est_rows, ...}]}。
    """
    ops = dataops.list_ops(
        user_id=caller_user_id or "", role=caller_role or "", status=status, limit=limit
    )
    # 回查列表只给模型看"自己的"计划: 有审批权的角色在审批台(REST)上看全部。
    mine = [op for op in ops if op.get("actor_user_id") == (caller_user_id or "").strip()]
    return {"dataops": mine[: max(1, min(50, int(limit or 10)))]}


def _summarize(metrics: dict[str, Any], label: str, focus_department: str) -> str:
    """把结构化指标交给模型写成结论段; 模型不可用时退回规则式要点。

    只给数值不给表结构: 结论段的作用是"读出重点", 不是再算一遍。
    """
    payload = {
        "区间": label,
        "报销": metrics.get("expense"),
        "类别Top": (metrics.get("expense_by_category") or [])[:3],
        "部门Top": (metrics.get("expense_by_department") or [])[:3],
        "预算消耗": (metrics.get("budget") or [])[:4],
        "工单": metrics.get("tickets"),
        "采购": metrics.get("purchase"),
        "合同": metrics.get("contracts"),
        "30天到期合同数": len(metrics.get("contracts_expiring_30d") or []),
        "关注部门": focus_department or "",
    }
    try:
        from app.llm import get_chat_model

        from app.config import get_settings

        settings = get_settings()
        llm = get_chat_model(settings.llm_model, temperature=0.2, json_mode=False)
        prompt = (
            "你是经营分析助手。基于下面这份只读统计结果, 写 3~5 条中文要点, "
            "每条一句话且必须带上具体数字; 只说数据支持的结论, 数据不足就直说, "
            "不要预测或编造。若给出关注部门, 单列一条它的表现。每条以 - 开头。\n\n"
            f"统计结果(JSON):\n{payload!r}"
        )
        resp = llm.invoke(prompt)
        text = str(resp.content).strip()
        if text:
            return text
    except Exception as exc:  # noqa: BLE001 - 结论段缺失不影响报告主体
        from app.analytics.reports import logger as report_logger

        report_logger.warning("report summary LLM failed, use rule-based summary: %s", exc)

    exp = metrics.get("expense") or {}
    con = metrics.get("contracts") or {}
    pur = metrics.get("purchase") or {}
    lines = [
        f"- 本期报销 {int(exp.get('cnt') or 0)} 单, 合计 {float(exp.get('total') or 0):,.2f} 元, "
        f"其中待审 {int(exp.get('pending') or 0)} 单。",
        f"- 采购申请 {int(pur.get('cnt') or 0)} 单 / {float(pur.get('total') or 0):,.2f} 元, "
        f"待审 {int(pur.get('pending') or 0)} 单。",
        f"- 合同初审 {int(con.get('cnt') or 0)} 份, 高风险 {int(con.get('high') or 0)} 份。",
        "- 未来 30 天内到期合同 "
        f"{len(metrics.get('contracts_expiring_30d') or [])} 份, 建议提前确认续签或终止。",
    ]
    if focus_department:
        row = next(
            (r for r in (metrics.get("expense_by_department") or []) if r.get("department") == focus_department),
            None,
        )
        lines.append(
            f"- {focus_department} 本期报销 "
            f"{float((row or {}).get('total') or 0):,.2f} 元 / {int((row or {}).get('cnt') or 0)} 单。"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
