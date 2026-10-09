"""Role-based permission whitelists for tools and agents."""

from __future__ import annotations

from typing import Any

from app.schemas import Role

# Which MCP tool namespaces each role may invoke.
# analytics/procurement 是新增的两个业务域(数据洞察 / 采购合同初审)。
MCP_WHITELIST: dict[Role, set[str]] = {
    Role.EMPLOYEE: {"hr", "finance", "procurement"},
    Role.MANAGER: {"hr", "finance", "analytics", "procurement"},
    Role.HR: {"hr", "finance", "analytics", "procurement"},
    Role.FINANCE: {"hr", "finance", "analytics", "procurement"},
    Role.ADMIN: {"hr", "finance", "analytics", "procurement"},
}

# Which A2A agents each role may delegate to.
# analytics_agent(Analyst_Agent) 跨域查询全员经营数据, 属敏感能力, 不对普通员工开放;
# procurement_agent(Contract_Agent) 支持员工自提采购单/送审合同, 对其开放。
AGENT_WHITELIST: dict[Role, set[str]] = {
    Role.EMPLOYEE: {"finance_agent", "hr_agent", "procurement_agent"},
    Role.MANAGER: {"finance_agent", "hr_agent", "analytics_agent", "procurement_agent"},
    Role.HR: {"hr_agent", "finance_agent", "analytics_agent", "procurement_agent"},
    Role.FINANCE: {"finance_agent", "hr_agent", "analytics_agent", "procurement_agent"},
    Role.ADMIN: {"finance_agent", "hr_agent", "analytics_agent", "procurement_agent"},
}

# Fine-grained tool-level restrictions: sensitive tools are limited by role.
# 本表只管"某工具对哪些角色可见"; "能不能碰别人的数据"不归它管 —— 归属校验在服务端
# 注入的调用者身份上做(见 app/security/caller.py 与各 MCP server 的 guard_* 调用)。
TOOL_ROLE_RESTRICTIONS: dict[str, set[Role]] = {
}

# ---------------------------------------------------------------------------
# 角色×工具白名单矩阵 (单一事实来源): 默认拒绝语义 —— 工具未列入某角色的白名单
# 即对该角色完全隐藏 (LLM 看不见、调不到), 而非调用时报错。
# 注意: MCP server 新增工具时必须同步维护本矩阵。
# None 表示该域全量可见。
# 本矩阵只管"看得见看不见"; "能不能碰别人的数据"由工具内部按网关注入的调用者
# 身份判定(见 app/security/caller.py) —— 所以员工侧保留单据类工具是安全的:
# 他们只能拿到自己名下的, 跨人访问会在工具侧被拒。
# Text2SQL (execute_sql) 可查询全部员工/部门数据, 属敏感工具, 仅对管理角色开放。
_HR_BASE_TOOLS = {
    "create_hr_ticket",
    "query_hr_ticket",
    "list_hr_tickets",
    "cancel_hr_ticket",
    "get_leave_balance",
}
HR_TOOL_WHITELIST: dict[Role, set[str] | None] = {
    Role.EMPLOYEE: _HR_BASE_TOOLS,
    Role.MANAGER: _HR_BASE_TOOLS | {"execute_sql"},
    Role.HR: None,      # HR/财务专员/管理员: HR 域全量可见
    Role.FINANCE: None,
    Role.ADMIN: None,
}

FINANCE_TOOL_WHITELIST: dict[Role, set[str] | None] = {
    Role.EMPLOYEE: {
        "create_reimbursement",
        "preview_reimbursement",
        "query_reimbursement",
        "list_reimbursements",
        "get_reimbursement_policy",
    },
    Role.MANAGER: {
        "create_reimbursement",
        "preview_reimbursement",
        "query_reimbursement",
        "list_reimbursements",
        "get_reimbursement_policy",
        "finance_budget_query",
        "execute_sql",
    },
    Role.HR: None,      # HR/财务专员/管理员: 财务域全量可见
    Role.FINANCE: None,
    Role.ADMIN: None,
}

# ---------------------------------------------------------------------------
# 数据洞察域(analytics): run_sql 可跨 HR/Finance/Procurement 查全员数据, 属敏感工具,
# 与 execute_sql 同级 —— 仅管理角色可见; 普通员工对本域默认拒(无任何工具)。
#
# 写三工具(plan_data_op/confirm_data_op/list_my_dataops)单独按配置开放: 能看全员
# 不等于能改数据(所以 manager 从原来的"全量可见"收成了"只读全量")。
# ---------------------------------------------------------------------------
ANALYTICS_READ_TOOLS: frozenset[str] = frozenset({
    "run_sql",
    "describe_tables",
    "get_metrics_snapshot",
    "render_chart",
    "write_weekly_report",
    "list_artifacts",
})
ANALYTICS_WRITE_TOOLS: frozenset[str] = frozenset({
    "plan_data_op",
    "confirm_data_op",
    "list_my_dataops",
})


def dataops_writable_roles() -> set[str]:
    """可发起/确认写计划的角色集(配置驱动; 解不出任何角色 = 无人可写)。

    刻意每次调用现读配置而不是 import 时烧成常量: 写权限名单变了就得生效,
    不能变成"改了 docker/.env 但不重建镜像就不生效"那种静默不一致。
    """
    from app.config import get_settings

    raw = (get_settings().dataops_writable_roles or "").lower()
    known = {r.value for r in Role}
    return {p.strip() for p in raw.split(",") if p.strip() in known}


def analytics_whitelist(role: Role) -> set[str]:
    """analytics 域对某角色可见的工具集(读集 + 按配置附加写集)。"""
    if role is Role.EMPLOYEE:
        # 跨域经营数据对普通员工完全隐藏(网关层 AGENT_WHITELIST 也已拦他们)。
        return set()
    tools = set(ANALYTICS_READ_TOOLS)
    if role.value in dataops_writable_roles():
        tools |= ANALYTICS_WRITE_TOOLS
    return tools

# ---------------------------------------------------------------------------
# 采购合同域(procurement): 员工可自助下单/送审/查自己单据, 但 execute_sql
# (跨全员采购/合同统计)仅对管理角色开放。None 表示全量可见。
# ---------------------------------------------------------------------------
_PROCUREMENT_BASE_TOOLS = {
    "create_purchase_order",
    "precheck_purchase_order",
    "query_purchase_order",
    "list_purchase_orders",
    "check_purchase_compliance",
    "list_suppliers",
    "query_supplier",
    "check_contract_clauses",
    "submit_contract_review",
    "query_contract",
    "list_contracts",
    "get_contract_text",
    "save_contract_opinion",
}
PROCUREMENT_TOOL_WHITELIST: dict[Role, set[str] | None] = {
    Role.EMPLOYEE: _PROCUREMENT_BASE_TOOLS,
    Role.MANAGER: _PROCUREMENT_BASE_TOOLS | {"execute_sql", "confirm_contract_review"},
    Role.HR: None,      # HR/财务专员/管理员: 采购域全量可见
    Role.FINANCE: None,
    Role.ADMIN: None,
}

# server_name -> 角色×工具白名单矩阵; analytics 不在这里(它需要按配置即时算)。
_DOMAIN_TOOL_WHITELISTS: dict[str, dict[Role, set[str] | None]] = {
    "finance": FINANCE_TOOL_WHITELIST,
    "hr": HR_TOOL_WHITELIST,
    "procurement": PROCUREMENT_TOOL_WHITELIST,
}

# ---------------------------------------------------------------------------
# 能力域(web 联网检索 / docgen 文件生成)的域级闸门
# ---------------------------------------------------------------------------
# 能力域的工具在本进程内, 不参与上面的 MCP 矩阵; "不建工具级矩阵" (计划 D3) 原先被
# 实现成了"什么都不查", 于是任意角色任意调用量都能驱动联网抓取与落盘生成。现在按
# 配置声明允许的角色与域 —— 默认拒: 未登记的域与不在白名单里的角色一律拒。
_ALL_ROLES = frozenset(Role)


def _capability_roles() -> frozenset[str]:
    """从配置解出可用能力域的角色集合(解不出任何一个时 = 无人可用, 而不是全员可用)。"""
    from app.config import get_settings

    raw = (get_settings().capability_allowed_roles or "").lower()
    known = {r.value for r in _ALL_ROLES}
    return frozenset(p.strip() for p in raw.split(",") if p.strip() in known)


def capability_allowed(capability: str) -> set[str]:
    """该能力域当前对哪些角色开放(返回角色 value 集合, 空集 = 全员禁用)。"""
    return set(_capability_roles()) if capability in ("web", "docgen") else set()


def check_capability_permission(role: Role, capability: str) -> None:
    """Raise PermissionDenied if the role may not use the in-process capability domain."""
    if capability not in ("web", "docgen"):
        raise PermissionDenied(f"未登记的能力域 {capability}")
    if role.value not in capability_allowed(capability):
        raise PermissionDenied(f"角色 {role.value} 无权使用 {capability} 能力域")


def filter_tools_for_role(role: Role, server_name: str, tools: list[Any]) -> list[Any]:
    """Return the subset of ``tools`` visible to ``role`` on ``server_name``.

    Tools must expose a ``name`` attribute (LangChain BaseTool). The four MCP
    business domains (finance/hr/analytics/procurement) are tiered by the
    matrices in ``_DOMAIN_TOOL_WHITELISTS`` (analytics by :func:`analytics_whitelist`,
    because its write tools depend on ``DATAOPS_WRITABLE_ROLES``); unregistered
    domains (e.g. the in-process capability domains web/docgen) pass through here
    **without a per-tool matrix** — they are gated one level up by
    :func:`check_capability_permission` plus :mod:`app.security.quota`, so
    "pass through" no longer means "no permission layer at all". Default-deny:
    roles missing from the matrix see nothing.
    """
    if server_name == "analytics":
        return [t for t in tools if t.name in analytics_whitelist(role)]
    matrix = _DOMAIN_TOOL_WHITELISTS.get(server_name)
    if matrix is None:
        return list(tools)
    if role not in matrix:
        return []
    whitelist = matrix[role]
    if whitelist is None:
        return list(tools)
    return [t for t in tools if t.name in whitelist]


class PermissionDenied(Exception):
    """Raised when a role attempts to use a forbidden tool/agent."""


def check_agent_permission(role: Role, agent_name: str) -> None:
    """Raise PermissionDenied if the role may not delegate to the agent."""
    allowed = AGENT_WHITELIST.get(role, set())
    if agent_name not in allowed:
        raise PermissionDenied(f"角色 {role.value} 无权访问智能体 {agent_name}")


def check_mcp_permission(role: Role, server_name: str, tool_name: str) -> None:
    """Raise PermissionDenied if the role may not call the MCP tool."""
    allowed_servers = MCP_WHITELIST.get(role, set())
    if server_name not in allowed_servers:
        raise PermissionDenied(f"角色 {role.value} 无权访问 MCP 服务 {server_name}")
    restricted = TOOL_ROLE_RESTRICTIONS.get(f"{server_name}.{tool_name}")
    if restricted is not None and role not in restricted:
        raise PermissionDenied(f"角色 {role.value} 无权调用工具 {server_name}.{tool_name}")
