"""单域 ReAct 的有界横向委派工具: 本域答不了的另一半交给对等智能体。

背景: 意图漏斗只产出**一个** target, 而用户一句话里可能有两类子问题
(如"年假剩多少。部门预算呢" 被判定为 hr)。单域 ReAct 手里只有本域 MCP 工具,
第二问只能回"我查不到" —— 用户得再点选/重问一次。

这里补的是一条**工具级**出口, 与已下线的"入口层 LLM 自动拆句"不是一回事:
- 不改变单意图路由, 不把整句并发分发给多个智能体, 不合并多份回答;
- 只在该域 ReAct 已经接手、且用户消息里确实存在域外子问题时, 由模型调用一次;
- 目标域必须是当前角色白名单内的对等智能体, 调用前过 ``check_agent_permission``;
- 每轮最多 ``mcp_peer_delegate_max_calls`` 次, 由本轮的 :class:`DelegationContext` 计数;
- 转发**原始调用者身份** metadata, 对端各自再执行一遍 ACL, 不借网关洗权。

委派上下文走 ContextVar 而不是工具对象属性: ReAct agent 按 (域, 角色, 工具清单)
缓存复用, 把 trace_id/计数挂在工具上会让并发轮次互相串号。
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool

from app.assistant.a2a_client import AGENT_PROFILES, get_a2a_pool, is_agent_domain
from app.assistant.askdata_bridge import split_askdata_payload
from app.schemas import Role
from app.security.audit import get_audit_logger
from app.security.auth import AGENT_WHITELIST, PermissionDenied, check_agent_permission
from app.security.caller import current_caller

TOOL_NAME = "delegate_to_agent"


@dataclass
class DelegationContext:
    """一轮单域 ReAct 的委派上下文(每轮新建, 随 ContextVar 传播)。"""

    source_domain: str
    trace_id: str = ""
    session_id: str = ""
    max_calls: int = 2
    calls: int = 0


_DELEGATION_CTX: ContextVar[DelegationContext | None] = ContextVar(
    "peer_delegation_ctx", default=None
)


def set_delegation_context(ctx: DelegationContext):
    """在图节点内设置本轮上下文; 返回 token 供 finally 复位。"""
    return _DELEGATION_CTX.set(ctx)


def reset_delegation_context(token) -> None:
    _DELEGATION_CTX.reset(token)


def allowed_peer_domains(role: Role, source_domain: str) -> list[str]:
    """当前角色可委派、且不是当前域自身的对等域(顺序与 ``AGENT_PROFILES`` 一致)。"""
    granted = AGENT_WHITELIST.get(role, set())
    return [
        domain
        for domain in AGENT_PROFILES
        if domain != source_domain and f"{domain}_agent" in granted
    ]


def peer_prompt_block(role: Role, source_domain: str, max_calls: int) -> str:
    """注入单域 ReAct 的 system 消息: 何时该委派、可委派给谁、硬上限。"""
    peers = allowed_peer_domains(role, source_domain)
    if not peers:
        return ""
    lines = [
        "跨域子问题处理(本轮适用):",
        f"- 用户一句话里可能包含多个子问题。属于 {source_domain} 域的部分由你正常回答。",
        "- 若其中某个子问题明确属于下列其他域、且本域工具确实无法回答, 必须调用 "
        f"{TOOL_NAME}(domain, task) 把**该子问题**交给对应智能体, 再如实转述它的结果;",
        "  只传子问题本身(含用户指定的部门/姓名/时间范围), 不要把整句原样转发, 不要编造数字。",
        "- 本域工具能回答的部分不要委派; 委派失败或无权限时如实说明, "
        "并提示用户可在输入框上方点选对应智能体。",
    ]
    for domain in peers:
        _, brief = AGENT_PROFILES.get(domain, (domain, ""))
        lines.append(f"  · {domain}: {brief}")
    lines.append(f"- 本轮最多委派 {max(1, max_calls)} 次。")
    return "\n".join(lines)


def build_peer_delegate_tool(source_domain: str, role: Role) -> Any | None:
    """按 (来源域, 角色) 构建委派工具; 无可委派域时返回 None(不注入)。

    工具本身无状态: 身份与链路号在调用时从 ContextVar / 调用者上下文读取, 因此可以
    随 ReAct agent 一起缓存复用。
    """
    peers = allowed_peer_domains(role, source_domain)
    if not peers:
        return None
    audit = get_audit_logger()
    peer_brief = "; ".join(
        f"{domain}({AGENT_PROFILES.get(domain, (domain, ''))[1]})" for domain in peers
    )

    async def _delegate(domain: str, task: str) -> str:
        dom = (domain or "").strip().lower()
        text = (task or "").strip()
        if dom not in peers or not is_agent_domain(dom):
            return f"非法委派域 {domain!r}; 本轮可用: {', '.join(peers)}。"
        if not text:
            return "委派任务为空, 未执行。"

        ctx = _DELEGATION_CTX.get()
        if ctx is None:
            return "委派上下文缺失, 本轮无法跨域办理。"
        if ctx.calls >= max(1, ctx.max_calls):
            return f"本轮跨域委派已达上限({max(1, ctx.max_calls)} 次), 未执行。"

        caller = current_caller()
        caller_role = caller.role if caller is not None else role.value
        caller_user = caller.user_id if caller is not None else ""
        try:
            role_obj = Role(caller_role)
        except ValueError:
            role_obj = Role.EMPLOYEE
        # 权限门与编排层同源: 低权用户不能借当前域洗权到其他智能体。
        try:
            check_agent_permission(role_obj, f"{dom}_agent")
        except PermissionDenied as exc:
            return f"无权限委派到 {dom} 智能体: {exc}"

        ctx.calls += 1
        metadata = {
            "user_id": caller_user,
            "role": role_obj.value,
            # 对端将来支持"对端再委派"时据此限制递归; 当前单层委派不依赖它。
            "delegation_depth": 1,
        }
        started = time.monotonic()
        audit.log(
            ctx.trace_id, "assistant", "peer_delegated",
            {"from": source_domain, "domain": dom, "role": role_obj.value,
             "task": text[:500]},
            ctx.session_id or None,
        )
        try:
            reply = await get_a2a_pool().send_guarded(dom, text, metadata=metadata)
        except Exception as exc:  # noqa: BLE001 - 协同失败只降级这一问, 不炸整轮
            reply = f"委派 {dom} 智能体失败: {exc}"
        # 问数对端会在正文末尾附 fenced JSON; 嵌在别人回答里只会变成噪音。
        reply, _ = split_askdata_payload(reply)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        audit.log(
            ctx.trace_id, "assistant", "peer_delegated_done",
            {"from": source_domain, "domain": dom, "elapsed_ms": elapsed_ms,
             "replied": bool(reply), "reply": reply[:500]},
            ctx.session_id or None,
        )
        return reply

    return StructuredTool.from_function(
        coroutine=_delegate,
        name=TOOL_NAME,
        description=(
            "把用户这句话里一个明确属于其他业务域、且本域工具无法回答的子问题, "
            "委派给对应专业智能体并取回结果。只在确实存在跨域子问题时使用; "
            "本域能办的不要委派, 也不要把整句原样转发。"
            f"可委派域: {peer_brief}。"
            "参数 domain 取上述域键之一; task 只写该子问题的完整自然语言描述"
            "(含用户指定的对象与时间范围)。"
        ),
    )
