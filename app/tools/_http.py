"""web 工具(app/tools/web.py)与抓取需求的进程级共享 httpx 连接池。

为什么复用单例: 抓取/检索在对话热路径上被 ReAct 反复触发, 每次新建客户端会堆
TIME_WAIT, 并发轮次下把延迟放大一个数量级(与 app/rag/reranker.py 的同一条结论)。
这里统一 ``follow_redirects=False``: 重定向由 fetch_url 手动逐跳跟随, 每一跳都
重新过 SSRF 护栏 —— httpx 的自动重定向会跳过中间跳的校验, 那是绕过护栏的后门。
"""

from __future__ import annotations

import httpx

from app.config import get_settings

_client: httpx.AsyncClient | None = None
_search_client: httpx.AsyncClient | None = None


def get_web_client() -> httpx.AsyncClient:
    """Lazily build the process-wide client (bounded pool + fetch-level default timeout)."""
    global _client
    if _client is None or _client.is_closed:
        s = get_settings()
        _client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(s.web_fetch_timeout, connect=5.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            # 不跟随环境代理变量: 内网部署下 http_proxy 常指向办公代理, 会让 SSRF
            # 校验(直连视角)与真实出口(代理视角)不一致; 显式禁用保持行为可预测。
            trust_env=False,
            headers={"User-Agent": "mxi-web-tool/1.0 (+internal assistant)"},
        )
    return _client


def get_search_client() -> httpx.AsyncClient:
    """检索 API(tavily/serper)专用客户端: 按 ``web_search_proxy`` 走代理。

    与 fetch 的直连客户端分开, 因为代理只该影响检索出口(翻到 Google/境外 API),
    不能污染 fetch_url 的 SSRF 直连校验视角。未配代理时与 get_web_client 行为一致
    (proxy=None 即直连)。ddgs 是同步库, 不走本客户端, 由 web.py 直接传 proxy。
    """
    global _search_client
    if _search_client is None or _search_client.is_closed:
        s = get_settings()
        _search_client = httpx.AsyncClient(
            follow_redirects=True,
            proxy=s.web_search_proxy or None,
            timeout=httpx.Timeout(s.web_search_timeout, connect=5.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            trust_env=False,
            headers={"User-Agent": "mxi-web-tool/1.0 (+internal assistant)"},
        )
    return _search_client


async def close_web_client() -> None:
    """关停时释放连接池(挂到 main.py lifespan, 与 close_reranker_client 同风格)。"""
    global _client, _search_client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
    if _search_client is not None and not _search_client.is_closed:
        await _search_client.aclose()
    _search_client = None
