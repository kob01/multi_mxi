"""Finance_Agent business logic: 第三代 Agentic AI(自主决策 + 协同执行)。

从第二代"单循环隐式 ReAct"升级为显式 **Plan-and-Execute + 反思闭环**, 并作为对等
智能体横向委派其他 A2A 专业智能体。对外 A2A 契约(:class:`FinanceAgentExecutor`、
:meth:`FinanceAgent.invoke`)保持不变, 编排层零改动。

三段自主决策(LangGraph StateGraph):
- planner: 用 json_mode LLM 把目标拆成有序子任务(失败降级为"单子任务=原目标")。
- executor: 逐个子任务跑一个**有界 ReAct 工作者**(复用 create_agent), 每步墙钟上限
  ``finance_step_timeout``, 产出观察。
- reflector: json_mode 复核 finish/replan/ask_confirm; replan 续排受
  ``finance_max_replan_rounds`` 限制, 全程受 ``finance_max_plan_steps`` 封顶, 绝不无界自旋。

协同执行(横向 A2A peer):
- 注入进程内工具 ``delegate_to_agent(domain, task)``, 允许域 hr/analytics/procurement
  (绝不含 finance 自身)。调用前先过 ``check_agent_permission``(与编排层同一口径, 防低权
  用户借 finance 洗权), 再转发**原始调用者身份** metadata 给对端, 让对端各自执行 ACL。
- 依赖对端 agent 服务地址在容器内可达(见 docker-compose finance-agent 的 *_AGENT_URL 注入);
  不可达/超时由 send_guarded 回可读降级文本, 不阻断本轮。

写操作安全边界(人工确认, 硬控制而非仅提示词):
- agentic 路径下 ``create_reimbursement`` 被包一层确认门: 只有调用者本轮原句(经服务端
  注入的 ``Caller.intent_text``, 模型伪造不了)命中确认词才真正放行, 否则拒绝并引导先调
  ``preview_reimbursement`` 出草稿。读/查询/统计/peer 协同可自主完成。

权限分级(沿用既有同源机制):
- 角色×工具白名单矩阵(app.security.auth)决定每个角色可见的 MCP 工具(硬控制)。
- 按角色动态生成 System Prompt 声明能力边界(软控制)。
- 角色只信 A2A Message.metadata(协议级结构化字段), 缺失按 employee 最小权限。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, TypedDict

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph

from app.agents.common_tools import take_lookup_tool, with_lookup_tool
from app.agents.finance_agent import prompts
from app.assistant.a2a_client import get_a2a_pool
from app.config import get_settings
from app.db.schema_docs import FINANCE_SCHEMA_DDL
from app.llm import get_chat_model
from app.schemas import Role
from app.security.audit import get_audit_logger
from app.security.auth import (
    AGENT_WHITELIST,
    PermissionDenied,
    check_agent_permission,
    filter_tools_for_role,
)
from app.security.caller import (
    Caller,
    bind_caller_tools,
    current_caller,
    reset_caller,
    set_caller,
)

logger = logging.getLogger(__name__)

_EMPLOYEE_TAG_RE = re.compile(r"\[employee_id=([A-Za-z0-9_\-]+)\]")

# 可横向委派的智能体域(绝不包含 finance 自身, 杜绝自委派环)。
PEER_DOMAINS: tuple[str, ...] = ("hr", "analytics", "procurement")

# 写操作确认门: 只有调用者本轮原句命中"对草稿的独立表态"才允许真正落单。
# 刻意不收"提交/可以/没问题/ok"这类首轮就会自然出现的动作词/泛化词(否则"帮我提交
# 报销单"这种提单请求会直接命中而架空"先出草稿再确认"的两轮语义)。真正的确认是
# 用户对已展示草稿的答语: 确认/确定/同意/批准/yes/confirm。
_CONFIRM_TOKENS = ("确认", "确定", "同意", "批准", "批准提交", "yes", "confirm")


def _has_confirmation(text: str) -> bool:
    lowered = (text or "").lower()
    return any(tok in lowered for tok in _CONFIRM_TOKENS)


def _clip(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出里稳健地抠出一个 JSON 对象(容忍 ``` 包裹与前后杂字)。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


def _valid_step(step: Any) -> dict[str, Any] | None:
    """规整单个子任务: 只保留合法 kind/peer_domain, 非法 peer 直接剥掉。"""
    if not isinstance(step, dict):
        return None
    intent = str(step.get("intent") or "").strip()
    if not intent:
        return None
    kind = str(step.get("kind") or "read").strip().lower()
    if kind not in ("read", "query", "analyze", "write", "peer"):
        kind = "read"
    peer_domain = str(step.get("peer_domain") or "").strip().lower()
    if peer_domain not in PEER_DOMAINS:
        peer_domain = ""
    return {
        "intent": intent,
        "kind": kind,
        "peer_domain": peer_domain,
        "hint": str(step.get("hint") or "").strip(),
    }


def _allowed_peer_domains(role: Role) -> list[str]:
    """当前角色可横向委派的对端域(与 AGENT_WHITELIST 同源, 不含 finance)。"""
    allowed_agents = AGENT_WHITELIST.get(role, set())
    return [d for d in PEER_DOMAINS if f"{d}_agent" in allowed_agents]


def _gate_write_tool(tool: Any) -> Any:
    """给写工具包一层人工确认门(读当前上下文里的调用者原句判定)。

    放在 ``bind_caller_tools`` 之前: 门放行时把 kwargs(含后续注入的 caller_*)原样转发
    给底层工具; 不放行时直接回可读的拒绝体, 引导先出草稿再确认。
    """

    async def _coro(**kwargs):
        caller = current_caller()
        intent_text = caller.intent_text if caller is not None else ""
        if not _has_confirmation(intent_text):
            return {
                "error": "写操作需用户明确确认后才执行; 请先调用 preview_reimbursement 校验并出草稿, "
                         "向用户复述要点并请求确认, 待用户回复确认后再提交。",
                "needs_confirmation": True,
            }
        return await tool.ainvoke(kwargs)

    return StructuredTool.from_function(
        coroutine=_coro,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


class FinanceState(TypedDict, total=False):
    """自主决策闭环的共享状态。"""

    goal: str           # 用户原句(本轮目标)
    user_id: str
    role: Role
    trace_id: str
    plan: list[dict[str, Any]]
    step_idx: int
    observations: list[str]
    replan_left: int
    steps_used: int
    verdict: str        # finish | ask_confirm | replan
    confirm_reason: str
    final: str


class FinanceAgent:
    """第三代 Agentic: plan-execute-reflect 闭环 + 横向 A2A 协同, 角色感知。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=0)
        # planner/reflector 走结构化 JSON 短任务通道(关闭思考, 降时延)。
        self._json_llm = get_chat_model(settings.llm_model, temperature=0, json_mode=True)
        self._tools: list[Any] | None = None
        self._tools_at = 0.0
        self._lookup: Any | None = None
        self._delegate_tool: Any | None = None
        self._step_agents: dict[Role, Any] = {}
        self._legacy_agents: dict[Role, Any] = {}
        self._graph: Any | None = None
        self._audit = get_audit_logger()

    def _ttl(self) -> float:
        return max(1.0, float(self._settings.mcp_tools_ttl))

    def _step_tools(self, role: Role) -> list[Any]:
        """agentic 步骤工作者可见的工具集(带确认门的写工具 + peer 委派)。"""
        tools = filter_tools_for_role(role, "finance", self._tools or [])
        tools = with_lookup_tool(tools, self._lookup)
        if self._delegate_tool is not None:
            tools = [*tools, self._delegate_tool]
        # 写工具加人工确认门(仅 agentic 路径); 门在 bind 之前包, 转发时 caller_* 已合入。
        tools = [
            _gate_write_tool(t) if getattr(t, "name", "") == "create_reimbursement" else t
            for t in tools
        ]
        return bind_caller_tools(tools)

    def _legacy_tools(self, role: Role) -> list[Any]:
        """回滚路径工具集: 与升级前完全同构(无 peer, 无写门)。"""
        tools = filter_tools_for_role(role, "finance", self._tools or [])
        tools = with_lookup_tool(tools, self._lookup)
        return bind_caller_tools(tools)

    def _ensure_delegate_tool(self) -> None:
        """惰性构建 peer 委派工具(协程读 current_caller, 与角色无关, 只建一次)。"""
        if self._delegate_tool is not None:
            return

        async def _delegate(domain: str, task: str) -> str:
            dom = (domain or "").strip().lower()
            if dom == "finance" or dom not in PEER_DOMAINS:
                return f"非法委派域 {domain!r}; 可委派域: {', '.join(PEER_DOMAINS)}。"
            caller = current_caller()
            role_val = caller.role if caller is not None else Role.EMPLOYEE.value
            try:
                role = Role(role_val)
            except ValueError:
                role = Role.EMPLOYEE
            # 权限门: 与编排层同一口径, 防止低权用户借 finance 洗权到其对等智能体。
            try:
                check_agent_permission(role, f"{dom}_agent")
            except PermissionDenied as exc:
                return f"无权限委派到 {dom} 智能体: {exc}"
            metadata = {"user_id": caller.user_id, "role": role.value} if caller else {}
            trace_id = caller.trace_id if caller is not None else "unknown"
            started = time.monotonic()
            self._audit.log(
                trace_id, "finance_agent", "peer_delegated",
                {"domain": dom, "task": _clip(task, 500), "role": role.value},
            )
            try:
                reply = await get_a2a_pool().send_guarded(dom, task, metadata=metadata)
            except Exception as exc:  # noqa: BLE001  # 协同失败只降级该步观察, 不炸整轮
                reply = f"委派 {dom} 智能体失败: {exc}"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._audit.log(
                trace_id, "finance_agent", "peer_delegated_done",
                {"domain": dom, "elapsed_ms": elapsed_ms, "reply": _clip(reply, 500)},
            )
            return reply

        self._delegate_tool = StructuredTool.from_function(
            coroutine=_delegate,
            name="delegate_to_agent",
            description=(
                "把一段子任务横向委派给其他 A2A 专业智能体并取回结果。仅当本域工具拿不到"
                "所需信息时使用。参数: domain(取 hr/analytics/procurement 之一), "
                "task(自然语言子任务描述, 会带上当前调用者身份)。"
            ),
        )

    async def _ensure_tools(self) -> None:
        """按 TTL 惰性发现 finance + hr MCP 工具; 清单变了就丢弃所有缓存的 agent/图。"""
        if self._tools is not None and time.monotonic() - self._tools_at <= self._ttl():
            return
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(
            {
                "finance": {"url": self._settings.finance_mcp_url, "transport": "streamable_http"},
                # 层 0(收回跨域凭证): 本进程不直连库, "姓名->工号"由数据属域(HR MCP)代做;
                # 两个 server 的工具分开取, 避开同名 execute_sql 合并后重名。
                "hr": {"url": self._settings.hr_mcp_url, "transport": "streamable_http"},
            }
        )
        self._tools = await client.get_tools(server_name="finance")
        self._lookup = take_lookup_tool(await client.get_tools(server_name="hr"))
        self._tools_at = time.monotonic()
        self._step_agents.clear()
        self._legacy_agents.clear()
        self._graph = None
        if self._settings.finance_peer_delegate_enabled:
            self._ensure_delegate_tool()

    # ------------------------------------------------------------------ 节点
    def _peer_hint(self, role: Role) -> str:
        if not self._settings.finance_peer_delegate_enabled:
            return "未启用(本轮不可横向委派其他智能体)"
        allowed = _allowed_peer_domains(role)
        return ", ".join(allowed) if allowed else "无(当前角色无可委派的跨域智能体)"

    async def _node_planner(self, state: FinanceState) -> dict[str, Any]:
        role = state["role"]
        prompt = prompts.PLANNER_PROMPT.format(
            role_label=prompts.ROLE_LABELS[role],
            capabilities=prompts.ROLE_CAPABILITIES[role],
            peer_domains=self._peer_hint(role),
            max_steps=self._settings.finance_max_plan_steps,
        )
        msg = await self._json_llm.ainvoke(
            [("system", prompt), ("user", f"用户目标: {state['goal']}")]
        )
        parsed = _parse_json(str(msg.content))
        steps: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            for raw in parsed.get("steps") or []:
                step = _valid_step(raw)
                if step:
                    steps.append(step)
        if not steps:
            # 解析失败降级: 单子任务=原目标, 交给通用工作者直接办, 不阻断。
            steps = [{"intent": state["goal"], "kind": "read", "peer_domain": "", "hint": ""}]
        steps = steps[: self._settings.finance_max_plan_steps]
        self._audit.log(
            state["trace_id"], "finance_agent", "plan_created",
            {"steps": [s["intent"] for s in steps], "role": role.value},
        )
        return {"plan": steps, "step_idx": 0, "observations": [], "steps_used": len(steps)}

    async def _ensure_step_agent(self, role: Role) -> Any:
        if role in self._step_agents:
            return self._step_agents[role]
        prompt = prompts.STEP_EXECUTOR_PROMPT.format(
            role_label=prompts.ROLE_LABELS[role],
            capabilities=prompts.ROLE_CAPABILITIES[role],
            schema=FINANCE_SCHEMA_DDL,
            peer_domains=self._peer_hint(role),
        )
        agent = create_agent(self._llm, self._step_tools(role), system_prompt=prompt)
        self._step_agents[role] = agent
        return agent

    async def _node_executor(self, state: FinanceState) -> dict[str, Any]:
        idx = state["step_idx"]
        plan = state["plan"]
        if idx >= len(plan):
            return {}
        step = plan[idx]
        role = state["role"]
        agent = await self._ensure_step_agent(role)
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={state['user_id'] or 'anonymous'}; 当前角色 role={role.value}。"
            "caller_* 字段由系统注入且会覆盖你填的值, 无需也不要在工具参数里传它们。"
        )
        user_msg = (
            f"整体目标: {state['goal']}\n\n"
            f"当前子任务: {step['intent']}\n"
            f"子任务类型: {step['kind']}"
            + (f"\n目标委派域: {step['peer_domain']}" if step.get("peer_domain") else "")
            + (f"\n执行提示: {step['hint']}" if step.get("hint") else "")
            + "\n请只完成当前子任务并报告观察结果。"
        )
        self._audit.log(
            state["trace_id"], "finance_agent", "step_started",
            {"idx": idx, "intent": step["intent"], "kind": step["kind"]},
        )
        try:
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [("system", system_context), ("user", user_msg)]}),
                timeout=max(1.0, float(self._settings.finance_step_timeout)),
            )
            observation = ""
            for msg in reversed(result["messages"]):
                if isinstance(msg, AIMessage) and msg.content:
                    observation = str(msg.content)
                    break
            if not observation:
                observation = "(该步未产出有效观察)"
        except TimeoutError:
            observation = f"(该步执行超过 {self._settings.finance_step_timeout:.0f}s 未完成, 已跳过)"
        except Exception as exc:  # noqa: BLE001  # 单步异常只记为该步观察, 不连坐整轮
            observation = f"(该步执行失败: {exc})"
        self._audit.log(
            state["trace_id"], "finance_agent", "step_completed",
            {"idx": idx, "observation": _clip(observation, 800)},
        )
        return {"observations": [*state["observations"], observation], "step_idx": idx + 1}

    async def _node_reflector(self, state: FinanceState) -> dict[str, Any]:
        if state["step_idx"] < len(state["plan"]):
            # 仍有排定步骤未跑: 直接回 executor, 不做反思(反思只在计划跑完后触发)。
            return {"verdict": "continue"}
        prompt = prompts.REFLECTOR_PROMPT.format(
            goal=state["goal"],
            observations="\n".join(
                f"[{i + 1}] {_clip(obs, 600)}" for i, obs in enumerate(state["observations"])
            ) or "(无)",
            max_replan_steps=self._settings.finance_max_plan_steps,
        )
        try:
            msg = await self._json_llm.ainvoke([("system", prompt), ("user", "请给出结论。")])
            parsed = _parse_json(str(msg.content))
        except Exception as exc:  # noqa: BLE001
            logger.warning("reflector 调用失败, 收口为 finish: %s", exc)
            parsed = None
        verdict = "finish"
        confirm_reason = ""
        new_steps: list[dict[str, Any]] = []
        if parsed:
            v = str(parsed.get("verdict") or "").strip().lower()
            if v in ("finish", "replan", "ask_confirm"):
                verdict = v
            confirm_reason = str(parsed.get("reason") or "").strip()
            if verdict == "replan":
                for raw in parsed.get("next_steps") or []:
                    step = _valid_step(raw)
                    if step:
                        new_steps.append(step)
        # 预算闸门: replan 需有余量且有待排步骤, 否则降级 finish。
        if verdict == "replan":
            if state["replan_left"] <= 0 or not new_steps or state["steps_used"] >= self._settings.finance_max_plan_steps:
                verdict = "finish"
            else:
                room = self._settings.finance_max_plan_steps - state["steps_used"]
                new_steps = new_steps[:max(1, room)]
                self._audit.log(
                    state["trace_id"], "finance_agent", "reflection",
                    {"verdict": "replan", "added": [s["intent"] for s in new_steps]},
                )
                return {
                    "verdict": "replan",
                    "plan": [*state["plan"], *new_steps],
                    "replan_left": state["replan_left"] - 1,
                    "steps_used": state["steps_used"] + len(new_steps),
                }
        self._audit.log(
            state["trace_id"], "finance_agent", "reflection",
            {"verdict": verdict, "reason": _clip(confirm_reason, 300)},
        )
        final = "\n\n".join(o for o in state["observations"] if o) or "财务智能体未能生成有效回复。"
        if verdict == "ask_confirm" and confirm_reason:
            final = f"{final}\n\n{confirm_reason}"
        return {"verdict": verdict, "final": final, "confirm_reason": confirm_reason}

    def _route_after_executor(self, state: FinanceState) -> str:
        return "executor" if state["step_idx"] < len(state["plan"]) else "reflector"

    def _route_after_reflector(self, state: FinanceState) -> str:
        return "executor" if state.get("verdict") == "replan" else END

    def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        g = StateGraph(FinanceState)
        g.add_node("planner", self._node_planner)
        g.add_node("executor", self._node_executor)
        g.add_node("reflector", self._node_reflector)
        g.add_edge(START, "planner")
        g.add_edge("planner", "executor")
        g.add_conditional_edges("executor", self._route_after_executor, ["executor", "reflector"])
        g.add_conditional_edges("reflector", self._route_after_reflector, ["executor", END])
        self._graph = g.compile()
        return self._graph

    # ------------------------------------------------------------------ 入口
    async def invoke(self, user_text: str, user_id: str, role: Role, trace_id: str = "unknown") -> str:
        """Run one delegated task under the given protocol-level identity.

        签名向后兼容(仅新增可选 trace_id); 关闭 agentic 时走旧单循环 ReAct 路径。
        """
        await self._ensure_tools()
        # intent_text 携带用户原句供写门判定确认; trace_id 供 peer 委派工具归并审计链。
        token = set_caller(
            Caller(user_id=user_id or "", role=role.value, intent_text=user_text, trace_id=trace_id)
        )
        try:
            if not self._settings.finance_agentic_enabled:
                return await self._legacy_invoke(user_text, user_id, role)
            graph = self._ensure_graph()
            state: FinanceState = {
                "goal": user_text,
                "user_id": user_id or "",
                "role": role,
                "trace_id": trace_id,
                "plan": [],
                "step_idx": 0,
                "observations": [],
                "replan_left": self._settings.finance_max_replan_rounds,
                "steps_used": 0,
                "verdict": "",
                "confirm_reason": "",
                "final": "",
            }
            result = await graph.ainvoke(state)
            return result.get("final") or "财务智能体未能生成有效回复。"
        finally:
            reset_caller(token)

    async def _legacy_invoke(self, user_text: str, user_id: str, role: Role) -> str:
        """回滚路径: 升级前的第二代单循环 ReAct(create_agent 全量单轮, 无写门)。"""
        if role not in self._legacy_agents:
            prompt = prompts.LEGACY_SYSTEM_PROMPT.format(
                role_label=prompts.ROLE_LABELS[role],
                capabilities=prompts.ROLE_CAPABILITIES[role],
                schema=FINANCE_SCHEMA_DDL,
            )
            self._legacy_agents[role] = create_agent(
                self._llm, self._legacy_tools(role), system_prompt=prompt
            )
        agent = self._legacy_agents[role]
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={user_id or 'anonymous'}; 当前角色 role={role.value}。"
            "操作者身份仅代表登录态, 不等于报销人/任务目标用户: 若用户消息中指定了"
            "报销人(工号/姓名), 以消息指定的为准; 仅当代办\"我/本人\"的报销业务且"
            "未指定他人时, 才默认使用操作者 employee_id 作为报销人。caller_* 字段由系统"
            "注入且会覆盖你填的值, 无需也不要在工具参数里传它们。"
        )
        # 注意: set_caller 已由 invoke 完成, 这里复用同一上下文身份。
        result = await agent.ainvoke(
            {"messages": [("system", system_context), ("user", user_text)]}
        )
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                return str(msg.content)
        return "财务智能体未能生成有效回复。"


class FinanceAgentExecutor(AgentExecutor):
    """A2A AgentExecutor bridge: A2A task -> FinanceAgent invocation."""

    def __init__(self) -> None:
        self._agent = FinanceAgent()
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
            trace_id, "finance_agent", "a2a_task_received",
            {"input": user_input, "user_id": user_id or "unknown", "role": role.value},
        )
        try:
            answer = await self._agent.invoke(user_input, user_id=user_id, role=role, trace_id=trace_id)
        except Exception as exc:  # surface a graceful failure message
            answer = f"财务智能体处理失败: {exc}"
        self._audit.log(trace_id, "finance_agent", "a2a_task_completed", {"answer": answer})
        await event_queue.enqueue_event(new_agent_text_message(answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消未实现: 正在跑的闭环没有可中断点(委派靠 a2a_timeout 兑底)。"""
        raise NotImplementedError(
            "Finance_Agent 不支持 A2A cancel: 编排层不得依赖取消来回收资源"
        )
