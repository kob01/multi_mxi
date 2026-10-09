"""数据作用域(tenant/dept)的解析、回填与下发(层 1/2)。

这一层存在的理由: **隔离谓词不能由模型给**。模型只能给"我要改哪几行的业务条件",
而 ``tenant_id`` / ``dept_id`` 这两道谓词由本模块从认证态解析出来, 由服务端模板写进
最终 SQL, 并同步写进会话变量供 RLS 策略读取(双保险: 模板漏写也还有数据库层兜底)。

三条口径:
1. **默认拒**: 解析不到调用者所属部门、行未归属(``dept_id=''``)、GUC 缺失, 一律让
   谓词与策略都匹配不上 —— 宁可"暂时看不见", 也不要"暂时谁都能看见"。
2. **作用域只认服务端查到的那份**: 入参只有工号与角色(网关注入的 caller_*), 部门名/
   部门号都不接受来自模型或客户端的直传。
3. **全员角色与本部门角色分开**: ``analytics_all_dept_roles``(hr/finance/admin) 才拿到
   全租户部门集; manager 默认只有本部门 —— 这与改造前的"经理可跨全员查数"是**收紧**。

一个统一形状值得说明: 全员角色不是"关掉部门过滤", 而是"部门集=本租户全部部门"。
这样只读模板与写模板的谓词形状完全一致(``tenant_id = ? AND dept_id = ANY(?)``),
RLS 策略也只需一条表达式, 少一个 ``app.dept_all`` 之类的开关型概念就可能少一次
"忘了开/开错了"的故障面。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from app.config import get_settings

logger = logging.getLogger(__name__)

# RLS 策略与模板共用的会话变量名(值只由本模块写; 下发见 app/db/sync.py)。
GUC_TENANT = "app.tenant_id"
GUC_DEPT_SCOPE = "app.dept_scope"

# 模板与作用域之间的固定绑参名契约(写通道与 AST 校验都按这两个名字检查)。
PARAM_TENANT = "scope_tenant"
PARAM_DEPTS = "scope_depts"

# 租户内共享的主数据(供应商/合同台账)的部门哨兵值: 与"未归属"的空串严格区分。
SHARED_DEPT_ID = "*"
# 作用域为空时喂给 string_to_array 的哨兵: 保证匹配不到任何真实行(而不是匹配到 '')。
NO_DEPT_SENTINEL = "__unassigned__"

# 参与作用域隔离的业务表(与 app/db/session.py::_SCOPE_TABLES 同一份名单)。
SCOPED_TABLES = (
    "hr_employees",
    "hr_tickets",
    "hr_leave_records",
    "fin_reimbursements",
    "fin_department_budgets",
    "proc_suppliers",
    "proc_orders",
    "proc_contracts",
)


class ScopeError(ValueError):
    """解析不出可用的数据作用域(调用者不在主数据 / 行未归属 / 租户缺失)。"""


@dataclass(frozen=True)
class DataScope:
    """一次调用被授权触达的数据范围。"""

    tenant_id: str
    dept_ids: tuple[str, ...]
    all_depts: bool
    user_id: str = ""
    role: str = ""
    department: str = ""

    def dept_scope_text(self) -> str:
        """逗号串形式的部门集(与 RLS 策略里的 string_to_array 同一种表示)。"""
        return ",".join(self.dept_ids)

    def session_settings(self) -> dict[str, str]:
        """写进当前事务的会话变量, 供 RLS 策略读取。"""
        return {
            GUC_TENANT: self.tenant_id,
            GUC_DEPT_SCOPE: self.dept_scope_text() or NO_DEPT_SENTINEL,
        }

    def scope_params(self) -> dict[str, object]:
        """与 :meth:`scope_predicate` 配套的绑定参数(值走参数通道, 永不进 SQL 文本)。"""
        return {
            PARAM_TENANT: self.tenant_id,
            PARAM_DEPTS: self.dept_scope_text() or NO_DEPT_SENTINEL,
        }

    def scope_clause(self, alias: str = "") -> str:
        """服务端注入的域谓词(列名可按表别名前缀拼; 值全是绑参占位符)。

        占位符用 SQLAlchemy 的 ``:name`` 而不是 DBAPI 的 ``%(name)s``: 模板文本要能直接
        交给 sqlglot 解析做层 3 自校验, ``:name`` 是它能认的 placeholder, ``%()s`` 不是。

        ``dept_id = ANY(string_to_array(:scope_depts, ','))`` 而不是 ``IN (...)``:
        前者一个参数装得下任意个部门, 不必为变长列表展开占位符, 也就没有"展开时把
        值拼进文本"的机会。
        """
        prefix = f"{alias}." if alias else ""
        return (
            f"{prefix}tenant_id = :{PARAM_TENANT} "
            f"AND {prefix}dept_id = ANY(string_to_array(:{PARAM_DEPTS}, ','))"
        )

    def note(self) -> str:
        """口径说明(要出现在给用户的回答里, 让人知道这次看到的是谁的数据)。"""
        if self.all_depts:
            return f"统计范围: 租户 {self.tenant_id} 全员数据(角色 {self.role})"
        label = self.department or "本部门"
        return f"统计范围: {label}(dept_id={self.dept_scope_text() or '未归属'})"


def all_dept_roles() -> frozenset[str]:
    """不受部门范围限制的角色集合(配置解析失败时退为空集 = 谁都不能跨部门)。"""
    raw = (get_settings().analytics_all_dept_roles or "").lower()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def resolve_scope_sync(user_id: str, role: str) -> DataScope:
    """把"网关注入的工号 + 角色"解析成服务端可信的数据作用域(同步版)。

    走同步引擎而不是 async: 调用方是 FastMCP 的普通函数工具(不能 await), 且写计划
    的执行事务本来就在同步引擎上。REST 审批台在同一网关进程里用 threadpool 调它。

    部门归属从库里查(``hr_employees.dept_id``), **不看客户端自报的 department 字段**:
    后者只用于文档 ACL 展示, 拿它当隔离键等于让调用方自己声明属于哪个部门。
    解析不出就抛 :class:`ScopeError` —— 调用方必须拒执行, 而不是"不过滤=看全部"。
    """
    from sqlalchemy.orm import Session

    from app.db import sync as dbsync

    settings = get_settings()
    tenant = settings.default_tenant_id
    role = (role or "").strip().lower() or "employee"
    user_id = (user_id or "").strip()
    if not tenant:
        raise ScopeError("未配置 DEFAULT_TENANT_ID, 无法确定数据租户")

    with Session(dbsync.get_sync_engine()) as session:
        if role in all_dept_roles():
            rows = session.execute(
                text("SELECT dept_id FROM sys_departments WHERE tenant_id = :tenant "
                     "ORDER BY dept_id"),
                {"tenant": tenant},
            ).all()
            if not rows:
                raise ScopeError("部门注册表为空, 无法建立跨部门作用域(请先完成作用域回填)")
            dept_ids = tuple(r[0] for r in rows) + (SHARED_DEPT_ID,)
            return DataScope(
                tenant_id=tenant, dept_ids=dept_ids, all_depts=True,
                user_id=user_id, role=role, department="全员",
            )

        if not user_id:
            raise ScopeError("缺少调用者工号, 无法确定数据作用域")
        row = session.execute(
            text("SELECT e.tenant_id, e.dept_id, e.department FROM hr_employees e "
                 "WHERE e.emp_id = :emp_id"),
            {"emp_id": user_id},
        ).first()
        if row is None:
            raise ScopeError(f"调用者 {user_id} 不在员工主数据里, 无法确定所属部门")
        emp_tenant, emp_dept, emp_department = (row[0] or ""), (row[1] or ""), (row[2] or "")
        if not emp_tenant or not emp_dept:
            raise ScopeError(
                f"调用者 {user_id} 的数据归属未回填(tenant_id/dept_id 为空), "
                "请先执行 init_schema 的作用域回填"
            )
        if emp_tenant != tenant:
            raise ScopeError(f"调用者所属租户 {emp_tenant} 与当前部署租户 {tenant} 不一致")
        return DataScope(
            tenant_id=tenant,
            dept_ids=(emp_dept, SHARED_DEPT_ID),
            all_depts=False,
            user_id=user_id,
            role=role,
            department=emp_department,
        )


# ---------------------------------------------------------------------------
# 作用域回填(幂等): 老库升级与新库首建都走这里
# ---------------------------------------------------------------------------
_DEPT_REGISTRATION_SQL = """
INSERT INTO sys_departments (dept_id, name, tenant_id)
SELECT 'D' || lpad((COALESCE(mx.maxn, 0) + row_number() OVER (ORDER BY x.department))::text, 3, '0'),
       x.department, :tenant
FROM (SELECT DISTINCT department FROM hr_employees WHERE department <> '') x
CROSS JOIN (
    SELECT MAX(substring(dept_id FROM 2)::int) AS maxn
    FROM sys_departments WHERE dept_id ~ '^D[0-9]+$'
) mx
WHERE NOT EXISTS (SELECT 1 FROM sys_departments d WHERE d.name = x.department)
ON CONFLICT (name) DO NOTHING
"""


async def ensure_data_scope_backfill() -> None:
    """给业务表补出可信的 tenant_id/dept_id(只填空着的部分, 可反复执行)。

    部门号必须**稳定**: 每次都按排序重新编号会让历史行指向错误的部门, 所以这里只给
    "新出现的部门名"追加号段(``D001`` 起), 已注册的号永不改动。

    回填不出部门归属的两张表按"租户内共享"处理(``dept_id='*'``):

    - ``proc_suppliers``: 供应商主数据本来就跨部门共用;
    - ``proc_contracts``: **表里没有部门归属列**, 无法判定属于哪个部门。本轮按共享
      处理, 代价是合同台账对本租户所有角色可见(与改造前无隔离时相同, 不是新增泄露)。
      要让合同也按部门隔离, 需要给送审流程加一个 dept_id 落库点(见 plan 的边界章节)。
    """
    from app.db.session import get_engine

    tenant = get_settings().default_tenant_id
    if not tenant:
        logger.warning("DEFAULT_TENANT_ID 为空, 跳过作用域回填")
        return

    async with get_engine().begin() as conn:
        await conn.execute(text(_DEPT_REGISTRATION_SQL), {"tenant": tenant})

        # 1) 租户归属: 只动"未归属"的行, 已归属的保持原值(多租户回填由外部脚本负责)。
        for table in SCOPED_TABLES:
            await conn.execute(
                text(f"UPDATE {table} SET tenant_id = :tenant WHERE tenant_id = ''"),
                {"tenant": tenant},
            )

        # 2) 部门归属: 按各自的归属路径回填, 同样只动 dept_id = '' 的行。
        #    每条语句自带自己的参数: 给不引用 :shared 的语句传多余参数会被驱动拒。
        statements = (
            # 员工: 部门名直接对注册表。
            ("UPDATE hr_employees e SET dept_id = d.dept_id FROM sys_departments d "
             "WHERE d.name = e.department AND e.dept_id = ''", {}),
            # 工单/请假/报销: 归属看提单人的部门(经员工表一跳)。
            ("UPDATE hr_tickets t SET dept_id = e.dept_id FROM hr_employees e "
             "WHERE e.emp_id = t.emp_id AND t.dept_id = '' AND e.dept_id <> ''", {}),
            ("UPDATE hr_leave_records l SET dept_id = e.dept_id FROM hr_employees e "
             "WHERE e.emp_id = l.emp_id AND l.dept_id = '' AND e.dept_id <> ''", {}),
            ("UPDATE fin_reimbursements r SET dept_id = e.dept_id FROM hr_employees e "
             "WHERE e.emp_id = r.emp_id AND r.dept_id = '' AND e.dept_id <> ''", {}),
            # 预算/采购单: 自带部门名列。
            ("UPDATE fin_department_budgets b SET dept_id = d.dept_id FROM sys_departments d "
             "WHERE d.name = b.department AND b.dept_id = ''", {}),
            ("UPDATE proc_orders o SET dept_id = d.dept_id FROM sys_departments d "
             "WHERE d.name = o.department AND o.dept_id = ''", {}),
            # 共享主数据/无归属列的表: 打租户内共享哨兵。
            ("UPDATE proc_suppliers SET dept_id = :shared WHERE dept_id = ''",
             {"shared": SHARED_DEPT_ID}),
            ("UPDATE proc_contracts SET dept_id = :shared WHERE dept_id = ''",
             {"shared": SHARED_DEPT_ID}),
        )
        for stmt, params in statements:
            await conn.execute(text(stmt), params)

        leftover = (
            await conn.execute(
                text("SELECT COUNT(*) FROM hr_employees WHERE dept_id = ''")
            )
        ).scalar_one()
        if leftover:
            logger.warning(
                "作用域回填后仍有 %s 名员工没有 dept_id(部门名不在注册表): 这些行在任何"
                "作用域下都查得到的是'未归属', 即谁也看不见 —— 需要人工纠正部门名", leftover,
            )
