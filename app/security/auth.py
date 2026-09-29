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
TOOL_ROLE_RESTRICTIONS: dict[str, set[Role]] = {
    # e.g. only finance staff may look up *other* people's orders in a
    # real system; here the tools are self-service so all roles pass.
}

# ---------------------------------------------------------------------------
# 角色×工具白名单矩阵 (单一事实来源): 默认拒绝语义 —— 工具未列入某角色的白名单
# 即对该角色完全隐藏 (LLM 看不见、调不到), 而非调用时报错。
# 注意: MCP server 新增工具时必须同步维护本矩阵。
# None 表示该域全量可见。
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
        "query_reimbursement",
        "list_reimbursements",
        "get_reimbursement_policy",
    },
    Role.MANAGER: {
        "create_reimbursement",
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
# 与 execute_sql 同级 —— 仅管理角色可见; 普通员工对本域默认拒绝(无任何工具)。
# ---------------------------------------------------------------------------
ANALYTICS_TOOL_WHITELIST: dict[Role, set[str] | None] = {
    Role.EMPLOYEE: set(),  # 跨域经营数据对普通员工完全隐藏
    Role.MANAGER: None,    # 经理及以上: analytics 域全量可见
    Role.HR: None,
    Role.FINANCE: None,
    Role.ADMIN: None,
}

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
    Role.MANAGER: _PROCUREMENT_BASE_TOOLS | {"execute_sql"},
    Role.HR: None,      # HR/财务专员/管理员: 采购域全量可见
    Role.FINANCE: None,
    Role.ADMIN: None,
}

# server_name -> 角色×工具白名单矩阵
_DOMAIN_TOOL_WHITELISTS: dict[str, dict[Role, set[str] | None]] = {
    "finance": FINANCE_TOOL_WHITELIST,
    "hr": HR_TOOL_WHITELIST,
    "analytics": ANALYTICS_TOOL_WHITELIST,
    "procurement": PROCUREMENT_TOOL_WHITELIST,
}


def filter_tools_for_role(role: Role, server_name: str, tools: list[Any]) -> list[Any]:
    """Return the subset of ``tools`` visible to ``role`` on ``server_name``.

    Tools must expose a ``name`` attribute (LangChain BaseTool). The four MCP
    business domains (finance/hr/analytics/procurement) are tiered by the
    matrices in ``_DOMAIN_TOOL_WHITELISTS``; unregistered domains (e.g. the
    in-process capability domains web/docgen, which have no role×tool matrix)
    pass through unchanged. Default-deny: roles missing from the matrix see
    nothing.
    """
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
