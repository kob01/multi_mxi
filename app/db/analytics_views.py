"""沙箱专用的只读治理视图: 问数降级链第 3 级(模型直出 SQL)的唯一可查面。

为什么要有这一层: 只读角色 + RLS 只解决"能读到哪些**行**", 不解决"能读到哪些**列**"。
模型自由发挥的一条 SELECT 只要碰到业务表, 机密列(年假额度/收款账号/合同全文)就在结构上
可达, 出口 DLP 打码只能决定"给人看什么", 决定不了"数据库里读过什么"。视图把可查面收敛成
一份投影: 碰不到没暴露的列, 比"读得到再遮掉"硬一个量级(与全仓"取消能力优先于兜底"一致)。

两条必须知道的边界:
1. **视图必须是 ``security_invoker``**。普通视图以**视图属主**的权限执行, 查询者的 RLS
   策略会被完全跳过 —— 那种视图不是隔离, 而是跨部门泄漏的新通道。带 security_invoker 后
   PG 按调用角色检查基表权限并施加行级策略, 视图才只是"列的白名单"。
2. **列集是治理资产, 不是随手投影**。去掉的是"自由 SQL 路径不该碰"的列(机密列 + 内部
   审阅产物 + 服务端维护的作用域/软删列); 保留的列与语义层维度/指标口径对齐, 否则合法
   问法会被自己的防线挡在门外。

本模块是**单一事实源**: 建视图用的 SELECT 与发给模型的字段说明都由 :data:`SANDBOX_VIEW_SPECS`
派生。手写两份(一份 SQL、一份 prompt)必然漂移, 而漂移的后果是模型写出"表里有但视图里没有"
的列, 在数据库侧报错并被当成引擎故障 —— 这类错最难归因。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxViewSpec:
    """一个沙箱视图的治理规格(名称 / 基表 / 暴露列 / 是否过滤软删)。"""

    name: str
    source_table: str
    label: str
    columns: tuple[tuple[str, str], ...]      # (列名, 类型说明)
    soft_delete: bool = False
    note: str = ""

    def column_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)

    def select_sql(self) -> str:
        """建视图的 SELECT 投影。

        软删表必须在视图里过掉 ``is_deleted``: 自由 SQL 的模型不会主动写这个条件,
        而编译器路径是靠 SOFT_DELETE_TABLES 注入的 —— 少了这一道, 沙箱就会把已删行
        算进数(与主链路口径不一致的一类错答)。
        """
        projection = ", ".join(self.column_names())
        where = " WHERE is_deleted = false" if self.soft_delete else ""
        return f"SELECT {projection} FROM {self.source_table}{where}"


# 列集依据: app/db/models.py 的真实列(改表结构时同步这里), 类型说明只用于发给模型。
SANDBOX_VIEW_SPECS: tuple[SandboxViewSpec, ...] = (
    SandboxViewSpec(
        name="v_analytics_employees",
        source_table="hr_employees",
        label="员工主数据",
        columns=(
            ("emp_id", "VARCHAR(32) PK"),
            ("name", "VARCHAR(64)"),
            ("department", "VARCHAR(64)"),
            ("position", "VARCHAR(64)"),
            ("hire_date", "DATE"),
            ("status", "VARCHAR(16)"),
        ),
        soft_delete=False,
        # 年假额度是"问得到就是越权读人"的列, 不进自由 SQL 面(按部门的聚合口径走编译器)。
        # 注: 说明里只说"有什么被拿掉"而**不点列名** —— 发给模型的文本里枚举机密列名,
        # 等于递给注入攻击者一张"哪里值得挖"的地图, 而模型本来根本不知道那些列存在。
        note="个人年假额度等员工个体属性列不在本视图内",
    ),
    SandboxViewSpec(
        name="v_analytics_tickets",
        source_table="hr_tickets",
        label="HR 服务工单",
        columns=(
            ("ticket_no", "VARCHAR(32) PK"),
            ("emp_id", "VARCHAR(32)"),
            ("category", "VARCHAR(32)"),
            ("title", "VARCHAR(255)"),
            ("description", "TEXT"),
            ("status", "VARCHAR(16)"),
            ("created_at", "TIMESTAMPTZ"),
        ),
        soft_delete=True,
    ),
    SandboxViewSpec(
        name="v_analytics_leave_records",
        source_table="hr_leave_records",
        label="请假记录",
        columns=(
            ("id", "BIGINT PK"),
            ("emp_id", "VARCHAR(32)"),
            ("leave_type", "VARCHAR(16)"),
            ("start_date", "DATE"),
            ("end_date", "DATE"),
            ("days", "DECIMAL(5,1)"),
            ("status", "VARCHAR(16)"),
            ("created_at", "TIMESTAMPTZ"),
        ),
        soft_delete=True,
    ),
    SandboxViewSpec(
        name="v_analytics_reimbursements",
        source_table="fin_reimbursements",
        label="报销单",
        columns=(
            ("order_no", "VARCHAR(32) PK"),
            ("emp_id", "VARCHAR(32)"),
            ("title", "VARCHAR(255)"),
            ("amount", "DECIMAL(12,2)"),
            ("category", "VARCHAR(32)"),
            ("reason", "TEXT"),
            ("status", "VARCHAR(16)"),
            ("current_node", "VARCHAR(64)"),
            ("created_at", "TIMESTAMPTZ"),
        ),
        soft_delete=True,
    ),
    SandboxViewSpec(
        name="v_analytics_department_budgets",
        source_table="fin_department_budgets",
        label="部门年度预算",
        columns=(
            ("id", "BIGINT PK"),
            ("department", "VARCHAR(64)"),
            ("year", "INT"),
            ("annual_budget", "DECIMAL(14,2)"),
            ("used_amount", "DECIMAL(14,2)"),
        ),
        soft_delete=False,
        # 基表没有 created_at: 年度快照型, 时间维度只能按 year 走。
        note="年度快照(没有 created_at, 时间条件只能用 year)",
    ),
    SandboxViewSpec(
        name="v_analytics_suppliers",
        source_table="proc_suppliers",
        label="供应商主数据",
        columns=(
            ("supplier_code", "VARCHAR(32) PK"),
            ("name", "VARCHAR(128)"),
            ("category", "VARCHAR(64)"),
            ("qualification", "VARCHAR(32)"),
            ("risk_status", "VARCHAR(16)"),
            ("created_at", "TIMESTAMPTZ"),
        ),
        soft_delete=False,
        # 收款账号属于付款欺诈面, 只在合同初审的内部路径里用。
        note="收款账号类支付信息不在本视图内",
    ),
    SandboxViewSpec(
        name="v_analytics_purchase_orders",
        source_table="proc_orders",
        label="采购申请单",
        columns=(
            ("order_no", "VARCHAR(32) PK"),
            ("emp_id", "VARCHAR(32)"),
            ("department", "VARCHAR(64)"),
            ("title", "VARCHAR(255)"),
            ("category", "VARCHAR(64)"),
            ("amount", "DECIMAL(14,2)"),
            ("currency", "VARCHAR(8)"),
            ("supplier_name", "VARCHAR(128)"),
            ("quotes_count", "INT"),
            ("budget_year", "INT"),
            ("reason", "TEXT"),
            ("status", "VARCHAR(16)"),
            ("current_node", "VARCHAR(64)"),
            ("created_at", "TIMESTAMPTZ"),
        ),
        soft_delete=True,
        note="初审内部结论与风险标记不在本视图内",
    ),
    SandboxViewSpec(
        name="v_analytics_contracts",
        source_table="proc_contracts",
        label="合同台账",
        columns=(
            ("contract_no", "VARCHAR(32) PK"),
            ("title", "VARCHAR(255)"),
            ("party_a", "VARCHAR(128)"),
            ("party_b", "VARCHAR(128)"),
            ("category", "VARCHAR(64)"),
            ("amount", "DECIMAL(14,2)"),
            ("currency", "VARCHAR(8)"),
            ("sign_date", "DATE"),
            ("effective_date", "DATE"),
            ("expiry_date", "DATE"),
            ("status", "VARCHAR(16)"),
            ("risk_level", "VARCHAR(16)"),
            ("reviewer", "VARCHAR(64)"),
            ("created_at", "TIMESTAMPTZ"),
        ),
        soft_delete=True,
        note="送审全文与审阅过程产物不在本视图内",
    ),
)


def sandbox_view_names() -> set[str]:
    """沙箱允许引用的关系名集合(交给 AST 校验当表白名单)。"""
    return {spec.name for spec in SANDBOX_VIEW_SPECS}


def source_tables() -> set[str]:
    """视图背后的基表集合(init 时的自校验对象: 必须是受 RLS 管辖的业务表)。"""
    return {spec.source_table for spec in SANDBOX_VIEW_SPECS}


def view_selects() -> dict[str, str]:
    """视图名 -> 建视图的 SELECT(rls 层建视图用; 排序由调用方负责幂等)。"""
    return {spec.name: spec.select_sql() for spec in SANDBOX_VIEW_SPECS}


def sensitive_columns() -> dict[str, tuple[str, ...]]:
    """视图名 -> 该基表被刻意排除的列名(仅用于服务端自检与离线断言)。

    这份名单**不进 prompt**: 它的用途是"确认视图真的没露这些列", 而不是告诉模型有哪些列。
    角色级禁用列仍然以语义层 col_policies 为真值, 这里只是结构层的底牌。
    """
    out: dict[str, tuple[str, ...]] = {}
    for spec in SANDBOX_VIEW_SPECS:
        exposed = set(spec.column_names())
        hidden = {
            "v_analytics_employees": ("annual_leave_total", "annual_leave_used"),
            "v_analytics_suppliers": ("bank_account",),
            "v_analytics_purchase_orders": ("precheck_result", "flags"),
            "v_analytics_contracts": ("content", "findings", "review_json", "opinion", "doc_key"),
        }.get(spec.name, ())
        out[spec.name] = tuple(c for c in hidden if c not in exposed)
    return out
