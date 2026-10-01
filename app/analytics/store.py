"""分析产物(图表/报告)的落盘与寻址。

为什么走"文件 + URL"而不是把 SVG/Markdown 塞进对话文本:
- SVG 塞进 LLM 回答里既占 token 又画不出来, 浏览器要的是可 <img> 的资源;
- 报告需要"下次还能找到"(周报归档), 文件比对话气泡活得久。

安全边界(必须守住):
- 文件名由本模块生成(时间戳 + slug), 不接受调用方传入的相对路径;
- 只允许落在 ``settings.report_dir`` 这一个目录内, 网关侧按名正则校验后回文件;
- 写失败一律返回 error 字典而不是抛出 —— 与全项目"能降级就降级"一致, 一次画图
  失败不该让整轮数据分析没有文字结论。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 内网统一东八区(与 graph.py 的时钟口径一致): 产物文件名里的日期要给中国人看。
_CST = timezone(timedelta(hours=8))

# 允许的文件扩展名 -> 台账 kind。白名单而不是通配: 产物只有这几类。
# (.png 是分析图表给 office 文档内嵌用的位图; SVG 已不再服务于网页工坊。)
_EXT_KIND = {".svg": "chart", ".md": "report", ".csv": "table", ".png": "chart"}

# 扩展名 -> 下发 Content-Type。与 _EXT_KIND 同源维护: 网关那边只按 ".svg" 二分、
# 其余一律回落 text/markdown 的话, 位图与 CSV 就带着错的 MIME 出网 —— <img> 引用
# PNG 在严格 MIME/禁嗅探的浏览器与所有 WebView 下不显示, CSV 下载拿到 .md 类型。
_EXT_MEDIA = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}

# 文件名硬约束: 只允许本模块生成的字符集, 挡掉 ../ 与绝对路径等一切穿越写法。
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def media_type(name: str) -> str | None:
    """按扩展名给出下发用的 Content-Type; 不在映射里返回 None(调用方据此拒下发)。"""
    return _EXT_MEDIA.get(Path(name).suffix.lower())


def reports_dir() -> Path:
    """产物目录(宿主轨 ./data/reports, 容器轨 /data/reports, 同一持久卷)。"""
    directory = Path(get_settings().report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def artifact_url(name: str) -> str:
    """产物的可访问相对路径(由 assistant 网关的 /api/files/reports 提供)。

    只返相对路径: MCP 进程不知道调用方最终从哪个主机名访问(宿主直跑/vite dev/
    容器三种拓扑的绝对地址都不同), 交给前端按自身 origin 解析最稳。
    """
    return f"/api/files/reports/{name}"


def slugify(text: str, max_len: int = 28) -> str:
    """把中文标题压成文件名可用的短 slug。

    中文不进 ASCII 白名单, 但保留在展示标题里; 文件名只取英数与部门等已在文本中
    出现的 ASCII 片段, 全中文时退化为时间戳命名(仍能定位, 只是不好读)。
    """
    normalized = unicodedata.normalize("NFKC", (text or "").strip()).lower()
    kept = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return kept[:max_len].strip("-")


def stamp(prefix: str, ext: str, title: str = "") -> str:
    """生成带北京时间戳与 slug 的文件名: {prefix}-{yyyymmdd-hhmmss}-{slug}{ext}。"""
    now = datetime.now(_CST)
    slug = slugify(title)
    parts = [prefix, now.strftime("%Y%m%d-%H%M%S")]
    if slug:
        parts.append(slug)
    return "-".join(parts) + ext


def is_safe_name(name: str) -> bool:
    """网关侧回文件前的名字校验(正则白名单, 不看扩展名之外的任何东西)。"""
    return bool(name) and _SAFE_NAME_RE.match(name) is not None and ".." not in name


def write_text(name: str, content: str, *, created_by: str = "", title: str = "", params: dict | None = None) -> dict[str, Any]:
    """写一份产物并登记台账; 返回 {name, url, kind, bytes} 或 {error}。

    台账(report_artifacts)写入失败不影响文件可用性: 只记一条 WARNING, 返回值里
    补上 ledger=false —— 对话里仍能给链接, 只是"下次按人回查"这条能力暂缺。
    """
    if not is_safe_name(name):
        return {"error": f"非法产物文件名: {name}"}
    suffix = Path(name).suffix.lower()
    if suffix not in _EXT_KIND:
        return {"error": f"不支持的产物类型: {suffix or '(无扩展名)'}"}
    try:
        path = reports_dir() / name
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("write artifact %s failed: %s", name, exc)
        return {"error": f"产物写入失败: {exc.__class__.__name__}"}
    ledger_ok = _register_ledger(
        name=name,
        kind=_EXT_KIND[suffix],
        title=title or name,
        created_by=created_by,
        params=params,
        size=path.stat().st_size,
    )
    payload: dict[str, Any] = {
        "name": name,
        "url": artifact_url(name),
        "kind": _EXT_KIND[suffix],
        "bytes": path.stat().st_size,
    }
    if not ledger_ok:
        payload["ledger"] = False
    return payload


def write_bytes(
    name: str, data: bytes, *, created_by: str = "", title: str = "", params: dict | None = None
) -> dict[str, Any]:
    """写一份二进制产物(如图表 PNG)并登记台账; 与 :func:`write_text` 同构。

    同一套命名/白名单/穿越校验: 只允许可知扩展名、只落 report_dir; 台账写失败不挡产物。
    """
    if not is_safe_name(name):
        return {"error": f"非法产物文件名: {name}"}
    suffix = Path(name).suffix.lower()
    if suffix not in _EXT_KIND:
        return {"error": f"不支持的产物类型: {suffix or '(无扩展名)'}"}
    try:
        path = reports_dir() / name
        path.write_bytes(data)
    except OSError as exc:
        logger.warning("write artifact %s failed: %s", name, exc)
        return {"error": f"产物写入失败: {exc.__class__.__name__}"}
    ledger_ok = _register_ledger(
        name=name, kind=_EXT_KIND[suffix], title=title or name,
        created_by=created_by, params=params, size=path.stat().st_size,
    )
    payload: dict[str, Any] = {
        "name": name, "url": artifact_url(name), "kind": _EXT_KIND[suffix], "bytes": path.stat().st_size,
    }
    if not ledger_ok:
        payload["ledger"] = False
    return payload


def _register_ledger(
    *, name: str, kind: str, title: str, created_by: str, params: dict | None, size: int
) -> bool:
    """把产物登记进 report_artifacts; 任何 DB 异常都只降级为"没有台账"。"""
    try:
        from sqlalchemy.orm import Session

        from app.db import sync as dbsync
        from app.db.models import ReportArtifact

        with Session(dbsync.get_sync_engine()) as session:
            session.merge(
                ReportArtifact(
                    name=name,
                    kind=kind,
                    title=title[:255],
                    created_by=created_by,
                    params=params or {},
                    bytes_size=size,
                )
            )
            session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 台账是加分项, 不能挡住产物
        logger.warning("artifact ledger register failed for %s: %s", name, exc)
        return False


def recent_artifacts(created_by: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """列出最近生成的产物(可按生成者过滤); DB 不可用返回空列表。"""
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from app.db import sync as dbsync
        from app.db.models import ReportArtifact

        stmt = select(ReportArtifact).order_by(ReportArtifact.created_at.desc()).limit(max(1, limit))
        if created_by:
            stmt = stmt.where(ReportArtifact.created_by == created_by)
        with Session(dbsync.get_sync_engine()) as session:
            rows = session.scalars(stmt).all()
            return [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "title": r.title,
                    "url": artifact_url(r.name),
                    "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else "",
                }
                for r in rows
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("list artifacts failed: %s", exc)
        return []
