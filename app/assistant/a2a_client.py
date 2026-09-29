"""A2A client used by the Assistant to delegate tasks to specialist agents.

Implements the Agent2Agent protocol client flow:
1. Discover the agent card from /.well-known/agent-card.json
2. Send a JSON-RPC 2.0 `message/send` request
3. Unwrap the returned Task/Message into plain text
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TextPart,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

AGENT_URLS = {
    "finance": lambda: get_settings().finance_agent_url,
    "hr": lambda: get_settings().hr_agent_url,
    # domain 键与 tool 侧保持同名(analytics/procurement), 委派时拼成
    # analytics_agent / procurement_agent, 与 AGENT_WHITELIST 中的名称一致。
    "analytics": lambda: get_settings().analyst_agent_url,
    "procurement": lambda: get_settings().contract_agent_url,
}


def _pin_card_url(card, base_url: str, domain: str):
    """把卡片里的端点地址强制换成配置地址。

    Agent Card 的 ``url`` 是智能体自己通告的地址, 在 compose 里写的是容器内服务名
    (如 ``http://hr-agent:9001``); 而 a2a-sdk 的 JSON-RPC transport 正是拿 ``card.url``
    发 ``message/send``。宿主直跑网关 + 容器跑智能体时, 宿主解析不到该服务名, 委派
    会在卡片发现成功后的下一步静默失败。以配置端点(调用方真正要连的地址)为准,
    两种拓扑都成立, 同时避免被外部构造的卡片把请求引到非预期主机。
    """
    pinned = base_url.rstrip("/") + "/"
    if card.url != pinned:
        logger.info("A2A %s: 卡片通告地址 %s -> 按配置覆盖为 %s", domain, card.url, pinned)
        card = card.model_copy(update={"url": pinned})
    return card


class A2AClientPool:
    """Lazily-resolved A2A clients keyed by agent domain.

    并发下三件事(缺一不可):
    - 共享一个带上限的 ``httpx.AsyncClient``: 委派是多步办理, 一次可能跑几十秒,
      不卡上限就是"默认 100 条连接 + 无 keepalive 上限"直接把智能体压垮。
    - 卡片发现加单飞锁: 旧写法没有锁, 同时到来的 N 个委派会做 N 次 agent-card 发现
      (每个都一次 HTTP + 一整套卡片解析), 而且同一域名可能被写入两次不同的 client。
    - 有明确的关停入点: 不关就是常驻连接池在进程退出时留一堆未释放资源。
    """

    def __init__(self) -> None:
        self._clients: dict[str, A2AClient] = {}
        self._http: httpx.AsyncClient | None = None
        self._lock: asyncio.Lock | None = None

    def _lock_obj(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _http_client(self) -> httpx.AsyncClient:
        """惰性建共享客户端(上限/超时来自 settings)。"""
        if self._http is None or self._http.is_closed:
            s = get_settings()
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(s.a2a_timeout, connect=s.a2a_connect_timeout),
                limits=httpx.Limits(
                    max_connections=max(1, s.a2a_max_connections),
                    max_keepalive_connections=max(1, s.a2a_max_keepalive),
                ),
            )
        return self._http

    async def _get_client(self, domain: str) -> A2AClient:
        """Resolve agent card and build an A2AClient for a domain (single-flight)."""
        if domain in self._clients:
            return self._clients[domain]
        base_url = AGENT_URLS[domain]()
        async with self._lock_obj():
            # 双检: 等锁期间另一个委派可能已经把这一域名发现好了
            if domain in self._clients:
                return self._clients[domain]
            http = self._http_client()
            card = await A2ACardResolver(httpx_client=http, base_url=base_url).get_agent_card()
            card = _pin_card_url(card, base_url, domain)
            client = A2AClient(httpx_client=http, agent_card=card)
            self._clients[domain] = client
            return client

    async def aclose(self) -> None:
        """释放共享连接池(由 lifespan 关停路径调用), 并丢弃已缓存的 client。"""
        self._clients.clear()
        if self._http is not None and not self._http.is_closed:
            try:
                await self._http.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("closing a2a client failed: %s", exc)
        self._http = None

    @staticmethod
    def _extract_text(result: Any) -> str:
        """Pull concatenated text parts out of a Task or Message result."""
        parts = []
        if isinstance(result, Task):
            # Prefer the agent's final status message, then artifacts.
            if result.status.message and result.status.message.parts:
                parts = [p.root.text for p in result.status.message.parts if isinstance(p.root, TextPart)]
            if not parts and result.artifacts:
                for artifact in result.artifacts:
                    parts.extend(p.root.text for p in artifact.parts if isinstance(p.root, TextPart))
        elif isinstance(result, Message):
            parts = [p.root.text for p in result.parts if isinstance(p.root, TextPart)]
        return "\n".join(parts) or "(专业智能体未返回文本)"

    async def send(
        self,
        domain: str,
        text: str,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Delegate a task to a specialist agent and return its reply text.

        Args:
            domain: 'finance' or 'hr'.
            text: Task description in natural language.
            context_id: Optional A2A context id to continue a remote task.
            metadata: Optional structured message metadata (protocol-native),
                e.g. trusted identity {"user_id": ..., "role": ...}.

        Returns:
            The agent's textual answer.
        """
        client = await self._get_client(domain)
        message = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
            messageId=uuid.uuid4().hex,
            contextId=context_id,
            metadata=metadata,
        )
        request = SendMessageRequest(id=uuid.uuid4().hex, params=MessageSendParams(message=message))
        response = await client.send_message(request)
        root = response.root  # JSONRPCErrorResponse | SendMessageSuccessResponse
        error = getattr(root, "error", None)
        if error is not None:
            return f"A2A 调用失败: {getattr(error, 'message', error)}"
        return self._extract_text(getattr(root, "result", None))

    async def send_guarded(
        self,
        domain: str,
        text: str,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> str:
        """带墙钟上限的委派: 超时不炸掉整轮对话, 而是回一段可读的降级文本。

        智能体卡在一步内部工具上(它的 ReAct 循环没有全局预算)时, 没有这一层就是一条
        永远不返回的委派 —— 占住一个并发闸门与一个 HTTP 连接直到进程重启。
        """
        limit = timeout if timeout is not None else get_settings().a2a_timeout
        try:
            return await asyncio.wait_for(
                self.send(domain, text, context_id=context_id, metadata=metadata),
                timeout=limit,
            )
        except TimeoutError:
            logger.warning("A2A 委派超时(%ss): domain=%s", limit, domain)
            return f"专业智能体 {domain} 在 {limit:.0f}s 内未返回结果, 请稍后重试。"
        except httpx.HTTPError as exc:
            logger.warning("A2A 委派网络异常(domain=%s): %s", domain, str(exc)[:160])
            return f"专业智能体 {domain} 暂时不可达({type(exc).__name__}), 请稍后重试。"


_a2a_pool: A2AClientPool | None = None


def get_a2a_pool() -> A2AClientPool:
    """Process-wide singleton A2A client pool."""
    global _a2a_pool
    if _a2a_pool is None:
        _a2a_pool = A2AClientPool()
    return _a2a_pool


async def close_a2a_pool() -> None:
    """关停共享连接池(挂到 main.py lifespan, 与 close_web_client 同风格)。"""
    global _a2a_pool
    if _a2a_pool is not None:
        await _a2a_pool.aclose()
        _a2a_pool = None
