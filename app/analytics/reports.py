"""周期经营报告(周报/月报)的指标装配与 Markdown 组装。

与 LLM 自由 Text2SQL 的分工(本模块存在的主要理由):
- 周报的指标口径是**管理者反复要看的固定几项**, 每次让 LLM 现写 SQL 既烧 token
  又会漂移(这周算"已批准", 下周算"已打款", 两版周报对不上账);
- 所以固定指标由本模块用**写死的 SQL**产出(口径单一事实来源), LLM 只负责
  "读数成文"——把结构化指标写成有判断的中文摘要, 不参与口径定义。

区间口径: 全部按 created_at 落在 [start, end) 左闭右开, 内网东八区日历日。

数据作用域(层 1/2): 本模块的 SQL 是服务端写好的, 语法不必再过校验器, 但**隔离不能免**:
它们在同一份"生效角色 + 会话作用域"的事务里执行, 于是 RLS 策略对固定口径指标一样生效。
JOIN 两侧都会被过滤, 所以"部门费用对比"天然只统计到调用者能看见的那部分人与那部分单。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import sync as dbsync

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

# 与 analytics MCP server 同一张表白名单(见 app/mcp_servers/analytics_server.py)。
ALLOWED_TABLES = {
    "hr_employees",
    "hr_tickets",
    "hr_leave_records",
    "fin_reimbursements",
    "fin_department_budgets",
    "proc_orders",
    "proc_contracts",
    "proc_suppliers",
}


def current_cst_date() -> date:
    """东八区今天(容器 TZ 默认 UTC, 直接用 date.today() 会差 8 小时)。"""
    return datetime.now(_CST).date()


def resolve_range(period: str, end: date | None = None) -> tuple[date, date, str]:
    """把 "week/month/quarter" 解析成左闭右开区间, 返回 (start, end, label)。

    周报按 ISO 周(周一起算); 月报按自然月; 超出的一律按月处理并在 label 里说明。
    """
    today = end or current_cst_date()
    kind = (period or "week").strip().lower()
    if kind in ("week", "weekly", "本周", "周报"):
        start = today - timedelta(days=today.weekday())
        label = f"{start.isoformat()} ~ {(today + timedelta(days=1)).isoformat()}(本周至今)"
    elif kind in ("month", "monthly", "本月", "月报"):
        start = today.replace(day=1)
        label = f"{start.isoformat()} ~ {(today + timedelta(days=1)).isoformat()}(本月至今)"
    else:
        start = today - timedelta(days=90)
        label = f"{start.isoformat()} ~ {(today + timedelta(days=1)).isoformat()}(近90天)"
    # 左闭右开: 上界取"明天零点", 保证今天的数据被包含进来。
    return start, today + timedelta(days=1), label


def _rows(sql: str, params: dict, scope: Any = None) -> list[dict[str, Any]]:
    """跑一条服务端写好的只读指标 SQL; 单条失败返回空列表并留 WARNING。

    逐条降级是有意为之: 某张业务表还没建(新功能刚上线)时, 周报仍应把其余域的
    数字交出去, 而不是整份报告报错。

    ``scope`` 带上时就切到 analytics 只读角色并注入会话作用域(层 0/1); 不带时
    行为与改造前一致(属主连接), 仅给"不属于任何调用者"的后台任务用。
    """
    try:
        from app.db.rls import read_role

        payload = dbsync.execute_readonly_sql(
            sql,
            ALLOWED_TABLES,
            params,
            db_role=read_role(),
            session_settings=scope.session_settings() if scope is not None else None,
        )
        return payload[0]["rows"] if payload else []
    except Exception as exc:  # noqa: BLE001 - 指标级降级(白名单校验/执行失败都只让该域为空)
        logger.warning("report metric query failed (%s): %s", exc, sql[:80])
        return []


def _scalar(rows: list[dict[str, Any]], key: str, default: Any = 0) -> Any:
    return rows[0].get(key, default) if rows else default


def collect_metrics(start: date, end: date, scope: Any = None) -> dict[str, Any]:
    """按区间汇总五类经营指标(费用/预算/服务工单/人力/采购合同)。

    ``scope`` 决定这些数字能被谁看见: 它被带进每一条指标 SQL 的执行事务(切只读角色 +
    注会话作用域), 于是 RLS 与层 1 的隔离对固定口径一样生效。不传 = 属主连接全量,
    只能给无调用者的后台任务用。
    """

    def _metric(sql: str, params: dict) -> list[dict[str, Any]]:
        """本批指标 SQL 的统一出口: 把作用域带到每一条执行里。"""
        return _rows(sql, params, scope)

    win = {"start": start.isoformat(), "end": end.isoformat()}
    year = {"year": start.year}

    expense = _metric(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total, "
        "COALESCE(SUM(amount) FILTER (WHERE status = 'PAID'), 0) AS paid, "
        "COUNT(*) FILTER (WHERE status = 'SUBMITTED') AS pending, "
        "COUNT(*) FILTER (WHERE status = 'REJECTED') AS rejected "
        "FROM fin_reimbursements WHERE created_at >= :start AND created_at < :end",
        win,
    )
    expense_cat = _metric(
        "SELECT category, COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total "
        "FROM fin_reimbursements WHERE created_at >= :start AND created_at < :end "
        "GROUP BY category ORDER BY total DESC",
        win,
    )
    dept_expense = _metric(
        "SELECT e.department, COUNT(*) AS cnt, COALESCE(SUM(r.amount), 0) AS total "
        "FROM fin_reimbursements r JOIN hr_employees e ON r.emp_id = e.emp_id "
        "WHERE r.created_at >= :start AND r.created_at < :end "
        "GROUP BY e.department ORDER BY total DESC",
        win,
    )
    daily = _metric(
        "SELECT to_char(date_trunc('day', created_at), 'MM-DD') AS day, "
        "COALESCE(SUM(amount), 0) AS total "
        "FROM fin_reimbursements WHERE created_at >= :start AND created_at < :end "
        "GROUP BY 1 ORDER BY 1",
        win,
    )
    budget = _metric(
        "SELECT department, annual_budget, used_amount, "
        "ROUND(used_amount / NULLIF(annual_budget, 0) * 100, 1) AS used_pct "
        "FROM fin_department_budgets WHERE year = :year ORDER BY used_pct DESC NULLS LAST",
        year,
    )
    tickets = _metric(
        "SELECT COUNT(*) AS cnt, "
        "COUNT(*) FILTER (WHERE status = 'DONE') AS done, "
        "COUNT(*) FILTER (WHERE status IN ('OPEN', 'PROCESSING')) AS open_cnt "
        "FROM hr_tickets WHERE created_at >= :start AND created_at < :end",
        win,
    )
    ticket_cat = _metric(
        "SELECT category, COUNT(*) AS cnt FROM hr_tickets "
        "WHERE created_at >= :start AND created_at < :end GROUP BY category ORDER BY cnt DESC",
        win,
    )
    leave = _metric(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(days), 0) AS days "
        "FROM hr_leave_records WHERE created_at >= :start AND created_at < :end",
        win,
    )
    purchase = _metric(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total, "
        "COUNT(*) FILTER (WHERE status IN ('PRECHECK', 'PENDING')) AS pending "
        "FROM proc_orders WHERE created_at >= :start AND created_at < :end",
        win,
    )
    contracts = _metric(
        "SELECT COUNT(*) AS cnt, "
        "COUNT(*) FILTER (WHERE risk_level = '高') AS high, "
        "COUNT(*) FILTER (WHERE risk_level = '中') AS medium, "
        "COUNT(*) FILTER (WHERE status = 'RISK') AS risk "
        "FROM proc_contracts WHERE created_at >= :start AND created_at < :end",
        win,
    )
    expiring = _metric(
        "SELECT contract_no, title, party_b, expiry_date, amount "
        "FROM proc_contracts WHERE expiry_date IS NOT NULL "
        "AND expiry_date >= :start AND expiry_date < :end ORDER BY expiry_date",
        {"start": end.isoformat(), "end": (end + timedelta(days=30)).isoformat()},
    )

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "expense": expense[0] if expense else {},
        "expense_by_category": expense_cat,
        "expense_by_department": dept_expense,
        "expense_daily": daily,
        "budget": budget,
        "tickets": tickets[0] if tickets else {},
        "tickets_by_category": ticket_cat,
        "leave": leave[0] if leave else {},
        "purchase": purchase[0] if purchase else {},
        "contracts": contracts[0] if contracts else {},
        "contracts_expiring_30d": expiring,
    }


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    """把指标行渲染成 Markdown 表格; 空数据出一行提示而不是空表。"""
    header = "| " + " | ".join(label for _, label in columns) + " |"
    sep = "|" + "|".join(" --- " for _ in columns) + "|"
    if not rows:
        return header + "\n" + sep + "\n| (本期无数据) |" + " |" * (len(columns) - 1)
    lines = [header, sep]
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            if isinstance(value, float):
                value = f"{value:,.2f}".rstrip("0").rstrip(".")
            cells.append(str(value if value is not None else "-"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_markdown(metrics: dict[str, Any], label: str, charts: list[tuple[str, str]]) -> str:
    """组装周报正文 Markdown。

    Args:
        metrics: :func:`collect_metrics` 的输出。
        label: 统计区间描述(给人类看的口径说明)。
        charts: ``[(标题, 产物文件名), ...]``, 以相对路径引用同目录下的 SVG。
    """
    exp = metrics.get("expense") or {}
    tick = metrics.get("tickets") or {}
    pur = metrics.get("purchase") or {}
    con = metrics.get("contracts") or {}
    leave = metrics.get("leave") or {}

    total = float(exp.get("total") or 0)
    pending = int(exp.get("pending") or 0)
    rejected = int(exp.get("rejected") or 0)
    open_cnt = int(tick.get("open_cnt") or 0)
    high_risk = int(con.get("high") or 0)

    head = [
        f"# 经营周期报告 · {label}",
        "",
        f"> 生成时间(北京时间): {datetime.now(_CST).strftime('%Y-%m-%d %H:%M')} · "
        f"统计区间 {metrics['range']['start']} ~ {metrics['range']['end']}(左闭右开)",
        "",
        "## 一、总览",
        "",
        f"- 报销: {int(exp.get('cnt') or 0)} 单 / {total:,.2f} 元, "
        f"其中待审 {pending} 单、被驳回 {rejected} 单",
        f"- 预算: 最高消耗部门 "
        f"{(metrics.get('budget') or [{}])[0].get('department', '-')} "
        f"{(metrics.get('budget') or [{}])[0].get('used_pct', '-')}%",
        f"- HR 工单: {int(tick.get('cnt') or 0)} 单, 未结 {open_cnt} 单",
        f"- 请假: {int(leave.get('cnt') or 0)} 人次 / {leave.get('days', 0)} 天",
        f"- 采购申请: {int(pur.get('cnt') or 0)} 单 / {float(pur.get('total') or 0):,.2f} 元, "
        f"待审 {int(pur.get('pending') or 0)} 单",
        f"- 合同初审: {int(con.get('cnt') or 0)} 份, 高风险 {high_risk} 份",
    ]
    if charts:
        head += ["", "## 二、图表", ""]
        for title, name in charts:
            head.append(f"### {title}\n\n![{title}]({name})\n")
    head += [
        "## 三、费用明细",
        "",
        "### 按类别",
        "",
        _table(metrics.get("expense_by_category") or [], [("category", "类别"), ("cnt", "笔数"), ("total", "金额")]),
        "",
        "### 按部门",
        "",
        _table(metrics.get("expense_by_department") or [], [("department", "部门"), ("cnt", "笔数"), ("total", "金额")]),
        "",
        "### 部门预算消耗",
        "",
        _table(
            metrics.get("budget") or [],
            [("department", "部门"), ("annual_budget", "年度预算"), ("used_amount", "已用"), ("used_pct", "消耗%")],
        ),
        "",
        "## 四、服务与人力",
        "",
        "### HR 工单类别分布",
        "",
        _table(metrics.get("tickets_by_category") or [], [("category", "类别"), ("cnt", "单数")]),
        "",
        "## 五、采购与合同",
        "",
        "### 30 天内到期合同",
        "",
        _table(
            metrics.get("contracts_expiring_30d") or [],
            [("contract_no", "合同号"), ("title", "标题"), ("party_b", "对手方"), ("expiry_date", "到期日"), ("amount", "金额")],
        ),
        "",
        "## 六、口径说明",
        "",
        "- 所有指标按单据 `created_at` 落在统计区间内计算, 左闭右开;",
        "- 报销金额含全部状态(含待审/驳回), " + "`已打款` 单列, 不与财务实付口径混用;",
        "- 本报告由 Analyst_Agent 自动生成, 数字取自业务库只读查询, 结论段由模型撰写,"
        " 对外披露前需人工复核。",
    ]
    return "\n".join(head) + "\n"
