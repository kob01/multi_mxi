"""Tool Cache: 缓存"MCP 工具调用 -> 文本结果"这一映射。

只缓存只读查询 —— 前缀白名单(见 ``_CACHEABLE_PREFIXES``)之外的工具(如
``create_hr_ticket`` / ``cancel_hr_ticket`` / ``create_reimbursement`` /
``submit_contract_review``)是写操作,
缓存它们等价于把"提交成功"的响应复用给下一次调用, 用户会以为报销单提交了两
次, 实际后端只创建了一条(或相反)。这与 ACL 的 default-deny 思路一致:
无法确认是只读, 就不缓存 —— 代价是确定性只读工具也被前缀名单筛掉(如采购域的
``precheck_purchase_order`` 不以白名单前缀开头, 不缓存), 这是故意取的保守值。

A2A ``agent_delegate`` 是"需要专业系统多步办理的复杂业务"(见 INTENT_PROMPT 对
AGENT_DELEGATE 的定义: "我要报销""帮我开在职证明""申请离职"), 语义上就是写/
办理类任务, 整体不接入 Tool Cache —— 这是对原方案的一处安全修正。

key 同样带角色(权限敏感: 同名工具对不同角色可见/结果可能不同, 如查余额,
员工只能查自己, 管理者可查他人)。TTL 必须最短(``settings.tool_cache_ttl``,
默认 30s)—— 报销进度/年假余额都是实时数据, 缓存久了就是读到脏结果。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable

from langchain_core.tools import BaseTool, StructuredTool

from app.cache.redis_client import CACHE_PREFIX, get_redis, try_redis
from app.config import get_settings

logger = logging.getLogger(__name__)

# 只读工具名前缀白名单: 不在名单内一律不缓存 (见模块 docstring)。
_CACHEABLE_PREFIXES = ("query_", "list_", "get_", "check_", "lookup_", "search_")


def is_cacheable_tool_name(name: str) -> bool:
    """工具名是否命中只读前缀白名单(默认 deny, 不确认是只读就不缓存)。"""
    lowered = name.lower()
    return lowered.startswith(_CACHEABLE_PREFIXES)


def _key(server: str, tool_name: str, args: dict, role: str) -> str:
    # sort_keys 保证同一组参数不同 dict 顺序生成同一 key。
    args_json = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    material = f"{server}|{tool_name}|{args_json}|{role}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:tool:{digest}"


def _ttl_for(server: str) -> int:
    """按域解析 TTL(计划 D4): 业务实时数据(余额/进度)用全局短 TTL;
    联网检索(web)的结果在短窗口内稳定, 用更宽的 ``web_search_cache_ttl``。
    其余能力域/未登记域一律回退全局值 —— 无法确认可缓存时取保守值。"""
    settings = get_settings()
    if server == "web":
        return max(1, settings.web_search_cache_ttl)
    return max(1, settings.tool_cache_ttl)


async def cached_tool_call(
    server: str,
    tool_name: str,
    args: dict,
    role: str,
    invoke: Callable[[], Awaitable[str]],
) -> tuple[str, bool]:
    """查 Tool Cache; 未命中则 ``await invoke()`` 真实调用并回填。

    Returns ``(result_text, cache_hit)``。调用方须自行确保 ``tool_name`` 命中
    只读白名单(见 :func:`is_cacheable_tool_name`), 本函数不做二次判断——
    判定权在 :func:`wrap_tools_for_cache` 那一层。
    """
    settings = get_settings()
    redis = get_redis() if settings.cache_enabled else None
    key = _key(server, tool_name, args, role)

    if redis is not None:
        hit = await try_redis(lambda: redis.get(key), what="tool cache get")
        if hit is not None:
            logger.debug("tool cache hit: %s", key[-12:])
            return hit, True

    result = await invoke()

    if redis is not None and result:
        await try_redis(
            lambda: redis.set(key, result, ex=_ttl_for(server)),
            what="tool cache set",
        )
    return result, False


def wrap_tools_for_cache(tools: list[BaseTool], server: str, role: str) -> list[BaseTool]:
    """把一批 MCP 工具包一层 Tool Cache, 只读白名单命中的才包, 其余原样返回。

    ``AssistantOrchestrator.tool_execute`` 走的是 LangChain ReAct 循环
    (``create_agent(self._llm, tools)``), 工具由 LLM 自主决定何时以何参数调用,
    无法在调用点做缓存, 只能把缓存逻辑下推到工具本身。
    """
    wrapped: list[BaseTool] = []
    for tool in tools:
        if not is_cacheable_tool_name(tool.name):
            wrapped.append(tool)
            continue
        wrapped.append(_wrap_one(tool, server, role))
    return wrapped


def _wrap_one(tool: BaseTool, server: str, role: str) -> BaseTool:
    from app.assistant.mcp_client import flatten_mcp_result

    async def _coro(**kwargs) -> str:
        async def _invoke() -> str:
            return flatten_mcp_result(await tool.ainvoke(kwargs))

        result, _hit = await cached_tool_call(server, tool.name, kwargs, role, _invoke)
        return result

    return StructuredTool.from_function(
        coroutine=_coro,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )
