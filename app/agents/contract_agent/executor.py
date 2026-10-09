"""Contract_Agent business logic + A2A AgentExecutor.

采购与合同初审专业智能体: 挂在 procurement MCP server 上的 LangGraph ReAct agent。

"规则保底 + 模型加分"的编排(本智能体区别于其它专业智能体的核心):
1. 合同初审: 先调 check_contract_clauses(进程内确定性规则: 必备条款缺失/高风险表述/
   金额分级/供应商与账号一致性), 拿到不可被模型漏判的红线清单; 再由模型阅读原文补充
   规则覆盖不到的语义风险(如表述含糊、显失公平但未命中关键词的条款)。
2. 台账留痕: 需要归档时 submit_contract_review 落规则结论, save_contract_opinion 回写
   模型的条款抽取与语义风险(工具侧只允许升级风险等级, 不允许降级规则已判定的红线)。
3. 采购单: create_purchase_order -> precheck_purchase_order 出初审; 用户问"这样买行不行"
   用 check_purchase_compliance 预演, 不落单据。

权限分级与其它智能体同源:
- 角色×工具白名单矩阵 (app.security.auth.PROCUREMENT_TOOL_WHITELIST) 硬控制可见工具;
  execute_sql(跨全员采购/合同统计)仅管理角色。
- 身份只信 A2A Message.metadata, 缺失按 employee 最小权限。
"""

from __future__ import annotations

import re
import time
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.agents.common_tools import take_lookup_tool, with_lookup_tool
from app.config import get_settings
from app.db.schema_docs import PROCUREMENT_SCHEMA_DDL
from app.llm import get_chat_model
from app.schemas import Role
from app.security.audit import get_audit_logger
from app.security.auth import filter_tools_for_role
from app.security.caller import Caller, bind_caller_tools, reset_caller, set_caller

_EMPLOYEE_TAG_RE = re.compile(r"\[employee_id=([A-Za-z0-9_\-]+)\]")

_ROLE_LABELS: dict[Role, str] = {
    Role.EMPLOYEE: "普通员工",
    Role.MANAGER: "部门经理",
    Role.HR: "HR专员",
    Role.FINANCE: "财务专员",
    Role.ADMIN: "管理员",
}

_BASE_PROMPT = """你是 Contract_Agent,企业采购与合同初审专业智能体。
当前操作用户权限层级: {role_label}。

职责:
1. 目标员工解析: 用户只提供姓名、未提供工号时, 先调用 lookup_employee_by_name 解析申请人工号;
   若返回 needs_selection=True(同名多人), 列出候选(姓名+工号+部门)请用户明确选择, 不要猜工号。
2. 采购办理: 收集事项/金额/类别/供应商/比价份数后调用 create_purchase_order 建单,
   再调用 precheck_purchase_order 出具合规初审结论(比价/供应商准入/预算余额)。
   用户只是问"这样买行不行/需要几家比价"时, 用 check_purchase_compliance 预演, 不要落单据。
3. 合同初审(必须两段式):
   a) 先调用 check_contract_clauses 拿到规则引擎的确定性结论(必备条款缺失、高风险表述、
      供应商与收款账号一致性、金额红线); 这是底线判定, 不能跳过、不能凭记忆替代。
   b) 再基于合同原文补充规则可能漏掉的语义风险(表述含糊、权利义务不对等但未命中关键词等)。
   需要归档时调用 submit_contract_review 落台账, 并把你的补充结论用 save_contract_opinion
   回写(risk_level 只能等于或高于规则结论, 不得降级红线)。
4. 查询: 采购单/合同/供应商分别用 query_purchase_order / query_contract / query_supplier,
   列表用 list_purchase_orders / list_contracts / list_suppliers。
{capabilities}

业务表结构 (只读, 供 Text2SQL 生成 SQL 参考):
{schema}

Text2SQL 规则 (仅当 execute_sql 在你本次可用工具列表中时适用):
- 只写单条 SELECT; 只查白名单内的表; 需要部门/姓名时 JOIN hr_employees。
- 若返回 error, 依据错误修正后重试一次; 仍失败如实告知, 不要编造结果。

规则:
- 缺少必填信息(金额/类别/供应商/合同正文)时主动追问, 不要编造。
- 合同正文为空时, check_contract_clauses/submit_contract_review 会报错; 此时请用户粘贴
  合同全文, 或先入库知识库后用 doc_key, 不要臆测条款。
- 工具返回的 error 字段必须如实转达。
- 初审是"初筛建议", 最终放行由法务/财务人工决定; 结论里要体现这一点, 不要说"已批准签署"。
- 采购类别仅限: IT设备/办公用品/咨询服务/市场推广/培训服务/其他。
- 系统上下文给出当前登录操作者 employee_id; 仅代表登录态, 办理"我/本人"的采购且未指定
  他人时才默认用操作者工号, 用户指定他人则以指定为准。
- 用简洁中文回复。

权限边界(必须严格遵守):
- 你只能使用系统提供的工具; 若某工具不在本次可用工具列表中, 不要尝试调用, 更不要编造结果。
- 若用户请求超出当前权限层级的能力, 礼貌说明无权限, 并建议其联系部门经理或财务/采购专员。"""

_EMPLOYEE_CAPABILITIES = """当前角色可用采购工具: 采购单创建/初审/查询、合同条款初审与送审、
供应商查询(check_contract_clauses、submit_contract_review、create_purchase_order 等)。
注意: 采购数据统计查询(execute_sql, 可查全员采购/合同)对普通员工不可用。"""

_MANAGER_CAPABILITIES = """当前角色可用 procurement 域全量工具(含 execute_sql Text2SQL 只读统计)。"""

_SPECIALIST_CAPABILITIES = _MANAGER_CAPABILITIES

_ROLE_CAPABILITIES: dict[Role, str] = {
    Role.EMPLOYEE: _EMPLOYEE_CAPABILITIES,
    Role.MANAGER: _MANAGER_CAPABILITIES,
    Role.HR: _SPECIALIST_CAPABILITIES,
    Role.FINANCE: _SPECIALIST_CAPABILITIES,
    Role.ADMIN: _SPECIALIST_CAPABILITIES,
}


def _build_role_prompt(role: Role) -> str:
    """动态 System Prompt: 按角色注入权限层级、可用工具与业务表结构。"""
    return _BASE_PROMPT.format(
        role_label=_ROLE_LABELS[role],
        capabilities=_ROLE_CAPABILITIES[role],
        schema=PROCUREMENT_SCHEMA_DDL,
    )


class ContractAgent:
    """LangGraph ReAct agent over procurement MCP tools, role-aware."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=0)
        self._tools: list[Any] | None = None
        self._tools_at = 0.0
        self._lookup: Any | None = None
        self._agents: dict[Role, Any] = {}

    def _ttl(self) -> float:
        return max(1.0, float(self._settings.mcp_tools_ttl))

    async def _ensure_agent(self, role: Role = Role.EMPLOYEE) -> Any:
        """Lazily connect to the procurement MCP server and build a per-role ReAct graph.

        工具清单按 ``mcp_tools_ttl`` 过期重发(与编排层同一口径), 清单变了就连 agent 一
        起重建; 原先一次发现后永不刷新。本域工具全部带归属校验(单据/合同台账只能
        碰自己送审的), 所以身份必须逐请求注入。
        """
        if self._tools is None or time.monotonic() - self._tools_at > self._ttl():
            client = MultiServerMCPClient(
                {
                    "procurement": {
                        "url": self._settings.procurement_mcp_url, "transport": "streamable_http"
                    },
                    # 层 0(收回跨域凭证): 本进程不再直连库, "姓名->工号"由数据属域
                    # (HR MCP server)代做; 两个 server 的工具分开取, 避开同名
                    # execute_sql 合并后重名。
                    "hr": {"url": self._settings.hr_mcp_url, "transport": "streamable_http"},
                }
            )
            self._tools = await client.get_tools(server_name="procurement")
            self._lookup = take_lookup_tool(await client.get_tools(server_name="hr"))
            self._tools_at = time.monotonic()
            self._agents.clear()
        if role not in self._agents:
            # 权限Mask: 与编排层共用同一张角色×工具白名单矩阵 (硬控制)。
            tools = filter_tools_for_role(role, "procurement", self._tools)
            # 各域共用的"姓名->工号"基础解析能力注入。
            tools = with_lookup_tool(tools, self._lookup)
            tools = bind_caller_tools(tools)
            self._agents[role] = create_agent(self._llm, tools, system_prompt=_build_role_prompt(role))
        return self._agents[role]

    async def invoke(self, user_text: str, user_id: str, role: Role) -> str:
        """Run one delegated procurement/contract task under the given protocol-level identity."""
        agent = await self._ensure_agent(role)
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={user_id or 'anonymous'}; 当前角色 role={role.value}。"
            "操作者身份仅代表登录态, 不等于业务目标用户: 若用户消息中指定了申请人(工号/姓名),"
            " 以消息指定的为准; 仅当代办\"我/本人\"的采购/送审且未指定他人时, 才默认使用操作者"
            " employee_id。caller_* 字段由系统注入且会覆盖你填的值, 无需也不要在工具参数里传它们。"
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
        return "采购合同智能体未能生成有效回复。"


class ContractAgentExecutor(AgentExecutor):
    """A2A AgentExecutor bridge: A2A task -> ContractAgent invocation."""

    def __init__(self) -> None:
        self._agent = ContractAgent()
        self._audit = get_audit_logger()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        trace_id = context.task_id or context.context_id or "unknown"

        # 身份只信协议级 metadata; 用户文本中的角色字样一律忽略, 防伪造。
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
            trace_id, "contract_agent", "a2a_task_received",
            {"input": user_input[:200], "user_id": user_id or "unknown", "role": role.value},
        )
        try:
            answer = await self._agent.invoke(user_input, user_id=user_id, role=role)
        except Exception as exc:  # noqa: BLE001
            answer = f"采购合同智能体处理失败: {exc}"
        self._audit.log(trace_id, "contract_agent", "a2a_task_completed", {"answer": answer})
        await event_queue.enqueue_event(new_agent_text_message(answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消未实现: 正在跑的 ReAct 循环没有可中断点(委派靠 a2a_timeout 兑底)。"""
        raise NotImplementedError(
            "Contract_Agent 不支持 A2A cancel: 编排层不得依赖取消来回收资源"
        )
