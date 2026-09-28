"""数据洞察 MCP Server (FastMCP, streamable-http transport): NL2SQL -> 图表 -> 周期报告。

设计要点(与前两个 MCP server 的差别):
- **跨域只读**: 表白名单横跨 hr_/fin_/proc_, 但只走 sql_guard 的 SELECT 校验,
  任何写操作在语法层就被拒; 这是"分析"与"办理"的分界, 分析不改业务数据。
- **图表/报告是产物, 不是文本**: 出图落 data/reports 并回 URL(SVG 由浏览器渲染,
  中文不需要服务端字体), 报告同理。把 400 行 SVG 塞进对话既烧 token 又画不出来。
- **零新增依赖**: 图表用 app/analytics/charts.py 的纯 Python SVG 渲染, 不引
  matplotlib(镜像体积与容器 CJK 字体的双重代价)。
- **口径写死在 SQL 里**: 周报的固定指标由 app/analytics/reports.py 产出, LLM 只
  读数值写结论, 不负责定义统计口径 —— 防"两版周报对不上账"。

Run:
    python -m app.mcp_servers.analytics_server   # serves http://0.0.0.0:8005/mcp
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import SQLAlchemyError

from app.analytics import charts, reports, store
from app.db import sync as dbsync
from app.db.schema_docs import ANALYTICS_SCHEMA_DDL
from app.db.sql_guard import SQLGuardError

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
def run_sql(sql: str) -> list[dict[str, Any]]:
    """数据洞察 Text2SQL: 在跨业务域只读视图上执行一条 SELECT 并返回结果。

    可查表: hr_employees / hr_tickets / hr_leave_records / fin_reimbursements /
    fin_department_budgets / proc_orders / proc_contracts / proc_suppliers。
    仅允许单条 SELECT(或 WITH ... SELECT); 禁止写操作与系统表; 结果最多 50 行。
    需要字段说明时先调 describe_tables。统计口径不确定的跨域问题(如"费用含不含采购")
    应先向用户确认口径, 不要自行猜测。

    Args:
        sql: 一条针对上述白名单表的只读 PostgreSQL SELECT 语句。

    Returns:
        结果列表, 形如 [{columns, rows, rowcount}]; 校验或执行失败时返回
        [{error, sql}] —— 把错误原样交回, 由 Agent 依据 error 修正 SQL 重试一次。
    """
    try:
        return dbsync.execute_readonly_sql(sql, ALLOWED_TABLES)
    except SQLGuardError as exc:
        return [{"error": f"SQL 校验失败: {exc}", "sql": sql}]
    except SQLAlchemyError as exc:
        return [{"error": f"SQL 执行失败: {exc.__class__.__name__}: {str(exc)[:200]}", "sql": sql}]


@mcp.tool()
def describe_tables(domain: str = "all") -> dict[str, Any]:
    """返回可用于 Text2SQL 的业务表字段说明与 SQL 方言要求(只读元数据, 不含数据)。

    Args:
        domain: 目前仅 "all"(跨域全量字段说明); 保留参数以便后续按域裁剪。

    Returns:
        {domain, ddl_note}; 未知域回退到 all 并在 note 中说明。
    """
    key = (domain or "all").strip().lower()
    note = _DOMAIN_DOCS.get(key)
    if note is None:
        return {"domain": "all", "ddl_note": ANALYTICS_SCHEMA_DDL, "note": f"未知域 {key}, 已返回全量"}
    return {"domain": key, "ddl_note": note}


@mcp.tool()
def get_metrics_snapshot(period: str = "week", start: str = "", end: str = "") -> dict[str, Any]:
    """取一段周期内的固定口径经营指标(费用/预算/工单/请假/采购/合同)。

    指标 SQL 写死在服务端, 保证每次输出口径一致 —— 需要"数字"时优先用本工具,
    不要为了一个总数让 LLM 现写一条聚合 SQL。

    Args:
        period: week / month / quarter(默认本周, 无法识别时按月)。
        start: 显式区间起点 YYYY-MM-DD, 提供后覆盖 period。
        end: 显式区间终点 YYYY-MM-DD(左闭右开)。

    Returns:
        结构化指标 + range_label(口径说明); 某域查询失败只让该域为空, 不影响整体。
    """
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
    metrics = reports.collect_metrics(start_d, end_d)
    return {"range_label": label, **metrics}


@mcp.tool()
def render_chart(
    title: str,
    chart_type: str = "bar",
    categories: list[Any] | None = None,
    series: list[dict[str, Any]] | None = None,
    values: list[Any] | None = None,
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
) -> dict[str, Any]:
    """生成一份周期经营报告: 图表(SVG) + Markdown 正文落盘, 返回可打开的 URL。

    固定指标由服务端 SQL 产出(口径稳定), 结论段由模型基于指标撰写; 指标为空时
    仍出报告并如实标注"本期无数据", 不编造数字。

    Args:
        period: week / month / quarter。
        start: 自定义区间起点 YYYY-MM-DD。
        end: 自定义区间终点 YYYY-MM-DD。
        focus_department: 关注部门(如 研发部), 会在结论里单列其费用与预算对比。

    Returns:
        {name, url, charts, metrics} 或 {error}; charts 为内嵌图表的 URL 列表。
    """
    manual = bool(start or end)
    if manual:
        start_d = _parse_date(start, _today() - timedelta(days=7))
        end_d = _parse_date(end, _today() + timedelta(days=1))
        label = f"{start_d.isoformat()} ~ {end_d.isoformat()}(自定义区间)"
    else:
        start_d, end_d, label = reports.resolve_range(period)

    metrics = reports.collect_metrics(start_d, end_d)
    embedded: list[tuple[str, str]] = []
    links: list[str] = []

    daily = [
        {"name": "每日费用", "data": [row.get("total", 0) for row in metrics.get("expense_daily") or []]}
    ]
    daily_cats = [row.get("day", "") for row in metrics.get("expense_daily") or []]
    if daily_cats:
        chart = render_chart(f"费用日趋势 · {label}", "line", daily_cats, daily, None)
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
    written = store.write_text(
        name,
        body,
        title=title,
        params={"period": period, "start": start_d.isoformat(), "end": end_d.isoformat(),
                "focus_department": focus_department},
    )
    if "error" in written:
        return {"error": written["error"], "metrics": metrics}
    return {"name": written["name"], "url": written["url"], "charts": links,
            "summary": summary, "metrics": metrics, "markdown_bytes": written["bytes"]}


@mcp.tool()
def list_artifacts(limit: int = 10) -> dict[str, Any]:
    """列出最近生成的分析产物(图表/报告), 便于回答"上周那份报告在哪"。

    Args:
        limit: 返回条数上限(1~50)。

    Returns:
        {artifacts: [{name, kind, title, url, created_at}]}; DB 不可用时为空列表。
    """
    return {"artifacts": store.recent_artifacts("", limit=max(1, min(50, int(limit or 10))))}


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
