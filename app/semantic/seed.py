"""语义层内置集(Top 指标 / 维度 / 说法别名)。

这份文件承担两个角色, 缺一不可:

1. **灌库源**: ``init_schema`` 在 ``sem_metrics`` 为空时把这里的内容写进库(幂等);
2. **兜底集**: 数据库不可用时, :func:`builtin_catalog` 直接给出同一份口径 ——
   语义层不会因为库抖动就"退化成没有口径", 那是问数最坏的失效形态(算什么都敢说)。

三条刻意的约束:

- **口径表达式只写字面量, 不带表别名**: 别名由编译器在渲染时统一注入
  (见 app.semantic.compiler 的列限定), 这样同一份常量既能给"单表查询"用也能给
  "JOIN 员工表取部门"用, 不会出现"表达式写死了 r. 但这次没 JOIN"的错。
- **与周报重叠的指标必须带 ``caliber_ref``**, 表达式逐字取
  :data:`app.analytics.reports.CALIBER_SQL` —— 离线测试断言两侧相等, 防"问数与周报
  两个数对不上账"。
- **``compatible_dims`` 是白名单而不是提示**: 编译器遇到名单外维度直接拒,
  而不是"试试看能不能 JOIN"。
"""

from __future__ import annotations

from typing import Any

from app.analytics.reports import (
    CALIBER_BUDGET_USED_PCT_AGG,
    CALIBER_CONTRACT_COUNT,
    CALIBER_CONTRACT_HIGH,
    CALIBER_CONTRACT_MEDIUM,
    CALIBER_CONTRACT_RISK,
    CALIBER_EXPENSE_COUNT,
    CALIBER_EXPENSE_PAID,
    CALIBER_EXPENSE_PENDING,
    CALIBER_EXPENSE_REJECTED,
    CALIBER_EXPENSE_TOTAL,
    CALIBER_LEAVE_COUNT,
    CALIBER_LEAVE_DAYS,
    CALIBER_PURCHASE_COUNT,
    CALIBER_PURCHASE_PENDING,
    CALIBER_PURCHASE_TOTAL,
    CALIBER_TICKET_COUNT,
    CALIBER_TICKET_DONE,
    CALIBER_TICKET_OPEN,
)
from app.semantic.catalog import Dimension, Metric, MetricCatalog, Term

# 事实表的规范别名(与 app/analytics/reports.py 里已有的 JOIN 写法保持一致)。
TABLE_ALIAS: dict[str, str] = {
    "fin_reimbursements": "r",
    "hr_tickets": "t",
    "hr_leave_records": "l",
    "fin_department_budgets": "b",
    "proc_orders": "o",
    "proc_contracts": "c",
    "proc_suppliers": "s",
    "hr_employees": "e",
}

# 带软删列的表: 问数默认排除软删行(写通道只软删, 见 app/db/policy.py)。
# 注意这与周报的既有 SQL 有意不同 —— 周报写于软删通道上线前, 未带该谓词;
# 库里存在软删行时两边会出现可解释的差(问数更少), 口径说明里会明写。
SOFT_DELETE_TABLES: frozenset[str] = frozenset(
    {"hr_tickets", "hr_leave_records", "fin_reimbursements", "proc_orders", "proc_contracts"}
)

# 部门维度需要的 JOIN(工单/请假/报销都靠提单人一跳)。
_JOIN_EMP = "JOIN hr_employees e ON {alias}.emp_id = e.emp_id"


def _m(
    metric_id: str,
    label: str,
    domain: str,
    unit: str,
    agg: str,
    table: str,
    expression: str,
    dims: list[str],
    *,
    caliber_ref: str = "",
    where: str = "",
    time_column: str = "created_at",
    calendar: str = "month",
    desc: str = "",
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "label": label,
        "domain": domain,
        "unit": unit,
        "agg": agg,
        "source_table": table,
        "expression": expression,
        "caliber_where": where,
        "join_sql": "",
        "time_column": time_column,
        "compatible_dims": dims,
        "calendar": calendar,
        "allowed_roles": "",
        "caliber_ref": f"reports:{caliber_ref}" if caliber_ref else "",
        "description": desc,
        "status": "active",
    }


def _d(
    dim_id: str,
    label: str,
    table: str,
    column: str,
    enum: list[str] | None = None,
    *,
    join_emp: bool = False,
    enum_source: str = "",
    desc: str = "",
) -> dict[str, Any]:
    alias = TABLE_ALIAS[table]
    prefix = "e" if join_emp else alias
    return {
        "dim_id": dim_id,
        "label": label,
        "source_table": table,
        "column": column,
        # join_sql 里的 {alias} 由编译器换成当前事实表别名。
        "join_sql": _JOIN_EMP.format(alias=alias) if join_emp else "",
        "column_prefix": prefix,
        "enum_source": enum_source or ("static" if enum else "column"),
        "enum_values": enum or [],
        "time_like": False,
        "description": desc,
        "status": "active",
    }


# ---------------------------------------------------------------------------
# 维度(维度身份 = 概念 + 落在哪张表, 所以"部门"在不同事实表上是不同的 dim_id)
# ---------------------------------------------------------------------------
TOP_DIMS: list[dict[str, Any]] = [
    _d("d_expense_department", "部门", "fin_reimbursements", "department", join_emp=True),
    _d("d_ticket_department", "部门", "hr_tickets", "department", join_emp=True),
    _d("d_leave_department", "部门", "hr_leave_records", "department", join_emp=True),
    _d("d_order_department", "部门", "proc_orders", "department",
       enum_source="column:hr_employees.department"),
    _d("d_budget_department", "部门", "fin_department_budgets", "department",
       enum_source="column:hr_employees.department"),
    _d("d_employee_department", "部门", "hr_employees", "department",
       enum_source="column:hr_employees.department"),
    _d("d_expense_category", "费用类别", "fin_reimbursements", "category",
       ["差旅费", "交通费", "餐饮费", "办公用品", "培训费"]),
    _d("d_ticket_category", "工单类别", "hr_tickets", "category",
       ["入职", "离职", "考勤", "薪酬", "证明开具", "其他"]),
    _d("d_order_category", "采购类别", "proc_orders", "category",
       ["IT设备", "办公用品", "咨询服务", "市场推广", "其他"]),
    _d("d_contract_category", "合同类别", "proc_contracts", "category",
       ["采购", "服务", "框架协议", "劳动", "保密"]),
    _d("d_supplier_category", "供应商类别", "proc_suppliers", "category",
       ["IT设备", "办公用品", "咨询服务", "市场推广"]),
    _d("d_expense_status", "报销状态", "fin_reimbursements", "status",
       ["SUBMITTED", "APPROVED", "REJECTED", "PAID"]),
    _d("d_ticket_status", "工单状态", "hr_tickets", "status",
       ["OPEN", "PROCESSING", "DONE", "CANCELLED"]),
    _d("d_leave_status", "请假审批状态", "hr_leave_records", "status",
       ["审批中", "已批准", "已驳回"]),
    _d("d_order_status", "采购单状态", "proc_orders", "status",
       ["DRAFT", "PRECHECK", "PENDING", "APPROVED", "REJECTED", "PAID"]),
    _d("d_contract_status", "合同状态", "proc_contracts", "status",
       ["DRAFT", "PRECHECKED", "APPROVED", "RISK", "REJECTED"]),
    _d("d_leave_type", "请假类型", "hr_leave_records", "leave_type",
       ["年假", "事假", "病假", "调休", "婚假"]),
    _d("d_contract_risk_level", "合同风险等级", "proc_contracts", "risk_level", ["低", "中", "高"]),
    _d("d_supplier_risk_status", "供应商风险状态", "proc_suppliers", "risk_status",
       ["正常", "关注", "黑名单"]),
    _d("d_supplier_qualification", "供应商资质", "proc_suppliers", "qualification",
       ["一般纳税人", "小规模", "个体"]),
    _d("d_order_supplier", "供应商", "proc_orders", "supplier_name", enum_source="column:proc_suppliers.name"),
    _d("d_contract_party_b", "合同对手方", "proc_contracts", "party_b",
       enum_source="column:proc_suppliers.name"),
    _d("d_supplier_name", "供应商名称", "proc_suppliers", "name", enum_source="column:proc_suppliers.name"),
    _d("d_employee_position", "职位", "hr_employees", "position"),
    _d("d_employee_name", "员工", "hr_employees", "name"),
    # 员工姓名/职位也会被报销/工单/请假类指标下钻使用, 但这三张事实表上没有 name/position 列:
    # 它们必须按"事实表 + 需要 JOIN 员工主数据"各登记一份(与部门维度同一形状)。
    # 拿 hr_employees 那两条直接用在报销指标上会生成 `e.name` 但没有任何 JOIN, PG 直接报
    # "missing FROM-clause entry for table e"(实际被语义缓存+多维评测抖出来)。
    _d("d_expense_employee_name", "员工", "fin_reimbursements", "name", join_emp=True),
    _d("d_expense_employee_position", "职位", "fin_reimbursements", "position", join_emp=True),
    _d("d_ticket_employee_name", "员工", "hr_tickets", "name", join_emp=True),
    _d("d_ticket_employee_position", "职位", "hr_tickets", "position", join_emp=True),
    _d("d_leave_employee_name", "员工", "hr_leave_records", "name", join_emp=True),
    _d("d_leave_employee_position", "职位", "hr_leave_records", "position", join_emp=True),
    _d("d_order_employee_name", "员工", "proc_orders", "name", join_emp=True),
    _d("d_order_employee_position", "职位", "proc_orders", "position", join_emp=True),
    _d("d_employee_status", "在职状态", "hr_employees", "status", ["在职", "离职"]),
    _d("d_budget_year", "预算年度", "fin_department_budgets", "year"),
    _d("d_order_budget_year", "采购预算年度", "proc_orders", "budget_year"),
    _d("d_reimb_node", "报销审批节点", "fin_reimbursements", "current_node"),
    _d("d_order_node", "采购审批节点", "proc_orders", "current_node"),
    _d("d_contract_reviewer", "合同审阅人", "proc_contracts", "reviewer"),
    _d("d_order_currency", "币种", "proc_orders", "currency", ["CNY", "USD"]),
    _d("d_contract_currency", "合同币种", "proc_contracts", "currency", ["CNY", "USD"]),
]

# ---------------------------------------------------------------------------
# 指标(Top 52)
# ---------------------------------------------------------------------------
_EXPENSE_DIMS = ["d_expense_department", "d_expense_category", "d_expense_status",
                 "d_reimb_node", "d_expense_employee_name", "d_expense_employee_position"]
_TICKET_DIMS = ["d_ticket_department", "d_ticket_category", "d_ticket_status",
                "d_ticket_employee_name", "d_ticket_employee_position"]
_LEAVE_DIMS = ["d_leave_department", "d_leave_type", "d_leave_status",
               "d_leave_employee_name", "d_leave_employee_position"]
_ORDER_DIMS = ["d_order_department", "d_order_category", "d_order_status", "d_order_supplier",
               "d_order_node", "d_order_budget_year", "d_order_currency",
               "d_order_employee_name"]
_CONTRACT_DIMS = ["d_contract_category", "d_contract_status", "d_contract_risk_level",
                  "d_contract_party_b", "d_contract_reviewer", "d_contract_currency"]
_SUPPLIER_DIMS = ["d_supplier_category", "d_supplier_risk_status", "d_supplier_qualification",
                  "d_supplier_name"]
_EMP_DIMS = ["d_employee_department", "d_employee_position", "d_employee_name", "d_employee_status"]
_BUDGET_DIMS = ["d_budget_department", "d_budget_year"]

TOP_METRICS: list[dict[str, Any]] = [
    # ---------- 报销/费用 ----------
    _m("m_expense_total", "报销总额", "expense", "元", "sum", "fin_reimbursements",
       CALIBER_EXPENSE_TOTAL, _EXPENSE_DIMS, caliber_ref="expense_total",
       desc="全部状态报销单的金额合计(含待审与驳回)"),
    _m("m_expense_count", "报销单数", "expense", "单", "count", "fin_reimbursements",
       CALIBER_EXPENSE_COUNT, _EXPENSE_DIMS, caliber_ref="expense_count"),
    _m("m_expense_paid_total", "已打款报销金额", "expense", "元", "sum", "fin_reimbursements",
       CALIBER_EXPENSE_PAID, _EXPENSE_DIMS, caliber_ref="expense_paid"),
    _m("m_expense_pending_count", "待审报销单数", "expense", "单", "count", "fin_reimbursements",
       CALIBER_EXPENSE_PENDING, _EXPENSE_DIMS, caliber_ref="expense_pending"),
    _m("m_expense_rejected_count", "被驳回报销单数", "expense", "单", "count", "fin_reimbursements",
       CALIBER_EXPENSE_REJECTED, _EXPENSE_DIMS, caliber_ref="expense_rejected"),
    _m("m_expense_avg_amount", "平均单笔报销额", "expense", "元", "avg", "fin_reimbursements",
       "COALESCE(AVG(amount), 0)", _EXPENSE_DIMS),
    _m("m_expense_max_amount", "单笔最高报销额", "expense", "元", "max", "fin_reimbursements",
       "COALESCE(MAX(amount), 0)", _EXPENSE_DIMS),
    _m("m_expense_reject_rate", "报销驳回率", "expense", "%", "ratio", "fin_reimbursements",
       "ROUND(COUNT(*) FILTER (WHERE status = 'REJECTED') * 100.0 / NULLIF(COUNT(*), 0), 1)",
       _EXPENSE_DIMS),
    _m("m_expense_travel_total", "差旅费金额", "expense", "元", "sum", "fin_reimbursements",
       CALIBER_EXPENSE_TOTAL, _EXPENSE_DIMS, caliber_ref="expense_total",
       where="category = '差旅费'"),
    _m("m_expense_person_count", "报销涉及人数", "expense", "人", "count_distinct",
       "fin_reimbursements", "COUNT(DISTINCT emp_id)", _EXPENSE_DIMS),
    _m("m_expense_per_capita", "人均报销额", "expense", "元", "ratio", "fin_reimbursements",
       "ROUND(COALESCE(SUM(amount), 0) / NULLIF(COUNT(DISTINCT emp_id), 0), 2)", _EXPENSE_DIMS),
    # ---------- 预算 ----------
    _m("m_budget_annual_total", "年度预算总额", "budget", "元", "sum", "fin_department_budgets",
       "COALESCE(SUM(annual_budget), 0)", _BUDGET_DIMS, time_column="", calendar="year",
       desc="按年度快照, 不受时间区间约束"),
    _m("m_budget_used_total", "已用预算总额", "budget", "元", "sum", "fin_department_budgets",
       "COALESCE(SUM(used_amount), 0)", _BUDGET_DIMS, time_column="", calendar="year"),
    _m("m_budget_remaining", "预算剩余额", "budget", "元", "expression",
       "fin_department_budgets", "COALESCE(SUM(annual_budget) - SUM(used_amount), 0)",
       _BUDGET_DIMS, time_column="", calendar="year"),
    _m("m_budget_used_pct", "预算消耗率", "budget", "%", "ratio", "fin_department_budgets",
       CALIBER_BUDGET_USED_PCT_AGG, _BUDGET_DIMS, caliber_ref="budget_used_pct_agg",
       time_column="", calendar="year",
       desc="按分组聚合后的消耗占比(与周报行级口径同值)"),
    _m("m_budget_over_count", "超支部门数", "budget", "个", "count", "fin_department_budgets",
       "COUNT(*) FILTER (WHERE used_amount > annual_budget)", _BUDGET_DIMS,
       time_column="", calendar="year"),
    # ---------- HR 工单 ----------
    _m("m_ticket_count", "工单总量", "hr", "单", "count", "hr_tickets",
       CALIBER_TICKET_COUNT, _TICKET_DIMS, caliber_ref="ticket_count"),
    _m("m_ticket_done_count", "已结工单数", "hr", "单", "count", "hr_tickets",
       CALIBER_TICKET_DONE, _TICKET_DIMS, caliber_ref="ticket_done"),
    _m("m_ticket_open_count", "未结工单数", "hr", "单", "count", "hr_tickets",
       CALIBER_TICKET_OPEN, _TICKET_DIMS, caliber_ref="ticket_open"),
    _m("m_ticket_onboard_count", "入职类工单数", "hr", "单", "count", "hr_tickets",
       "COUNT(*) FILTER (WHERE category = '入职')", _TICKET_DIMS),
    _m("m_ticket_cert_count", "证明开具类工单数", "hr", "单", "count", "hr_tickets",
       "COUNT(*) FILTER (WHERE category = '证明开具')", _TICKET_DIMS),
    _m("m_ticket_person_count", "提单人数", "hr", "人", "count_distinct", "hr_tickets",
       "COUNT(DISTINCT emp_id)", _TICKET_DIMS),
    # ---------- 请假 ----------
    _m("m_leave_count", "请假人次", "leave", "人次", "count", "hr_leave_records",
       CALIBER_LEAVE_COUNT, _LEAVE_DIMS, caliber_ref="leave_count"),
    _m("m_leave_days_total", "请假天数合计", "leave", "天", "sum", "hr_leave_records",
       CALIBER_LEAVE_DAYS, _LEAVE_DIMS, caliber_ref="leave_days"),
    _m("m_leave_avg_days", "平均单次请假天数", "leave", "天", "avg", "hr_leave_records",
       "COALESCE(AVG(days), 0)", _LEAVE_DIMS),
    _m("m_leave_annual_days", "年假天数", "leave", "天", "sum", "hr_leave_records",
       CALIBER_LEAVE_DAYS, _LEAVE_DIMS, caliber_ref="leave_days",
       where="leave_type = '年假'"),
    _m("m_leave_sick_days", "病假天数", "leave", "天", "sum", "hr_leave_records",
       CALIBER_LEAVE_DAYS, _LEAVE_DIMS, caliber_ref="leave_days",
       where="leave_type = '病假'"),
    _m("m_leave_approved_days", "已批准请假天数", "leave", "天", "sum", "hr_leave_records",
       CALIBER_LEAVE_DAYS, _LEAVE_DIMS, caliber_ref="leave_days",
       where="status = '已批准'"),
    # ---------- 员工主数据 ----------
    _m("m_employee_headcount", "在职人数", "employee", "人", "count", "hr_employees",
       "COUNT(*) FILTER (WHERE status = '在职')", _EMP_DIMS,
       time_column="", calendar="year", desc="人数快照, 不受时间区间约束"),
    _m("m_employee_onboard_count", "新入职人数", "employee", "人", "count", "hr_employees",
       CALIBER_EXPENSE_COUNT, _EMP_DIMS, caliber_ref="expense_count",
       time_column="hire_date", desc="按 hire_date 落在区间内计数"),
    _m("m_employee_left_count", "离职人数", "employee", "人", "count", "hr_employees",
       "COUNT(*) FILTER (WHERE status = '离职')", _EMP_DIMS, time_column="hire_date"),
    _m("m_employee_leave_balance_total", "年假余额合计", "employee", "天", "expression",
       "hr_employees", "COALESCE(SUM(GREATEST(annual_leave_total - annual_leave_used, 0)), 0)",
       _EMP_DIMS, time_column="", calendar="year"),
    _m("m_employee_avg_leave_used", "人均已用年假", "employee", "天", "avg", "hr_employees",
       "COALESCE(AVG(annual_leave_used), 0)", _EMP_DIMS, time_column=""),
    # ---------- 采购 ----------
    _m("m_purchase_total", "采购申请金额", "purchase", "元", "sum", "proc_orders",
       CALIBER_PURCHASE_TOTAL, _ORDER_DIMS, caliber_ref="purchase_total"),
    _m("m_purchase_count", "采购申请单数", "purchase", "单", "count", "proc_orders",
       CALIBER_PURCHASE_COUNT, _ORDER_DIMS, caliber_ref="purchase_count"),
    _m("m_purchase_pending_count", "待审采购单数", "purchase", "单", "count", "proc_orders",
       CALIBER_PURCHASE_PENDING, _ORDER_DIMS, caliber_ref="purchase_pending"),
    _m("m_purchase_approved_total", "已批准采购金额", "purchase", "元", "sum", "proc_orders",
       CALIBER_PURCHASE_TOTAL, _ORDER_DIMS, caliber_ref="purchase_total",
       where="status IN ('APPROVED', 'PAID')"),
    _m("m_purchase_avg_amount", "平均单笔采购额", "purchase", "元", "avg", "proc_orders",
       "COALESCE(AVG(amount), 0)", _ORDER_DIMS),
    _m("m_purchase_quotes_short", "比价不足采购单数", "purchase", "单", "count", "proc_orders",
       "COUNT(*) FILTER (WHERE quotes_count < 3)", _ORDER_DIMS,
       desc="比价份数不足 3 份, 合规红线项"),
    _m("m_purchase_supplier_count", "涉及供应商数", "purchase", "家", "count_distinct",
       "proc_orders", "COUNT(DISTINCT supplier_name)", _ORDER_DIMS),
    # ---------- 合同 ----------
    _m("m_contract_count", "合同初审份数", "contract", "份", "count", "proc_contracts",
       CALIBER_CONTRACT_COUNT, _CONTRACT_DIMS, caliber_ref="contract_count"),
    _m("m_contract_amount_total", "合同金额合计", "contract", "元", "sum", "proc_contracts",
       CALIBER_PURCHASE_TOTAL, _CONTRACT_DIMS, caliber_ref="purchase_total"),
    _m("m_contract_high_risk_count", "高风险合同数", "contract", "份", "count", "proc_contracts",
       CALIBER_CONTRACT_HIGH, _CONTRACT_DIMS, caliber_ref="contract_high"),
    _m("m_contract_medium_risk_count", "中风险合同数", "contract", "份", "count",
       "proc_contracts", CALIBER_CONTRACT_MEDIUM, _CONTRACT_DIMS, caliber_ref="contract_medium"),
    _m("m_contract_risk_flag_count", "判定为有风险的合同数", "contract", "份", "count",
       "proc_contracts", CALIBER_CONTRACT_RISK, _CONTRACT_DIMS, caliber_ref="contract_risk"),
    _m("m_contract_avg_amount", "平均合同金额", "contract", "元", "avg", "proc_contracts",
       "COALESCE(AVG(amount), 0)", _CONTRACT_DIMS),
    _m("m_contract_expiring_count", "区间内到期合同数", "contract", "份", "count",
       "proc_contracts", CALIBER_CONTRACT_COUNT, _CONTRACT_DIMS, caliber_ref="contract_count",
       time_column="expiry_date", where="expiry_date IS NOT NULL",
       desc="按到期日落在区间内计数(未来 30 天 = 区间取到 30 天后)"),
    _m("m_contract_party_count", "合同对手方数", "contract", "家", "count_distinct",
       "proc_contracts", "COUNT(DISTINCT party_b)", _CONTRACT_DIMS),
    # ---------- 供应商 ----------
    _m("m_supplier_count", "在册供应商数", "supplier", "家", "count", "proc_suppliers",
       CALIBER_EXPENSE_COUNT, _SUPPLIER_DIMS, caliber_ref="expense_count"),
    _m("m_supplier_blacklist_count", "黑名单供应商数", "supplier", "家", "count",
       "proc_suppliers", "COUNT(*) FILTER (WHERE risk_status = '黑名单')", _SUPPLIER_DIMS),
    _m("m_supplier_watch_count", "关注名单供应商数", "supplier", "家", "count",
       "proc_suppliers", "COUNT(*) FILTER (WHERE risk_status = '关注')", _SUPPLIER_DIMS),
    _m("m_supplier_general_taxpayer_count", "一般纳税人供应商数", "supplier", "家", "count",
       "proc_suppliers", "COUNT(*) FILTER (WHERE qualification = '一般纳税人')", _SUPPLIER_DIMS),
]

# ---------------------------------------------------------------------------
# 说法别名(业务黑话/表头简称): 同义词精确匹配 + 向量召回的语料
# ---------------------------------------------------------------------------
TERM_ALIASES: list[tuple[str, str, str, float]] = [
    # (说法, 类型, 目标 id, 权重)
    ("报销总额", "metric", "m_expense_total", 1.0),
    ("报销金额", "metric", "m_expense_total", 1.0),
    ("花了多少", "metric", "m_expense_total", 0.95),
    ("报了多少", "metric", "m_expense_total", 0.95),
    ("费用总额", "metric", "m_expense_total", 0.9),
    ("费用合计", "metric", "m_expense_total", 0.9),
    ("报销笔数", "metric", "m_expense_count", 1.0),
    ("报销单量", "metric", "m_expense_count", 0.95),
    ("多少单报销", "metric", "m_expense_count", 0.9),
    ("已打款", "metric", "m_expense_paid_total", 1.0),
    ("到账金额", "metric", "m_expense_paid_total", 0.95),
    ("待审批报销", "metric", "m_expense_pending_count", 1.0),
    ("卡着的报销", "metric", "m_expense_pending_count", 0.85),
    ("驳回", "metric", "m_expense_rejected_count", 0.9),
    ("报销驳回率", "metric", "m_expense_reject_rate", 1.0),
    ("差旅费", "metric", "m_expense_travel_total", 0.9),
    ("平均报销", "metric", "m_expense_avg_amount", 1.0),
    ("人均报销", "metric", "m_expense_per_capita", 1.0),
    ("预算总额", "metric", "m_budget_annual_total", 1.0),
    ("年度预算", "metric", "m_budget_annual_total", 0.95),
    ("预算用了多少", "metric", "m_budget_used_total", 1.0),
    ("预算剩余", "metric", "m_budget_remaining", 1.0),
    ("还有多少预算", "metric", "m_budget_remaining", 0.95),
    ("预算消耗率", "metric", "m_budget_used_pct", 1.0),
    ("预算用超", "metric", "m_budget_over_count", 1.0),
    ("超支", "metric", "m_budget_over_count", 0.95),
    ("工单量", "metric", "m_ticket_count", 1.0),
    ("提了多少单", "metric", "m_ticket_count", 0.9),
    ("工单完成了多少", "metric", "m_ticket_done_count", 1.0),
    ("未结工单", "metric", "m_ticket_open_count", 1.0),
    ("工单积压", "metric", "m_ticket_open_count", 0.9),
    ("入职工单", "metric", "m_ticket_onboard_count", 1.0),
    ("在职证明工单", "metric", "m_ticket_cert_count", 0.95),
    ("请了多少假", "metric", "m_leave_days_total", 1.0),
    ("请假天数", "metric", "m_leave_days_total", 1.0),
    ("请假人次", "metric", "m_leave_count", 1.0),
    ("年假用了几天", "metric", "m_leave_annual_days", 1.0),
    ("病假", "metric", "m_leave_sick_days", 0.9),
    ("年假余额", "metric", "m_employee_leave_balance_total", 1.0),
    ("还有多少年假", "metric", "m_employee_leave_balance_total", 0.95),
    ("在职人数", "metric", "m_employee_headcount", 1.0),
    ("现有员工", "metric", "m_employee_headcount", 0.9),
    ("入职了多少人", "metric", "m_employee_onboard_count", 1.0),
    ("招了多少人", "metric", "m_employee_onboard_count", 0.9),
    ("离职人数", "metric", "m_employee_left_count", 1.0),
    ("采购金额", "metric", "m_purchase_total", 1.0),
    ("买了多少钱", "metric", "m_purchase_total", 0.95),
    ("采购单数", "metric", "m_purchase_count", 1.0),
    ("采购待审", "metric", "m_purchase_pending_count", 1.0),
    ("比价不够", "metric", "m_purchase_quotes_short", 1.0),
    ("三家比价", "metric", "m_purchase_quotes_short", 0.9),
    ("合同份数", "metric", "m_contract_count", 1.0),
    ("签了多少合同", "metric", "m_contract_count", 0.95),
    ("合同金额", "metric", "m_contract_amount_total", 1.0),
    ("高风险合同", "metric", "m_contract_high_risk_count", 1.0),
    ("有问题的合同", "metric", "m_contract_risk_flag_count", 0.95),
    ("合同到期", "metric", "m_contract_expiring_count", 1.0),
    ("要续签的合同", "metric", "m_contract_expiring_count", 0.9),
    ("供应商数量", "metric", "m_supplier_count", 1.0),
    ("黑名单供应商", "metric", "m_supplier_blacklist_count", 1.0),
    ("关注供应商", "metric", "m_supplier_watch_count", 1.0),
    # 维度说法
    ("部门", "dim", "d_expense_department", 1.0),
    ("部门", "dim", "d_ticket_department", 0.9),
    ("部门", "dim", "d_order_department", 0.9),
    ("部门", "dim", "d_budget_department", 0.9),
    ("部门", "dim", "d_employee_department", 0.9),
    ("部门", "dim", "d_leave_department", 0.9),
    ("类别", "dim", "d_expense_category", 1.0),
    ("费用类型", "dim", "d_expense_category", 0.95),
    ("品类", "dim", "d_order_category", 0.9),
    ("状态", "dim", "d_expense_status", 1.0),
    ("审批状态", "dim", "d_order_status", 0.9),
    ("供应商", "dim", "d_order_supplier", 1.0),
    ("乙方", "dim", "d_contract_party_b", 1.0),
    ("对手方", "dim", "d_contract_party_b", 0.95),
    ("风险等级", "dim", "d_contract_risk_level", 1.0),
    ("请假类型", "dim", "d_leave_type", 1.0),
    ("职位", "dim", "d_employee_position", 1.0),
    ("岗位", "dim", "d_employee_position", 0.95),
    ("年度", "dim", "d_budget_year", 1.0),
    ("年份", "dim", "d_budget_year", 0.95),
    ("审阅人", "dim", "d_contract_reviewer", 1.0),
    ("币种", "dim", "d_order_currency", 1.0),
]

# 部门维度的取值候选(靠库反查, 兜底集里只放常见说法)。
ENUM_HINTS: dict[str, list[str]] = {
    "d_expense_department": ["研发部", "市场部", "人事部", "财务部"],
    "d_ticket_department": ["研发部", "市场部", "人事部", "财务部"],
    "d_leave_department": ["研发部", "市场部", "人事部", "财务部"],
    "d_order_department": ["研发部", "市场部", "人事部", "财务部"],
    "d_budget_department": ["研发部", "市场部", "人事部", "财务部"],
    "d_employee_department": ["研发部", "市场部", "人事部", "财务部"],
}


def build_catalog(origin: str = "seed", built_at: float = 0.0) -> MetricCatalog:
    """由内置集构造目录(兜底路径; 库可用时 registry 走同一个构造器)。

    部门维度的取值在内置集里只有 :data:`ENUM_HINTS` 这一小份样本(供 prompt 回显与
    “研发部”这类说法的枚举反查); 真实部门集由 link 层按调用者作用域从库里反查 ——
    不把全量部门名单烧进代码, 因为它是会变的业务主数据。
    """
    dims: dict[str, Dimension] = {}
    for raw in TOP_DIMS:
        item = dict(raw)
        if not item.get("enum_values"):
            item["enum_values"] = list(ENUM_HINTS.get(item["dim_id"], []))
        dims[item["dim_id"]] = Dimension(**item)
    return MetricCatalog(
        metrics={m["metric_id"]: Metric(**m) for m in TOP_METRICS},
        dimensions=dims,
        terms=tuple(
            Term(term=t, term_type=kind, target_id=target, weight=w, source="seed")
            for t, kind, target, w in TERM_ALIASES
        ),
        origin=origin,
        built_at=built_at,
    )
