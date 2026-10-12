"""AskData_Agent 业务逻辑 + A2A AgentExecutor。

智能问数专业智能体: 一个跑在 askdata MCP server 之上的**确定性状态机**(不是自由 ReAct)。
范式来自 contract_agent 的 DAG 升级: 主干每步形状固定、可单测可回放, 只有结果校验失败时
才拉起一个有界的只读自修复循环。

与其它专业智能体同源的安全边界:
- 身份只信 A2A ``Message.metadata``; 缺失按 employee 最小权限(在本域等于无工具)。
- 本容器不持任何数据库凭据(层 0): 取数、校验、编译全部经 askdata MCP 工具, 于是
  "模型能不能绕过校验器"这件事根本没有代码路径可走。
- 行级隔离在数据库 RLS + 服务端作用域注入上, 提示词只描述边界不做安全承诺。
- 结果里的文字同样是不可信输入: 出口有 DLP 打码与 spotlighting(在 MCP server 侧做),
  叙述段读到的是已经打码与标记过的数据。

对外契约: 回复正文 = 给人看的答案; 末尾附一段 fenced `````askdata````` JSON 载荷
(澄清选项/结果表/图表/口径), 由网关 ``app/assistant/askdata_bridge`` 剥离后填进
``ChatResponse.metadata``。载荷之外正文自身仍然完整可读 —— 这样即使前端不认这个
载荷(旧版页面), 用户也照样能看懂答案与澄清选项。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message

from app.agents.askdata_agent.graph import AskDataPipeline, AskDataState
from app.config import get_settings
from app.schemas import Role
from app.security.audit import get_audit_logger
from app.security.caller import Caller, reset_caller, set_caller

logger = logging.getLogger(__name__)

PAYLOAD_FENCE = "askdata"

_DISABLED_TEXT = (
    "智能问数能力当前未开启(ASKDATA_ENABLED=false)。需要按口径问数请联系管理员开启; "
    "如果只是想查跨域统计并出图, 可以改用数据分析(Analyst_Agent)。"
)


def format_payload(answer: str, payload: dict[str, Any] | None) -> str:
    """把结构化载荷附在正文之后(单一出口, 保证正文与载荷的比例在所有路径上一致)。"""
    if not payload:
        return answer
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:  # noqa: BLE001 - 载荷序列化失败绝不能毁掉答案
        logger.warning("[askdata] 结构化载荷序列化失败(只发正文): %s", exc)
        return answer
    return f"{answer}\n\n```{PAYLOAD_FENCE}\n{text}\n```"


class AskDataAgent:
    """问数智能体本体(进程级单例; 状态机本身无状态, 每轮一个 state)。"""

    def __init__(self) -> None:
        self._pipeline = AskDataPipeline()

    async def invoke(
        self,
        user_text: str,
        user_id: str,
        role: Role,
        trace_id: str = "",
        session_id: str = "",
        prior_slots: dict[str, Any] | None = None,
        prior_dsl: dict[str, Any] | None = None,
        prior_top_metric: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """跑一轮问数, 返回 ``(给人看的正文, 结构化载荷)``。"""
        settings = get_settings()
        if not settings.askdata_enabled:
            return _DISABLED_TEXT, {}
        state: AskDataState = {
            "question": user_text,
            "prior_slots": prior_slots or {},
            # 上一轮的 DSL 与最高分口径: 前者给指代消解用, 后者只用来判误澄清。
            "prior_dsl": prior_dsl or {},
            "prior_top_metric": prior_top_metric or "",
            "user_id": user_id or "",
            "role": role.value,
            "trace_id": trace_id or "",
            "session_id": session_id or "",
            "started_at": time.monotonic(),
            "notes": [],
            "attempts": [],
            "rewrite_count": 0,
            "token_cost": 0,
            "llm_calls": 0,
        }
        result = await self._pipeline.run(state)
        payload = result.get("payload") or {}
        # 载荷里的 slots 是下一轮做确定性继承的唯一依据, 不能省。
        payload.setdefault("slots", result.get("slots") or {})
        answer = result.get("answer") or "没能生成答案, 请换个说法再问一次。"
        return answer, payload


class AskDataAgentExecutor(AgentExecutor):
    """A2A ``AgentExecutor`` 桥: A2A task -> 一轮问数状态机。"""

    def __init__(self) -> None:
        self._agent = AskDataAgent()
        self._audit = get_audit_logger()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        trace_id = context.task_id or context.context_id or "unknown"

        # 身份只信协议级 metadata; metadata 缺失按 employee 处理(默认拒)。
        metadata: dict[str, Any] = getattr(context.message, "metadata", None) or {}
        user_id = str(metadata.get("user_id") or "")
        try:
            role = Role(str(metadata.get("role") or Role.EMPLOYEE.value))
        except ValueError:
            role = Role.EMPLOYEE
        prior_slots = metadata.get("slots") if isinstance(metadata.get("slots"), dict) else {}
        prior_dsl = metadata.get("prior_dsl") if isinstance(metadata.get("prior_dsl"), dict) else {}
        prior_top = str(metadata.get("prior_top_metric") or "")
        session_id = str(metadata.get("session_id") or "")
        # 问题以网关单独下发的 metadata["question"] 为准(不带"对话背景/当前时间"前缀);
        # 缺失时退回消息原文, 行为与没这个字段时一致。
        question = str(metadata.get("question") or "").strip() or user_input

        self._audit.log(
            trace_id, "askdata_agent", "a2a_task_received",
            {"input": question, "user_id": user_id or "unknown", "role": role.value,
             "prior_slots": bool(prior_slots)},
        )
        # 调用者身份带进 ContextVar: 状态机里程序化调用 MCP 工具时要从同一处取 caller_*,
        # 不能靠把工号拼进文本(那等于让模型有机会改写它)。
        token = set_caller(Caller(
            user_id=user_id, role=role.value, intent_text=question, trace_id=trace_id
        ))
        started = time.monotonic()
        try:
            answer, payload = await self._agent.invoke(
                question, user_id=user_id, role=role, trace_id=trace_id,
                session_id=session_id, prior_slots=prior_slots,
                prior_dsl=prior_dsl, prior_top_metric=prior_top,
            )
        except Exception as exc:  # noqa: BLE001 - 状态机抛出来的一切只能兑成一句可读降级
            logger.exception("[askdata] 问数状态机异常")
            answer, payload = f"问数处理失败: {type(exc).__name__}: {str(exc)[:200]}", {}
        finally:
            reset_caller(token)
        elapsed = int((time.monotonic() - started) * 1000)
        self._audit.log(
            trace_id, "askdata_agent", "a2a_task_completed",
            {"answer": answer[:1500], "verdict": payload.get("verdict", ""),
             "route": payload.get("route", ""), "elapsed_ms": elapsed},
        )
        await event_queue.enqueue_event(new_agent_text_message(
            format_payload(answer, payload if _wants_payload(payload) else None)
        ))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消未实现: 状态机里的 LLM/工具调用没有可中断点(委派靠 a2a_timeout 兑底)。"""
        raise NotImplementedError(
            "AskData_Agent 不支持 A2A cancel: 编排层不得依赖取消来回收资源"
        )


def _wants_payload(payload: dict[str, Any]) -> bool:
    """没开启/异常降级这类纯文本回复不值得附载荷(前端会渲染出一个空面板)。"""
    if not payload:
        return False
    return bool(payload.get("kind"))
