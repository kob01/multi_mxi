"""Analyst_Agent business logic + A2A AgentExecutor.

数据洞察专业智能体: 一个挂在 analytics MCP server 上的 LangGraph ReAct agent。

与其它专业智能体同源同构的权限分级:
- 角色×工具白名单矩阵 (app.security.auth.ANALYTICS_TOOL_WHITELIST) 决定每个角色
  可见的工具 (硬控制); analytics 域可跨全员查数据, 属敏感能力, 普通员工被网关层
  (AGENT_WHITELIST)拦截, 到这里的基本都是管理角色。
- System Prompt 按角色声明能力边界 (软控制), 并注入多域表结构供 Text2SQL 参考。
- 身份只取 A2A Message.metadata(编排层从 ChatRequest 写入)。注意这是"可信上游假设":
  ChatRequest 的 user_id/role 仍由客户端自报, 本系统尚未接统一身份系统(口径见
  README 与 docs-interview 里的已记录欠债); 接 JWT/OIDC 后只需换 metadata 的来源。

分析产物(图表/报告)以 URL 形式回给用户, 由网关的 /api/files/reports 静态提供。
"""

from __future__ import annotations

import time
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import get_settings
from app.db.schema_docs import ANALYTICS_SCHEMA_DDL
from app.llm import get_chat_model
from app.schemas import Role
from app.security.audit import get_audit_logger
from app.security.auth import filter_tools_for_role
from app.security.caller import Caller, bind_caller_tools, reset_caller, set_caller

_ROLE_LABELS: dict[Role, str] = {
    Role.EMPLOYEE: "普通员工",
    Role.MANAGER: "部门经理",
    Role.HR: "HR专员",
    Role.FINANCE: "财务专员",
    Role.ADMIN: "管理员",
}

_BASE_PROMPT = """你是 Analyst_Agent,企业数据洞察专业智能体。
当前操作用户权限层级: {role_label}。

职责:
1. 自然语言统计查询: 依据下面的业务表结构, 编写**只读 SELECT** 并调用 run_sql 取数。
2. 图表: 需要可视化时, 先取到数据, 再调用 render_chart(bar/line/pie) 生成图表并给出链接。
3. 周期报告: 用户要"周报/月报/分析报告"时调用 write_weekly_report, 把返回的报告链接转达用户。
4. 指标概览: 用户问"整体情况/关键指标"时优先调用 get_metrics_snapshot(固定口径), 不要为
   一个总数现写 SQL。
{capabilities}

业务表结构 (只读, 供 Text2SQL 生成 SQL 参考):
{schema}

Text2SQL 规则 (仅当 run_sql 在你本次可用工具列表中时适用):
- 只写单条 SELECT(或 WITH...SELECT); 只查白名单内的表; 需要部门/姓名时 JOIN hr_employees。
- 统计口径不明确(如"费用"是否含采购、时间范围取本周还是本月)时, 先向用户确认, 不要自行猜测。
- 若 run_sql 返回 error(校验/执行失败), 依据错误信息修正后重试一次; 仍失败则如实告知,
  绝不编造数据。
- 结果用简洁表格或列表呈现, 并标注统计口径与时间范围。

产物展示规则:
- render_chart / write_weekly_report 返回的是 {url, ...} 结构; 把 url 原样以 Markdown 链接
  形式呈现给用户(如 [查看图表](url)), 不要杜撰或改写地址。

规则:
- 你是**只读**分析智能体: 不创建/修改任何业务单据, 只做查询、汇总、成图、成文。
- 工具返回的 error 字段必须如实转达。
- 系统上下文会给出当前登录操作者 employee_id, 仅供必要时定位"本人"数据; 跨域统计不以
  操作者为过滤条件(除非用户明确要"我的")。
- 用简洁中文回复。

权限边界(必须严格遵守):
- 你只能使用系统提供的工具; 若某工具不在本次可用工具列表中, 不要尝试调用, 更不要编造结果。
- 若用户请求超出当前权限层级的能力, 礼貌说明无权限, 并建议其联系部门经理或管理员。"""

_EMPLOYEE_CAPABILITIES = """当前角色对数据洞察域无可用工具(跨全员经营数据仅对管理角色开放)。
若收到普通员工的分析请求, 说明无权限并建议其联系部门经理。"""

_MANAGER_CAPABILITIES = """当前角色可用 analytics 域全量工具: run_sql(Text2SQL 只读跨域查询)、
describe_tables、get_metrics_snapshot、render_chart、write_weekly_report、list_artifacts。"""

_SPECIALIST_CAPABILITIES = _MANAGER_CAPABILITIES

_ROLE_CAPABILITIES: dict[Role, str] = {
    Role.EMPLOYEE: _EMPLOYEE_CAPABILITIES,
    Role.MANAGER: _MANAGER_CAPABILITIES,
    Role.HR: _SPECIALIST_CAPABILITIES,
    Role.FINANCE: _SPECIALIST_CAPABILITIES,
    Role.ADMIN: _SPECIALIST_CAPABILITIES,
}


def _build_role_prompt(role: Role) -> str:
    """动态 System Prompt: 按角色注入权限层级、可用工具与跨域表结构。"""
    return _BASE_PROMPT.format(
        role_label=_ROLE_LABELS[role],
        capabilities=_ROLE_CAPABILITIES[role],
        schema=ANALYTICS_SCHEMA_DDL,
    )


class AnalystAgent:
    """LangGraph ReAct agent over analytics MCP tools, role-aware."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=0)
        self._tools: list[Any] | None = None
        self._tools_at = 0.0
        self._agents: dict[Role, Any] = {}

    def _ttl(self) -> float:
        return max(1.0, float(self._settings.mcp_tools_ttl))

    async def _ensure_agent(self, role: Role = Role.MANAGER) -> Any:
        """Lazily connect to the analytics MCP server and build a per-role ReAct graph.

        工具清单按 ``mcp_tools_ttl`` 过期重发(与编排层同一口径): 原先一次发现后永不
        刷新, MCP server 重启或新增工具后本智能体永远看不到变化。
        """
        if self._tools is None or time.monotonic() - self._tools_at > self._ttl():
            client = MultiServerMCPClient(
                {"analytics": {"url": self._settings.analytics_mcp_url, "transport": "streamable_http"}}
            )
            self._tools = await client.get_tools()
            self._tools_at = time.monotonic()
            self._agents.clear()
        if role not in self._agents:
            # 权限Mask: 与编排层共用同一张角色×工具白名单矩阵 (硬控制)。
            tools = filter_tools_for_role(role, "analytics", self._tools)
            # analytics 工具不接单据归属校验(只读统计), 但统一走一次包装: 参数表里
            # 没有 caller_user_id 的工具会原样返回, 将来新增需要辨认调用者的工具时不会漏注入。
            tools = bind_caller_tools(tools)
            self._agents[role] = create_agent(self._llm, tools, system_prompt=_build_role_prompt(role))
        return self._agents[role]

    async def invoke(self, user_text: str, user_id: str, role: Role) -> str:
        """Run one delegated analysis task under the given protocol-level identity."""
        agent = await self._ensure_agent(role)
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={user_id or 'anonymous'}; 当前角色 role={role.value}。"
        )
        token = set_caller(Caller(user_id=user_id or "", role=role.value))
        try:
            result = await agent.ainvoke(
                {"messages": [("system", system_context), ("user", user_text)]}
            )
        finally:
            reset_caller(token)
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                return str(msg.content)
        return "数据洞察智能体未能生成有效回复。"


class AnalystAgentExecutor(AgentExecutor):
    """A2A AgentExecutor bridge: A2A task -> AnalystAgent invocation."""

    def __init__(self) -> None:
        self._agent = AnalystAgent()
        self._audit = get_audit_logger()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        trace_id = context.task_id or context.context_id or "unknown"

        # 身份只信协议级 metadata; metadata 缺失时按 employee 最小权限处理
        # (employee 在 analytics 域无工具, 会被告知无权限, 而非越权看数据)。
        metadata: dict[str, Any] = getattr(context.message, "metadata", None) or {}
        user_id = str(metadata.get("user_id") or "")
        try:
            role = Role(str(metadata.get("role") or Role.EMPLOYEE.value))
        except ValueError:
            role = Role.EMPLOYEE

        self._audit.log(
            trace_id, "analyst_agent", "a2a_task_received",
            {"input": user_input, "user_id": user_id or "unknown", "role": role.value},
        )
        try:
            answer = await self._agent.invoke(user_input, user_id=user_id, role=role)
        except Exception as exc:  # noqa: BLE001
            answer = f"数据洞察智能体处理失败: {exc}"
        self._audit.log(trace_id, "analyst_agent", "a2a_task_completed", {"answer": answer})
        await event_queue.enqueue_event(new_agent_text_message(answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消未实现: 正在跑的 ReAct 循环没有可中断点(委派靠 a2a_timeout 兑底)。"""
        raise NotImplementedError(
            "Analyst_Agent 不支持 A2A cancel: 编排层不得依赖取消来回收资源"
        )
