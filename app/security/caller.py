"""调用者身份的服务端注入与归属校验(自助业务工具的安全底座)。

这一层要修的是一条"写在注释里但没有任何一层实现"的不变式:
- ``app/security/auth.py`` 说业务工具是 self-service, 所以各角色都放行;
- ``app/cache/tool_cache.py`` 说"员工只能查自己, 管理者可查他人"。
原先两处都不成立: 单据类工具只认 LLM 传来的 ``ticket_no`` / ``user_id``, 而单号是
自增可枚举的, 姓名->工号解析又对全员开放, 于是任何调用方都能读别人的单据、取消
别人的工单、给别人的合同写意见。

三条口径:
1. **身份不由 LLM 传**。参数在模型手里, 它就可能(或被诱导)伪造; 身份由编排层在调用
   工具前注入(:func:`caller_tool_args`), 与 LLM 给出的同名字段冲突时一律覆盖。
2. **默认拒**。工具侧解析不到调用者(网关注入缺失, 例如直连 MCP)时拒绝执行, 而不是
   按"匿名 = 全权限"放行。
3. **跨人访问要角色**。``manager/hr/finance/admin`` 可跨人查办(与既有角色×工具矩阵
   口径一致), 其余角色只能触达归属自己的记录。

已知边界(与 README/REPO 文档口径一致): 上游 ``ChatRequest`` 的 ``user_id/role`` 仍是
"可信内网假设", 网关与 MCP server 之间未做服务间签名。生产接统一身份系统(JWT/OIDC)
后只需替换 :func:`set_caller` 的取值来源, 本模块的判定与工具侧代码都不用改。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

# 可跨人查办的角色(与 app.security.auth 的角色×工具矩阵同一口径)。
PRIVILEGED_ROLES = frozenset({"manager", "hr", "finance", "admin"})

# 注入进 MCP 工具参数的字段名: 工具签名里以同名可选参数接收, 编排层负责填满。
CALLER_ARG_USER_ID = "caller_user_id"
CALLER_ARG_ROLE = "caller_role"
CALLER_ARG_DEPT = "caller_department"


@dataclass(frozen=True)
class Caller:
    """服务端解析出的调用者主体(与 app.security.acl.Principal 同字段口径)。"""

    user_id: str = ""
    role: str = "employee"
    department: str = ""

    @property
    def is_privileged(self) -> bool:
        return self.role in PRIVILEGED_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


_current: ContextVar[Caller | None] = ContextVar("mxi_caller", default=None)


def set_caller(caller: Caller | None) -> Token:
    """绑定当前调用者(asyncio 任务上下文); 返回 token 供 ``finally`` 复位。"""
    return _current.set(caller)


def reset_caller(token: Token) -> None:
    _current.reset(token)


def current_caller() -> Caller | None:
    return _current.get()


def caller_tool_args() -> dict[str, str]:
    """编排层注入给 MCP/进程内工具的参数补丁(取当前上下文的调用者)。"""
    caller = _current.get()
    if caller is None:
        return {CALLER_ARG_USER_ID: "", CALLER_ARG_ROLE: "", CALLER_ARG_DEPT: ""}
    return {
        CALLER_ARG_USER_ID: caller.user_id,
        CALLER_ARG_ROLE: caller.role,
        CALLER_ARG_DEPT: caller.department,
    }


# ---------------------------------------------------------------------------
# 编排层: 把当前调用者包进工具参数
# ---------------------------------------------------------------------------


def bind_caller(tool):
    """包一层: 调用前用当前上下文的调用者覆盖工具里的 ``caller_*`` 参数。

    给**不经 Tool Cache 的工具集**用(专业智能体自己的 MCP 客户端); 编排层的工具走
    :func:`app.cache.tool_cache.wrap_tools_for_cache`, 那一层为了把调用者算进缓存 key
    已经自己合一次了。两边都只从 :func:`caller_tool_args` 取份, 注入字段不会两处漂移。

    参数表里没有 ``caller_user_id`` 的工具(如进程内的 web/docgen 工具)原样返回,
    否则它们的签名会拿到意料之外的关键字参数。
    """
    if CALLER_ARG_USER_ID not in getattr(tool, "args", {}):
        return tool

    from langchain_core.tools import StructuredTool

    async def _coro(**kwargs):
        args = {**kwargs, **caller_tool_args()}
        result = await tool.ainvoke(args)
        if isinstance(result, list):  # MCP 工具可能返回内容块列表, 拼成文本更好读
            result = "".join(
                str(block.get("text", "")) for block in result if isinstance(block, dict)
            ).strip()
        return result

    return StructuredTool.from_function(
        coroutine=_coro,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def bind_caller_tools(tools: list) -> list:
    """批量版 :func:`bind_caller`。"""
    return [bind_caller(tool) for tool in tools]


# ---------------------------------------------------------------------------
# 工具侧: 解析与判定(纯函数, 便于离线单测)
# ---------------------------------------------------------------------------

NO_CALLER = "缺少调用者身份, 已拒绝执行(该工具只能经助手网关以登录态调用)"


def resolve_caller(user_id: str = "", role: str = "", department: str = "") -> Caller | None:
    """把工具收到的注入参数还原成 :class:`Caller`; 空身份返回 None(调用方须拒执行)。"""
    user_id = (user_id or "").strip()
    if not user_id:
        return None
    return Caller(user_id=user_id, role=(role or "employee").strip().lower() or "employee",
                  department=(department or "").strip())


def forbidden(reason: str, **extra: object) -> dict[str, object]:
    """统一的越权返回体: ``error`` 供 LLM 读懂并停止尝试, ``forbidden`` 供上层判状态。"""
    payload: dict[str, object] = {"error": f"越权拒绝: {reason}", "forbidden": True}
    payload.update(extra)
    return payload


def guard_target_user(
    caller: Caller | None,
    target_user_id: str,
    *,
    what: str = "该员工的记录",
) -> tuple[str, dict[str, object] | None]:
    """自助/跨人判定: 返回 ``(实际生效的工号, 拒绝体)``。

    - 目标工号留空 -> 回填调用者本人(而不是"不过滤=看全部", 这是 ``list_*`` 类工具
      原先最容易被绕开的地方);
    - 目标工号是他人且调用者非管理角色 -> 拒绝。
    """
    if caller is None:
        return "", forbidden(NO_CALLER)
    wanted = (target_user_id or "").strip()
    if not wanted or wanted == caller.user_id:
        return caller.user_id, None
    if caller.is_privileged:
        return wanted, None
    return wanted, forbidden(f"只能查询本人的{what}, 无权访问工号 {wanted}", user_id=wanted)


def guard_owner(
    caller: Caller | None,
    owner_id: str,
    *,
    what: str = "该单据",
) -> dict[str, object] | None:
    """已取到记录后的归属复核: 非本人且非管理角色 -> 返回拒绝体, 否则 None。"""
    if caller is None:
        return forbidden(NO_CALLER)
    if caller.is_privileged:
        return None
    if (owner_id or "").strip() and (owner_id or "").strip() == caller.user_id:
        return None
    return forbidden(f"该{what}不属于你(归属人 {owner_id or '未知'}), 无权查看或变更", owner_id=owner_id)
