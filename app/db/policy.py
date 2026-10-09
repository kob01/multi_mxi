"""实体×字段白名单与高危表黑名单(层 2 的"名单", 层 4-6 的"无论如何不给写")。

模型交上来的 DSL 里有两处必须查名单的地方, 都在这里:

1. ``entity``: 不在 :data:`WRITE_ENTITIES` 里直接拒 —— 默认拒而不是默认放行, 所以
   "将来新表忘了登记"的后果是写不了(可发现), 不是能写(不可发现)。
2. ``filters[].field`` 与 ``sets[].field``: 逐个查该实体的可写字段集合。列的真实集合
   以 ORM 元数据为准(:func:`table_columns`), 名单只登记"其中允许被写的那一部分",
   这样加列时不会漏改这里, 也不会有"名单里写着早就不存在的列"。

为什么标识符不能直接来自模型输出的字符串: PG 的 ``%()s`` 只绑值不绑标识符, 模板必须
把列名写进 SQL 文本。于是"列名"这个自由度只能被收成"来自固定字典的键", 拼进文本前
再过一次标识符正则 —— 三层都成立才可能拼出 SQL。
"""

from __future__ import annotations

import re

from app.db.models import Base

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class PolicyError(ValueError):
    """DSL 触到了名单外的实体/字段。"""


def table_columns(table: str) -> frozenset[str]:
    """某张业务表当前的列集合(单一事实来源 = ORM 元数据)。"""
    if table not in Base.metadata.tables:
        raise PolicyError(f"未知表 {table}")
    return frozenset(Base.metadata.tables[table].columns.keys())


def checked_ident(name: str) -> str:
    """标识符白名单校验(列名/表名在拼进 SQL 文本前的最后一道)。"""
    if not _IDENT_RE.match(name or ""):
        raise PolicyError(f"非法标识符: {name!r}")
    return name


class WriteSpec:
    """一个实体的写权限形状。"""

    __slots__ = ("entity", "table", "label", "pk_columns", "writable_fields",
                 "filter_fields", "soft_delete", "insertable")

    def __init__(
        self,
        entity: str,
        table: str,
        label: str,
        pk_columns: tuple[str, ...],
        writable_fields: set[str],
        filter_fields: set[str] | None = None,
        soft_delete: bool = True,
        insertable: bool = False,
    ) -> None:
        self.entity = entity
        self.table = checked_ident(table)
        self.label = label
        self.pk_columns = tuple(checked_ident(c) for c in pk_columns)
        # 与 ORM 实际列对账: 名单里写了不存在的列 = 配置漂移, 启动时就该炸掉而不是
        # 等到用户提交写计划时报一个看不懂的错。
        real = table_columns(self.table)
        unknown = (set(writable_fields) | set(filter_fields or ())) - real
        if unknown:
            raise PolicyError(f"{table} 的写名单含不存在的列: {sorted(unknown)}")
        self.writable_fields = frozenset(checked_ident(c) for c in writable_fields)
        # 可过滤字段 = 可写字段 + 该表的定位列(主键/时间/状态/工号)。
        self.filter_fields = frozenset(
            checked_ident(c) for c in (filter_fields or writable_fields)
        ) | frozenset(self.pk_columns)
        self.soft_delete = soft_delete
        self.insertable = insertable

    @property
    def delete_sets(self) -> dict[str, object]:
        """软删除改写出来的 SET 子句(层 4-3): DELETE 永远不会真的删行。

        ``deleted_at`` 用 ``now()`` 而不是 Python 时间: 让数据库做时钟权威, 避免容器
        时区(UTC)与业务时区(东八区)相差 8 小时后"7 天保留窗口"变成 7 天 8 小时。
        """
        return {"is_deleted": True, "deleted_at": "now()", "deleted_by": "scope_actor"}

    @property
    def soft_delete_columns(self) -> frozenset[str]:
        """软删三件套列名(角色授权与回滚都要用, 不能只靠字面量飘在各处)。"""
        if not self.soft_delete:
            return frozenset()
        return frozenset({"is_deleted", "deleted_at", "deleted_by"})

    @property
    def grant_update_columns(self) -> tuple[str, ...]:
        """写角色实际要授的 UPDATE 列集: 可写字段 + 软删三件套。

        少了软删列会出一个很难定位的错: 软删模板要 SET is_deleted/deleted_at/deleted_by,
        而 PG 对列级权限不足报的是"permission denied for table"(看起来像整表没授权)。
        """
        return tuple(sorted(self.writable_fields | self.soft_delete_columns))


# 可写实体: 只有"单据/台账"类表。员工主数据、预算、供应商是高危表(见 FORBIDDEN_ENTITIES)。
WRITE_ENTITIES: dict[str, WriteSpec] = {
    "hr_tickets": WriteSpec(
        entity="hr_tickets",
        table="hr_tickets",
        label="HR 工单",
        pk_columns=("ticket_no",),
        writable_fields={"category", "title", "description", "status"},
        filter_fields={"emp_id", "category", "status", "created_at", "updated_at"},
    ),
    "hr_leave_records": WriteSpec(
        entity="hr_leave_records",
        table="hr_leave_records",
        label="请假记录",
        pk_columns=("id",),
        writable_fields={"status", "leave_type", "start_date", "end_date", "days"},
        filter_fields={"emp_id", "status", "leave_type", "created_at"},
    ),
    "fin_reimbursements": WriteSpec(
        entity="fin_reimbursements",
        table="fin_reimbursements",
        label="报销单",
        pk_columns=("order_no",),
        writable_fields={"title", "amount", "category", "reason", "status", "current_node"},
        filter_fields={"emp_id", "category", "status", "amount", "created_at"},
    ),
    "proc_orders": WriteSpec(
        entity="proc_orders",
        table="proc_orders",
        label="采购申请单",
        pk_columns=("order_no",),
        writable_fields={
            "title", "category", "amount", "currency", "supplier_name", "quotes_count",
            "budget_year", "reason", "status", "current_node", "precheck_result",
        },
        filter_fields={"emp_id", "department", "category", "status", "supplier_name",
                       "amount", "created_at"},
    ),
    "proc_contracts": WriteSpec(
        entity="proc_contracts",
        table="proc_contracts",
        label="合同台账",
        pk_columns=("contract_no",),
        writable_fields={"title", "category", "amount", "currency", "sign_date",
                         "effective_date", "expiry_date", "status", "risk_level",
                         "reviewer", "opinion"},
        filter_fields={"party_b", "category", "status", "risk_level", "amount",
                       "expiry_date", "created_at"},
    ),
}

# 永远不许出现在过滤器里的列: 作用域谓词由服务端注入, 模型/客户端无权干预。
# 单独列出来是为了报错时能说清"为什么不让你写"而不是只说"字段不在名单"。
SCOPE_COLUMNS: frozenset[str] = frozenset({"tenant_id", "dept_id"})

# 高危表黑名单(层 4-6): 财务主数据、权限/身份、审计与回滚数据。
# 语义上它们已经被"默认拒"覆盖了(不在 WRITE_ENTITIES 就写不了), 这里再显式列一遍是
# 给人与代码同一个信息: 这几张表**永远**不该出现在写计划里, 出现即告警。
FORBIDDEN_ENTITIES: frozenset[str] = frozenset({
    "hr_employees",              # 员工主数据(含部门/年假额度)
    "fin_department_budgets",    # 预算池
    "proc_suppliers",            # 供应商准入与黑名单判定依据
    "sys_departments",           # 隔离键本身
    "dataops_pending",           # 写计划状态机
    "sql_audit_records",         # 审计
    "dataop_before_image",       # 变更前镜像
    "dataop_archive",            # 真删归档
})


def spec_for(entity: str) -> WriteSpec:
    """取实体的写规格; 名单外一律拒(默认拒)。"""
    if entity in FORBIDDEN_ENTITIES:
        raise PolicyError(f"{entity} 是高危表, 智能体无任何写权限(请走人工变更流程)")
    spec = WRITE_ENTITIES.get(entity)
    if spec is None:
        raise PolicyError(f"实体 {entity!r} 不在可写白名单内, 可用实体: {sorted(WRITE_ENTITIES)}")
    return spec
