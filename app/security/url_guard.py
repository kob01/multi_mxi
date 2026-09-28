"""SSRF 护栏的单一实现(只此一份, fetch/search 之外的抓取需求也必须走这里)。

为什么放在 security 而不是 tools: 这是一条与业务无关的安全边界 —— 本项目的网关进程
"身处内网且具备外联能力"(能连 DeepSeek/CDN, 也持有 PG/Redis/Mongo/Neo4j 的可达路由),
让模型随口给一个 URL 就去抓, 等于把内网拓扑探测(``http://postgres:5432``)与云元数据
窃取(``http://169.254.169.254/``)交给了 LLM 的输出。护栏做三件事:

1. 字面名拒绝: localhost / host.docker.internal / compose 服务名直接拒(连 DNS 都不查);
2. 白名单(可选): ``WEB_FETCH_ALLOWLIST`` 非空时 default-deny, 白名单外一律拒 —— 这是
   内网环境收紧的最直接手段;
3. 解析即校验: 对每次 DNS 解析结果逐条 ``ipaddress.is_global`` 判定, 私网/回环/link-local/
   组播/保留段(含 169.254.169.254)全部拒绝; 解析失败(含外网不可达)同样拒绝。

TOCTOU/DNS-rebinding 的残余风险: 校验与真正建连之间存在时间窗, 严格做法是以已校验 IP
直连(https 需要 SNI/证书配合, 实现复杂度高)。当前采用计划文档认可的退一步方案:
抓取侧**手动逐跳跟随重定向且每跳重新过本护栏**, 响应落地后再对 final URL 复核一次 ——
把窗口压缩到"单跳内", 攻击者需要控制权威 DNS 才能利用。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.config import get_settings


class UrlBlocked(ValueError):
    """URL 未通过 SSRF 校验; message 面向调用方(LLM/用户), 不含内网细节。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# 字面名拒绝清单: 不查 DNS 直接拒。除 localhost 系与 docker 专用名外, 把 compose 全部
# 服务名列进来 —— 这些名字在 compose 网络内可解析, 是内网探测最方便的入口。
_LITERAL_DENY = {
    "localhost",
    "host.docker.internal",
    "gateway.docker.internal",
    # compose 服务名(见 docker/docker-compose.yml): 数据库/缓存/存储/应用全在这
    "postgres", "redis", "neo4j", "mongo", "elasticsearch", "tei-rerank", "mineru",
    "assistant", "gateway",
    "hr-mcp", "finance-mcp", "analytics-mcp", "procurement-mcp",
    "hr-agent", "finance-agent", "analyst-agent", "contract-agent",
}


def _host_allowed_by_allowlist(host: str, allowlist: list[str]) -> bool:
    """白名单匹配: 精确命中或以其结尾的子域(example.com 放行 api.example.com)。"""
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.lower().strip().lstrip(".")
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


def _parse_allowlist() -> list[str]:
    raw = get_settings().web_fetch_allowlist or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _is_denied_literal(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in _LITERAL_DENY:
        return True
    # *.localhost / *.internal 等保留后缀一并拒(docker 的服务别名常这么写)。
    return host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local")


def _all_global(ips: list[str]) -> bool:
    """逐条判定解析结果; 任何一个非 global(私网/回环/link-local/保留)都不可信。"""
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if not addr.is_global:
            return False
    return bool(ips)


def _resolve(host: str) -> list[str]:
    """同步 DNS 解析(调用方负责放到线程池): A/AAAA 全收, 失败按"不可信"处理。"""
    infos = socket.getaddrinfo(host, None)
    ips: list[str] = []
    for family, _type, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = sockaddr[0]
        if ip not in ips:
            ips.append(ip)
    return ips


async def resolve_and_validate(url: str) -> list[str]:
    """校验一个 URL 并返回其解析出的(已确认全为 global 的)IP 列表。

    抛 :class:`UrlBlocked` 表示拒绝; 抓取侧应把它转成 ``{error}`` 载荷回给模型,
    而不是让异常炸掉 ReAct 循环。
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise UrlBlocked("仅支持 http/https 链接")
    host = (parts.hostname or "").lower()
    if not host:
        raise UrlBlocked("链接缺少主机名")
    # userinfo(admin:pw@host) 一律拒: 抓取不该携带任何凭据, 也避免借 URL 混淆绕过 host 判断。
    if "@" in parts.netloc:
        raise UrlBlocked("不支持携带用户信息的链接")

    allowlist = _parse_allowlist()
    if allowlist and not _host_allowed_by_allowlist(host, allowlist):
        raise UrlBlocked("该域名不在允许抓取的白名单内")
    if _is_denied_literal(host):
        raise UrlBlocked("不允许访问内部地址")

    try:
        ips = await asyncio.to_thread(_resolve, host)
    except UrlBlocked:
        raise
    except Exception as exc:  # noqa: BLE001 - DNS 失败/超时都按"不可信"处理
        raise UrlBlocked("域名解析失败, 已拒绝访问") from exc
    if not _all_global(ips):
        raise UrlBlocked("目标地址解析到非公网网段, 已拒绝访问")
    return ips
