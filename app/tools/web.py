"""进程内 web 工具: 联网检索(search_web)与网页抓取(fetch_url)。

形态依据计划(web_search / web_fetch + docgen —— 纯进程内 tool 版): 与
``lookup_employee_by_name`` 同源同构的 LangChain ``@tool``, 由编排层
``tool_execute`` 的"能力域"分支注入, 不经 MCP 连接池与 MCP 权限矩阵
(它们不是业务域 server; 角色维度本期全量开放, 见 app/security/auth.py 的透传语义)。

两条工具都遵守同一个契约: **永不抛未捕获异常**。任何失败都返回 ``{error, ...}``
载荷交给 ReAct 循环自行降级 —— 一次抓取失败不该让整轮对话失败。

缓存: ``search_web`` 命中只读前缀白名单(``search_``), 被 ``wrap_tools_for_cache``
自动包 Redis(TTL 按 server 取 ``web_search_cache_ttl``); ``fetch_url`` 与写操作
同理不缓存(网页正文波动大, 缓存有害无益)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from langchain_core.tools import tool

from app.config import get_settings
from app.security.url_guard import UrlBlocked, resolve_and_validate
from app.tools._http import get_search_client, get_web_client

logger = logging.getLogger(__name__)

# ddgs 的时效档位: 模型传 day/week/... 或直接 d/w/m/y 都接受。
_RECENCY_ALIASES = {
    "day": "d", "d": "d",
    "week": "w", "w": "w",
    "month": "m", "m": "m",
    "year": "y", "y": "y",
}
# fetch 允许的响应类型: 只抽文本类正文, 二进制(图片/pdf/压缩包)没有抓取价值还烧内存。
_ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/json")


def _normalize_recency(recency: str) -> str:
    return _RECENCY_ALIASES.get((recency or "").strip().lower(), "")


def _clip_results(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """把各 provider 的异构结果压成统一的 {title, url, snippet}, 按 url 去重。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        url = str(item.get("url") or item.get("href") or item.get("link") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "title": str(item.get("title") or "").strip()[:200],
                "url": url,
                "snippet": str(item.get("snippet") or item.get("content") or item.get("body") or item.get("description") or "").strip()[:500],
            }
        )
        if len(out) >= limit:
            break
    return out


def _ddgs_text_sync(query: str, max_results: int, timelimit: str) -> list[dict[str, Any]]:
    """ddgs 是同步 HTTP 库, 由调用方 ``asyncio.to_thread`` 卸载。惰性导入: 包缺失/不可达都按降级处理。

    proxy/timeout/backends 均来自配置:
    - 大陆网络下 ddgs 默认引擎直连不可达, 需经 ``web_search_proxy`` 翻出去; 不配代理时 proxy=None 直连。
    - 逐引擎独立尝试(而非把一个逗号串交给 ddgs): ddgs 多引擎模式下任一引擎抛 TimeoutException
      会中断整个检索而不是跳过继续, 走代理时一个慢引擎(brave 偶发>10s)会毒化全局。改成自己循环,
      单引擎异常/空结果就换下一个, 首个出结果即返回; 全部失败才抛出(上层降级)。
    - ``web_search_ddgs_backends`` 控制尝试顺序(默认 duckduckgo,yahoo,brave: 三者全球可达且快)。
    """
    from ddgs import DDGS  # noqa: PLC0415 - 惰性导入, 缺包只影响本 provider

    settings = get_settings()
    backends = [b.strip() for b in (settings.web_search_ddgs_backends or "auto").split(",") if b.strip()] or ["auto"]
    base_kwargs: dict[str, Any] = {"max_results": max_results}
    if timelimit:
        base_kwargs["timelimit"] = timelimit
    ddgs_kwargs: dict[str, Any] = {"timeout": int(settings.web_search_timeout)}
    if settings.web_search_proxy:
        ddgs_kwargs["proxy"] = settings.web_search_proxy

    last_exc: Exception | None = None
    for backend in backends:
        call_kwargs = dict(base_kwargs)
        if backend != "auto":
            call_kwargs["backend"] = backend
        try:
            results = list(DDGS(**ddgs_kwargs).text(query, **call_kwargs))
        except Exception as exc:  # noqa: BLE001 - 单引擎失败(超时/不可达)换下一个, 不中断整轮
            last_exc = exc
            logger.debug("ddgs backend %s failed: %s", backend, str(exc)[:120])
            continue
        if results:
            return results
    if last_exc is not None:
        raise last_exc  # 所有引擎都报错(非空结果): 抛出交给上层 provider 降级
    return []


async def _search_ddgs(query: str, max_results: int, timelimit: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_ddgs_text_sync, query, max_results, timelimit)


async def _search_tavily(query: str, max_results: int, timelimit: str) -> list[dict[str, Any]]:
    """Tavily REST 直连(不加 SDK); 密钥经 secrets 注入, 不落代码。"""
    api_key = get_settings().tavily_api_key
    if not api_key:
        raise RuntimeError("tavily_api_key 未配置")
    payload: dict[str, Any] = {"api_key": api_key, "query": query, "max_results": max_results}
    if timelimit in ("d", "w", "m"):
        # Tavily 用 topic=news + days 表达时效; 按档位换算成天数。
        payload["topic"] = "news"
        payload["days"] = {"d": 1, "w": 7, "m": 30}[timelimit]
    resp = await get_search_client().post(
        "https://api.tavily.com/search", json=payload, timeout=get_settings().web_search_timeout
    )
    resp.raise_for_status()
    return list(resp.json().get("results") or [])


async def _search_serper(query: str, max_results: int, timelimit: str) -> list[dict[str, Any]]:
    """Serper(Google) REST 直连; tbs=qdr:d/w/m/y 表达时效。"""
    api_key = get_settings().serper_api_key
    if not api_key:
        raise RuntimeError("serper_api_key 未配置")
    payload: dict[str, Any] = {"q": query, "num": max_results}
    if timelimit:
        payload["tbs"] = f"qdr:{timelimit}"
    resp = await get_search_client().post(
        "https://google.serper.dev/search",
        json=payload,
        headers={"X-API-KEY": api_key},
        timeout=get_settings().web_search_timeout,
    )
    resp.raise_for_status()
    return list(resp.json().get("organic") or [])


_PROVIDERS = {"ddgs": _search_ddgs, "tavily": _search_tavily, "serper": _search_serper}


@tool
async def search_web(query: str, max_results: int = 0, recency: str = "") -> dict[str, Any]:
    """联网搜索: 按关键词检索公网, 返回带来源 URL 的结果列表。

    适用: 时效性/外部事实类问题(最新进展、新闻、行情、天气、外部产品资料),
    以及知识库里查不到的外部信息。知识库制度/流程类问题不要用本工具。

    Args:
        query: 检索关键词。建议提炼 3~8 个核心词, 不要整句照抄用户原话。
        max_results: 返回条数(1~10); 0 表示用系统默认(5)。
        recency: 时效过滤, 可选 "day"/"week"/"month"/"year" 或留空。

    Returns:
        {query, provider, results: [{title, url, snippet}], degraded: false};
        全部 provider 失败时 {query, provider, results: [], degraded: true, error},
        此时请基于已有知识回答并说明"未能联网核实", 不要编造来源。
    """
    settings = get_settings()
    query = (query or "").strip()
    limit = max(1, min(int(max_results or 0) or settings.web_search_max_results, 10))
    timelimit = _normalize_recency(recency)
    if not query:
        return {"query": "", "provider": "", "results": [], "degraded": True, "error": "检索词不能为空"}

    ordered = [settings.web_search_provider, "ddgs"]
    tried: list[str] = []
    last_error = ""
    for name in dict.fromkeys(ordered):  # 去重且保序: 配置的 provider 优先, ddgs 兜底
        fn = _PROVIDERS.get(name)
        if fn is None:
            continue
        tried.append(name)
        try:
            raw = await fn(query, limit, timelimit)
            results = _clip_results(raw, limit)
            if not results:
                # provider 正常应答但零结果: 记为失败转下一个, 免得把"没搜到"当"搜索坏了"。
                last_error = f"{name} 无结果"
                continue
            return {"query": query, "provider": name, "results": results, "degraded": False}
        except Exception as exc:  # noqa: BLE001 - 单个 provider 失败必须降级而不是抛
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.warning("web search provider %s failed: %s", name, last_error)
    return {
        "query": query,
        "provider": ",".join(tried),
        "results": [],
        "degraded": True,
        "error": f"联网检索不可用({last_error})",
    }


def _extract_text(html: str) -> tuple[str, str]:
    """HTML -> (title, 正文纯文本); bs4 惰性导入, 缺包时退化为去标签正则。"""
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415 - 惰性导入
    except ImportError:
        import re

        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = (title_m.group(1).strip() if title_m else "")[:200]
        return title, text

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    # \n 分隔让块级结构保留分段信息, 后续压掉连续空行。
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    return (title or "")[:200], "\n".join(line for line in lines if line)


def _collapse_blank(text: str) -> str:
    out: list[str] = []
    blank = 0
    for line in text.splitlines():
        if line.strip():
            out.append(line)
            blank = 0
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


@tool
async def fetch_url(url: str, max_chars: int = 0) -> dict[str, Any]:
    """抓取一个公网网页并抽取正文纯文本。

    适用: search_web 的结果里某条来源需要展开看正文时。每次抓取都经过 SSRF
    护栏(仅公网 http/https, 拒绝内网地址与云元数据地址), 重定向逐跳校验。

    Args:
        url: 要抓取的完整 http/https 链接(优先取 search_web 结果里的 url 原文)。
        max_chars: 正文字符上限(1~20000); 0 表示用系统默认。

    Returns:
        成功: {url, final_url, title, text, truncated, bytes}。
        失败: {error, url} —— 常见原因是链接不可达、非文本类型或被安全护栏拒绝,
        请如实地告知用户, 不要猜测页面内容。
    """
    settings = get_settings()
    url = (url or "").strip()
    if not url:
        return {"error": "url 不能为空", "url": ""}
    limit = max(1, min(int(max_chars or 0) or settings.web_fetch_max_chars, 20000))
    client = get_web_client()
    current = url
    final_url = ""
    try:
        for _hop in range(settings.web_fetch_max_redirects + 1):
            await resolve_and_validate(current)  # 每一跳都重新过 SSRF 护栏(含重定向目标)
            resp = await client.get(current)
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if not location:
                    return {"error": f"重定向缺少目标地址(HTTP {resp.status_code})", "url": url}
                current = str(resp.next_request.url) if resp.next_request else location
                continue
            resp.raise_for_status()
            final_url = str(resp.url)
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in _ALLOWED_CONTENT_TYPES:
                return {"error": f"不支持的内容类型: {ctype}(仅文本类网页)", "url": url, "final_url": final_url}
            # 流式限量: 超过 web_fetch_max_bytes 立即断, 不给恶意大文件烧内存的机会。
            chunks: list[bytes] = []
            received = 0
            truncated = False
            async for piece in resp.aiter_bytes():
                chunks.append(piece)
                received += len(piece)
                if received >= settings.web_fetch_max_bytes:
                    truncated = True
                    break
            raw = b"".join(chunks)
            charset = resp.charset_encoding or "utf-8"
            html = raw.decode(charset, errors="replace")
            title, text = _extract_text(html)
            text = _collapse_blank(text)
            if len(text) > limit:
                text = text[:limit].rstrip() + "…"
                truncated = True
            # 落地后对 final URL 再复核一次(压缩 DNS-rebinding 的时间窗)。
            await resolve_and_validate(final_url or current)
            return {
                "url": url,
                "final_url": final_url,
                "title": title,
                "text": text,
                "truncated": truncated,
                "bytes": received,
            }
        return {"error": f"重定向次数超过上限({settings.web_fetch_max_redirects})", "url": url}
    except UrlBlocked as exc:
        # 护栏拒绝是"安全事件"而非普通失败: 如实回给模型, 文案不暴露内网拓扑细节。
        logger.warning("fetch_url blocked: %s (%s)", exc.reason, url)
        return {"error": f"该链接被安全护栏拒绝: {exc.reason}", "url": url}
    except httpx.HTTPStatusError as exc:
        return {"error": f"目标返回 HTTP {exc.response.status_code}", "url": url, "final_url": str(exc.request.url)}
    except httpx.HTTPError as exc:
        return {"error": f"链接不可达({type(exc).__name__})", "url": url}
    except Exception as exc:  # noqa: BLE001 - 任何意外都不能炸掉 ReAct 循环
        logger.warning("fetch_url unexpected failure for %s: %s", url, exc)
        return {"error": f"抓取失败({type(exc).__name__})", "url": url}
