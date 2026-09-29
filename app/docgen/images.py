"""文档内嵌图片的统一解析: 本地文件 / 图片 URL -> 归一化 PNG 落到生成目录。

为什么单独一层、而且是 async:
- builder(docx/pptx/pdf/xlsx) 是纯同步且跑在 ``asyncio.to_thread`` 里, 不该在里面做
  网络请求与 SSRF 校验(async 护栏 + 共享 httpx 客户端); 所以图片在调用 builder 之前
  就在这层解析成**本地 PNG 文件**, builder 只管按路径嵌入。
- 安全边界与 web 抓取完全同源: URL 图片同样过 ``url_guard`` (拒内网/元数据/compose 服务名),
  逐跳跟随重定向并每跳复核; 本地图片只允许落在 report_dir / upload_dir 两个目录内,
  挡掉 ``../../etc`` 这类穿越, 也防止把任意本地文件塞进对外下载的文档里。
- 统一转 PNG(RGB) 的另一个理由: docx/pptx 对 jpg/png 都吃, reportlab 对带 alpha 的 png
  会花, 归一到白底 RGB PNG 后四种格式表现一致; 顺带按最长边降采样, 免得一张 4K 图把
  生成物撑爆。

单张图片解析失败(不可达/非图片/超大/损坏)只丢弃这一张并记一条 warning, 不影响整篇文档
生成 —— 与全项目"能降级就降级"的口径一致。
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 允许嵌入的原始图片扩展名(本地文件按这些取; SVG 不在此列 —— PIL 无法栅格化 SVG,
# 分析图表由 charts 额外产出的 PNG 提供)。
_ALLOWED_LOCAL_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
# URL 图片响应的 content-type 前缀白名单(只收 image/*)。
_IMAGE_CT_PREFIX = "image/"


def _allowed_local_roots() -> list[Path]:
    """可引用的本地图片根目录: 分析产物目录 + 文档上传目录(同一持久卷)。"""
    from app.analytics.store import reports_dir
    from app.docs.service import _upload_dir

    roots: list[Path] = []
    for getter in (reports_dir, _upload_dir):
        try:
            roots.append(Path(getter()).resolve())
        except Exception as exc:  # noqa: BLE001 - 任一目录不可用不影响另一处
            logger.debug("image root unavailable (%s): %s", getter, exc)
    return roots


def _local_path_to_file(src: str) -> Path | None:
    """把本地图片引用收敛到一个真实文件; 不在允许目录内或不存在一律 None。

    同时接受绝对路径与裸文件名: 裸文件名依次在 report_dir / upload_dir 下找(模型常只给
    ``chart-2026....png`` 这种产物名, 而不是全路径)。
    """
    raw = (src or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    roots = _allowed_local_roots()
    if not candidate.is_absolute():
        # 裸相对路径(含文件名): 逐个允许目录试。
        for root in roots:
            hit = (root / candidate).resolve()
            if hit.is_file() and any(_within(hit, r) for r in roots):
                return hit
        return None
    resolved = candidate.resolve()
    if resolved.is_file() and any(_within(resolved, r) for r in roots):
        return resolved
    return None


def _within(path: Path, root: Path) -> bool:
    """path 是否位于 root 之下(resolve 后按父级判定; root 自身不算)。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_to_png(raw: bytes, dest: Path) -> tuple[bool, str]:
    """PIL 解码 -> RGB(白底) -> 最长边降采样 -> 存 PNG。返回 (ok, note)。"""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - 非图片/损坏
        return False, f"无法解码图片({type(exc).__name__})"

    max_edge = max(1, int(get_settings().docgen_image_max_edge))
    if img.mode in ("RGBA", "LA", "P"):
        # 带透明通道: 贴白底展平, 避免 PDF 里透明区渲染成黑块。
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if max(img.size) > max_edge:
        scale = max_edge / max(img.size)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

    try:
        img.save(dest, format="PNG")
    except Exception as exc:  # noqa: BLE001
        return False, f"图片写盘失败({type(exc).__name__})"
    return True, ""


async def _fetch_url_bytes(url: str) -> tuple[bytes | None, str]:
    """按 SSRF 护栏抓取一张图: 逐跳校验 + content-type/体积限制。失败回 (None, note)。

    体积限制必须在流式读取里做: 先 ``client.get()`` 会把整张图(任意大小)先读进
    内存再看字节数, 一个百 MB 的"图片"就是一个内存尖峰 —— 多人同时生成就集火。
    """
    import httpx

    from app.security.url_guard import UrlBlocked, resolve_and_validate
    from app.tools._http import get_web_client

    settings = get_settings()
    limit = int(settings.docgen_image_max_bytes)
    client = get_web_client()
    current = url
    try:
        for _hop in range(settings.web_fetch_max_redirects + 1):
            await resolve_and_validate(current)
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        return None, f"重定向缺少目标地址(HTTP {resp.status_code})"
                    current = str(resp.next_request.url) if resp.next_request else location
                    continue
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype and not ctype.startswith(_IMAGE_CT_PREFIX):
                    return None, f"链接不是图片(content-type={ctype})"
                body: bytearray = bytearray()
                async for piece in resp.aiter_bytes():
                    body.extend(piece)
                    # 越界即断流: 不给大文件把内存顶到的机会(而不是读完了再判)。
                    if len(body) > limit:
                        return None, f"图片超过上限 {limit // 1024}KB"
            await resolve_and_validate(str(resp.url) or current)  # 落地后对 final URL 复核
            return bytes(body), ""
        return None, f"重定向次数超过上限({settings.web_fetch_max_redirects})"
    except UrlBlocked as exc:
        return None, f"图片链接被安全护栏拒绝: {exc.reason}"
    except httpx.HTTPStatusError as exc:
        return None, f"图片目标返回 HTTP {exc.response.status_code}"
    except httpx.HTTPError as exc:
        return None, f"图片链接不可达({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001
        return None, f"图片抓取失败({type(exc).__name__})"


def _entry_fields(item: Any) -> tuple[str, str, float | None]:
    """从一张图片项取 (src, caption, width_inch); 兼容直接给字符串路径的简写。"""
    if isinstance(item, str):
        return item, "", None
    if isinstance(item, dict):
        src = str(item.get("src") or item.get("url") or item.get("path") or "").strip()
        caption = str(item.get("caption") or item.get("title") or "")[:200]
        width_raw = item.get("width")
        try:
            width = float(width_raw) if width_raw not in (None, "") else None
        except (TypeError, ValueError):
            width = None
        return src, caption, width
    return "", "", None


async def resolve_images(spec_images: Any, dest_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """把 spec 里的图片项解析成本地 PNG 列表, 供 builder 直接嵌入。

    Args:
        spec_images: ``spec["images"]``, 每项 ``{"src": 本地路径或URL, "caption"?, "width"?(英寸)}``,
            也接受裸字符串路径的简写。
        dest_dir: 生成物所在目录(= token 目录); 归一化 PNG 落在其内, 随生成物一起被到期清扫。

    Returns:
        ``(resolved, warnings)``; resolved 每项 ``{"path","caption","width"}``(path 为绝对文件)。
    """
    items = spec_images if isinstance(spec_images, list) else ([spec_images] if spec_images else [])
    settings = get_settings()
    limit = int(settings.docgen_image_max_bytes)
    resolved: list[dict[str, Any]] = []
    warnings: list[str] = []

    for idx, item in enumerate(items[:20]):  # 硬限 20 张: 再多是把体积和耐心推向失控
        src, caption, width = _entry_fields(item)
        if not src:
            warnings.append(f"第 {idx + 1} 张图片缺少 src, 已跳过")
            continue

        raw: bytes | None = None
        local: Path | None = None
        if src.lower().startswith(("http://", "https://")):
            raw, note = await _fetch_url_bytes(src)
            if raw is None:
                warnings.append(f"图片 {src[:80]} 未能获取: {note}")
                continue
            if len(raw) > limit:
                warnings.append(f"图片 {src[:80]} 超过上限, 已跳过")
                continue
        else:
            local = _local_path_to_file(src)
            if local is None:
                warnings.append(f"本地图片不可访问或不在允许目录内: {src[:80]}")
                continue
            if local.suffix.lower() not in _ALLOWED_LOCAL_SUFFIXES:
                warnings.append(f"不支持的图片格式 {local.suffix}: {src[:80]}(SVG 请用同名 PNG)")
                continue
            if local.stat().st_size > limit:
                warnings.append(f"本地图片超过上限: {src[:80]}")
                continue
            try:
                raw = await asyncio.to_thread(local.read_bytes)
            except OSError as exc:
                warnings.append(f"本地图片读取失败: {exc.__class__.__name__}")
                continue

        out = dest_dir / f"img_{idx + 1}.png"
        # PIL 解码/降采样/写 PNG 是纯 CPU(一张大图可到百毫秒级), 必须卸载出事件
        # 循环: 否则共进程所有人的流式回复都在这张图上被卡住。
        ok, note = await asyncio.to_thread(_normalize_to_png, raw, out)
        if not ok:
            warnings.append(f"图片归一化失败({src[:60]}): {note}")
            continue
        resolved.append({"path": str(out), "caption": caption, "width": width})

    return resolved, warnings
