"""HR_Agent business logic + A2A AgentExecutor.

权限分级 (与 Finance_Agent 同源同构):
- 角色×工具白名单矩阵 (app.security.auth.HR_TOOL_WHITELIST) 决定每个角色
  可见的 MCP 工具 (权限Mask, 硬控制)。
- Text2SQL (execute_sql) 可查询全部员工数据, 仅对管理角色可见。
- 角色只信 A2A Message.metadata, 不从用户文本解析角色, 防伪造。
"""

from __future__ import annotations

import re
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.agents.common_tools import lookup_employee_by_name
from app.config import get_settings
from app.db.schema_docs import HR_SCHEMA_DDL
from app.llm import get_chat_model
from app.schemas import Role
from app.security.audit import get_audit_logger
from app.security.auth import filter_tools_for_role

_EMPLOYEE_TAG_RE = re.compile(r"\[employee_id=([A-Za-z0-9_\-]+)\]")

_ROLE_LABELS: dict[Role, str] = {
    Role.EMPLOYEE: "普通员工",
    Role.MANAGER: "部门经理",
    Role.HR: "HR专员",
    Role.FINANCE: "财务专员",
    Role.ADMIN: "管理员",
}

_BASE_PROMPT = """你是 HR_Agent,企业 HR 服务专业智能体。
当前操作用户权限层级: {role_label}。

职责:
1. 目标员工解析: 用户只提供姓名、未提供工号时, 先调用 lookup_employee_by_name
   解析目标员工工号; 若返回 needs_selection=True(同名多人), 向用户列出全部候选
   (姓名+工号+部门)请其明确选择一个, 不要自行猜测工号。
2. HR 工单创建:收集类别、标题、描述后调用 create_hr_ticket。
3. 工单查询/取消:调用 query_hr_ticket / list_hr_tickets / cancel_hr_ticket。
4. 年假查询:调用 get_leave_balance。
{capabilities}

业务表结构 (只读, 供 Text2SQL 生成 SQL 参考):
{schema}

Text2SQL 规则 (仅当 execute_sql 工具在你本次可用工具列表中时适用):
- 用户提出统计/明细类查询(如"研发部今年提了多少工单"、"谁的年假剩余最多")时,
  依据上述表结构编写单条 MySQL SELECT 语句并调用 execute_sql。
- 只写 SELECT;只查白名单内的表;需要部门/姓名时 JOIN hr_employees。
- 若工具返回 error (SQL 校验失败/执行失败), 依据错误信息修正后重试一次;
  仍失败则如实告知用户, 不要编造结果。
- 查询结果用简洁表格或列表呈现, 并说明统计口径与时间范围。

规则:
- 缺少必填信息时主动追问,不要编造。
- 工具返回的 error 字段必须如实转达。
- 系统上下文会给出当前登录操作者的 employee_id。操作者身份仅代表登录态,
  不等于业务目标用户: 若用户消息中指定了目标员工(工号/姓名), 以消息指定的
  为准; 仅当查询/办理"我/本人"相关业务且未指定他人时, 才默认使用操作者
  employee_id。
- 用简洁中文回复。

权限边界(必须严格遵守):
- 你只能使用系统提供的工具;若某工具不在你本次可用的工具列表中,不要尝试调用,更不要编造调用结果。
- 若用户请求超出当前权限层级的能力,礼貌说明无权限,并建议其联系部门经理或HR专员处理。"""

_EMPLOYEE_CAPABILITIES = """当前角色可用工具: lookup_employee_by_name、create_hr_ticket、query_hr_ticket、list_hr_tickets、cancel_hr_ticket、get_leave_balance。
注意: 数据统计查询(execute_sql, 可查全员数据)对普通员工不可用。"""

_MANAGER_CAPABILITIES = """当前角色可用工具: lookup_employee_by_name、工单全流程工具、get_leave_balance, 以及 execute_sql (Text2SQL, 只读查询 hr_employees/hr_tickets/hr_leave_records)。"""

_SPECIALIST_CAPABILITIES = """当前角色为管理角色(HR/财务专员/管理员), HR 域工具全量可用: lookup_employee_by_name、工单全流程工具、get_leave_balance, 以及 execute_sql (Text2SQL, 只读查询 hr_employees/hr_tickets/hr_leave_records)。"""

_ROLE_CAPABILITIES: dict[Role, str] = {
    Role.EMPLOYEE: _EMPLOYEE_CAPABILITIES,
    Role.MANAGER: _MANAGER_CAPABILITIES,
    Role.HR: _SPECIALIST_CAPABILITIES,
    Role.FINANCE: _SPECIALIST_CAPABILITIES,
    Role.ADMIN: _SPECIALIST_CAPABILITIES,
}


def _build_role_prompt(role: Role) -> str:
    """动态 System Prompt: 按角色注入权限层级、可用工具与表结构。"""
    return _BASE_PROMPT.format(
        role_label=_ROLE_LABELS[role],
        capabilities=_ROLE_CAPABILITIES[role],
        schema=HR_SCHEMA_DDL,
    )


class HRAgent:
    """LangGraph ReAct agent over HR MCP tools, role-aware."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=0.1)
        self._tools: list[Any] | None = None
        self._agents: dict[Role, Any] = {}

    async def _ensure_agent(self, role: Role = Role.EMPLOYEE) -> Any:
        """Lazily connect to the MCP server and build a per-role ReAct graph."""
        if role not in self._agents:
            if self._tools is None:
                client = MultiServerMCPClient(
                    {"hr": {"url": self._settings.hr_mcp_url, "transport": "streamable_http"}}
                )
                self._tools = await client.get_tools()
            # 权限Mask: 与编排层共用同一张角色×工具白名单矩阵 (硬控制)。
            tools = filter_tools_for_role(role, "hr", self._tools)
            # 跨域基础解析能力(姓名->工号)注入: 用户只给姓名时先解析工号。
            tools = [*tools, lookup_employee_by_name]
            self._agents[role] = create_agent(
                self._llm, tools, prompt=_build_role_prompt(role)
            )
        return self._agents[role]

    async def invoke(self, user_text: str, user_id: str = "", role: Role = Role.EMPLOYEE) -> str:
        """Run one delegated task under the given (trusted) identity."""
        agent = await self._ensure_agent(role)
        # 身份走 System 消息, 与用户请求文本分离; 显式区分"当前操作者"(登录态)
        # 与"任务目标用户"(消息中指定的他人), 防止 LLM 把操作者工号误用作
        # 目标员工的查询参数。
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={user_id or 'anonymous'}。"
            "操作者身份仅代表登录态, 不等于任务目标用户: 若用户消息中指定了目标员工"
            "(工号/姓名), 以消息指定的为准; 仅当查询/办理\"我/本人\"相关业务且未指定"
            "他人时, 才默认使用操作者 employee_id。"
        )
        result = await agent.ainvoke(
            {"messages": [("system", system_context), ("user", user_text)]}
        )
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                return str(msg.content)
        return "HR 智能体未能生成有效回复。"


class HRAgentExecutor(AgentExecutor):
    """A2A AgentExecutor bridge for HR_Agent."""

    def __init__(self) -> None:
        self._agent = HRAgent()
        self._audit = get_audit_logger()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        trace_id = context.task_id or context.context_id or "unknown"

        # 身份只信协议级 Message.metadata (编排层写入); 用户文本中的任何角色
        # 字样一律忽略, 防伪造。metadata 缺失 -> role 回退 employee 最小权限。
        metadata: dict[str, Any] = getattr(context.message, "metadata", None) or {}
        user_id = str(metadata.get("user_id") or "")
        if not user_id:
            fallback = _EMPLOYEE_TAG_RE.search(user_input)
            user_id = fallback.group(1) if fallback else ""
        try:
            role = Role(str(metadata.get("role") or Role.EMPLOYEE.value))
        except ValueError:
            role = Role.EMPLOYEE

        self._audit.log(
            trace_id, "hr_agent", "a2a_task_received",
            {"input": user_input, "user_id": user_id or "unknown", "role": role.value},
        )
        try:
            answer = await self._agent.invoke(user_input, user_id=user_id, role=role)
        except Exception as exc:
            answer = f"HR 智能体处理失败: {exc}"
        self._audit.log(trace_id, "hr_agent", "a2a_task_completed", {"answer": answer})
        await event_queue.enqueue_event(new_agent_text_message(answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("HR_Agent does not support cancellation")
