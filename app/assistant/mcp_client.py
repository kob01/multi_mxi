"""MCP client: discovers tools from all configured MCP servers.

Uses langchain-mcp-adapters' MultiServerMCPClient over streamable-http.
Tools are cached per server after first discovery; call refresh() to re-discover.

并发下的两个要点(都是"每轮 tool_call 都会踩到"的路径):
- 缓存必须按 server 建起来并加锁: 旧写法 ``get_tools(server)`` 每次都调
  ``MultiServerMCPClient.get_tools(server_name=...)``, 也就是每次工具调用都重开一条
  streamable-http 会话去 discover 一遍工具清单 —— 百人并发时 MCP server 被
  discover 打满, 而真正的业务工具反而排在其后。
- 首建风暴用单飞锁收敛: 同一 server 的 N 个并发请求只跑一次 discover。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import get_settings

logger = logging.getLogger(__name__)


class MCPClientPool:
    """Pool of MCP tool connections keyed by server name."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = MultiServerMCPClient(
            {
                "hr": {"url": settings.hr_mcp_url, "transport": "streamable_http"},
                "finance": {"url": settings.finance_mcp_url, "transport": "streamable_http"},
                "analytics": {"url": settings.analytics_mcp_url, "transport": "streamable_http"},
                "procurement": {"url": settings.procurement_mcp_url, "transport": "streamable_http"},
            }
        )
        # server -> (发现时间, 工具列表); None 键 = 全量清单
        self._cache: dict[str | None, tuple[float, list[BaseTool]]] = {}
        self._locks: dict[str | None, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, server: str | None) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(server)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[server] = lock
            return lock

    def _ttl(self) -> float:
        return max(1.0, float(get_settings().mcp_tools_ttl))

    def _cached(self, server: str | None) -> list[BaseTool] | None:
        entry = self._cache.get(server)
        if entry is None:
            return None
        discovered_at, tools = entry
        if time.monotonic() - discovered_at > self._ttl():
            return None
        return tools

    def cache_stamp(self, server: str | None) -> float:
        """当前工具清单的发现时刻(没有有效缓存时回 0.0)。

        给编排层当"ReAct agent 缓存版本"用: agent 是在具体一批工具对象上编译出来的,
        TTL 到期重新发现后工具换了批次, 旧 agent 就再也不能用(否则新增的工具永远看不
        到), 拿这个戳做键就能"清单一变就重建"而不必自己再维护一份失效逻辑。
        """
        entry = self._cache.get(server)
        if entry is None:
            return 0.0
        discovered_at = entry[0]
        if time.monotonic() - discovered_at > self._ttl():
            return 0.0
        return discovered_at

    async def get_tools(self, server: str | None = None) -> list[BaseTool]:
        """Return LangChain tools, optionally filtered by server name (cached)."""
        cached = self._cached(server)
        if cached is not None:
            return list(cached)
        lock = await self._lock_for(server)
        async with lock:
            # 双检: 等锁期间另一个请求可能已经把这一批工具发现了
            cached = self._cached(server)
            if cached is not None:
                return list(cached)
            try:
                tools = await (
                    self._client.get_tools()
                    if server is None
                    else self._client.get_tools(server_name=server)
                )
            except Exception as exc:  # noqa: BLE001 - MCP server 抖动时退回旧清单
                stale = self._cache.get(server)
                if stale is not None:
                    logger.warning(
                        "MCP 工具发现失败(server=%s), 沿用上次清单继续服务: %s", server, exc
                    )
                    return list(stale[1])
                raise
            self._cache[server] = (time.monotonic(), list(tools))
            return list(tools)

    async def refresh(self, server: str | None = None) -> None:
        """Drop cached tools so next get_tools() re-discovers."""
        if server is None:
            self._cache.clear()
        else:
            self._cache.pop(server, None)
            self._cache.pop(None, None)  # 全量清单也含这一域, 一并作废


_pool: MCPClientPool | None = None


def get_mcp_pool() -> MCPClientPool:
    """Process-wide singleton MCP client pool."""
    global _pool
    if _pool is None:
        _pool = MCPClientPool()
    return _pool


async def call_mcp_tool(server: str, tool_name: str, args: dict[str, Any]) -> Any:
    """Directly invoke a single MCP tool (used by the tool_call route)."""
    pool = get_mcp_pool()
    tools = await pool.get_tools(server)
    for tool in tools:
        if tool.name == tool_name:
            return await tool.ainvoke(args)
    raise LookupError(f"MCP tool {server}.{tool_name} not found")


async def call_mcp_tool_text(server: str, tool_name: str, args: dict[str, Any]) -> str:
    """Invoke one MCP tool and flatten its result into plain text.

    ``ainvoke`` returns either a string or a list of content blocks depending
    on the tool's return type; callers only need the text payload.
    """
    pool = get_mcp_pool()
    tools = await pool.get_tools(server)
    for tool in tools:
        if tool.name == tool_name:
            return _flatten(await tool.ainvoke(args))
    raise LookupError(f"MCP tool {server}.{tool_name} not found")


def _flatten(result: Any) -> str:
    """Flatten an MCP tool result (plain string / content blocks) into text."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "".join(
            str(block.get("text", "")) for block in result if isinstance(block, dict)
        ).strip()
    return str(result)


# 供 app.cache.tool_cache 复用(包装 MCP 工具时要把结果压成纯文本才能写进 Redis)。
flatten_mcp_result = _flatten
