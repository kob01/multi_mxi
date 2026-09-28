"""进程内文档生成工具: 把结构化 spec 落成可下载的 Word/Excel/PPT/PDF(计划 E 组)。

形态: LangChain ``@tool``, 由编排层 ``tool_execute`` 的"能力域"分支注入; 全部 async,
同步 CPU/IO(builder 落盘)统一 ``asyncio.to_thread`` 卸载 —— 这是纯 tool 形态唯一实质
代价, 计划摘要已接受, 体量过大再迁 worker/MCP。

命名约定即缓存策略(与 Tool Cache 的只读前缀白名单对齐):
- ``generate_*`` 不命中 ``search_/query_/get_...`` 白名单 → 天然不缓存。生成是写操作,
  缓存它等于把"上一次生成的下载链接"复用给下一次本应新建的文件。

交付: 返回 ``{file_name, doc_token, download_url, size_bytes, mime}``;
``download_url`` 在 ``PUBLIC_BASE_URL`` 为空时是**相对路径**(SPA 同源内可直接点),
配置了前缀则是绝对地址(把链接发给外部人员的场景)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import tool

from app.config import get_settings
from app.docgen import genstore
from app.docgen.docx_builder import build_docx
from app.docgen.md_builder import build_md
from app.docgen.pdf_builder import build_pdf
from app.docgen.pptx_builder import build_pptx
from app.docgen.xlsx_builder import build_xlsx

logger = logging.getLogger(__name__)


def build_image(path, spec: dict) -> None:
    """把解析好的第一张图片(本地 PNG)复制成交付文件。

    图片已由 images.resolve_images 归一化为 token 目录下的 img_*.png; 这里只负责把它
    们中的第一张落到正式文件名上。无可用图则抛 ValueError 由 _generate 转成 {error}。
    """
    import shutil

    images = spec.get("images") or []
    src = str((images[0] or {}).get("path") or "") if images else ""
    if not src:
        # 容错: 直接把 src/path 写在顶层的简写法(generate_image 常这样传)。
        raw = str(spec.get("src") or spec.get("path") or "").strip()
        raise ValueError(f"没有可用的图片(检查 src 是否本地png或图片URL; 当前: {raw[:80]})")
    shutil.copyfile(src, str(path))


def _download_url(token: str, file_name: str) -> str:
    base = (get_settings().public_base_url or "").rstrip("/")
    path = f"/api/files/{token}/{file_name}"
    return f"{base}{path}" if base else path


async def _generate(
    kind: str, spec_raw: Any, builder, title_fallback: str, *, resolve_imgs: bool = True
) -> dict[str, Any]:
    """各生成工具共用的编排: 解析 spec -> (图片解析落盘) -> to_thread 构建 -> 限量校验。

    ``resolve_imgs=True`` 时先把 spec.images 里的本地/URL 图归一化 PNG 落到 token 目录,
    并把 spec["images"] 换成带本地 path 的条目 —— 网络/SSRF 都在这一步(异步)完成, 不进
    同步 builder。md 工具置 False(它只引用原始 src)。单图解析失败只丢这一张并记
    image_warnings, 不阻断整篇生成。
    """
    settings = get_settings()
    spec, parse_error = genstore.parse_spec(spec_raw)
    if parse_error:
        return {"error": parse_error, "kind": kind}

    ext = f".{kind}"
    title = str((spec or {}).get("title") or title_fallback).strip()
    token = genstore.new_token()
    file_name = genstore.new_file_name(ext, title)
    directory = genstore.gen_dir(token)
    path = directory / file_name

    image_warnings: list[str] = []
    # 图片来源: spec.images 优先; 只有顶层 spec.src(generate_image 常这样传)也当成一张。
    img_refs = (spec or {}).get("images") or (
        [{"src": spec["src"]}] if (spec or {}).get("src") else []
    )
    if resolve_imgs and img_refs:
        from app.docgen import images as docgen_images

        resolved, image_warnings = await docgen_images.resolve_images(img_refs, directory)
        spec = {**(spec or {}), "images": resolved}

    try:
        await asyncio.to_thread(builder, path, spec or {})
    except ValueError as exc:
        # spec 结构问题: 原样回给模型, 它能按提示修一版重试。
        return {"error": f"spec 校验失败: {exc}", "kind": kind}
    except Exception as exc:  # noqa: BLE001 - 构建库的任何异常都不能炸 ReAct 循环
        logger.warning("generate_%s failed: %s", kind, exc)
        return {"error": f"文档生成失败({type(exc).__name__})", "kind": kind}

    size = path.stat().st_size
    if size > settings.docgen_max_bytes:
        path.unlink(missing_ok=True)
        return {
            "error": f"生成物 {size // 1024}KB 超过上限 {settings.docgen_max_bytes // 1024 // 1024}MB, 请精简内容",
            "kind": kind,
        }

    # 顺带清扫过期生成物(opportunistic, 计划 E4): 不依赖定时任务, 失败只记日志。
    try:
        await asyncio.to_thread(genstore.cleanup_expired)
    except Exception as exc:  # noqa: BLE001
        logger.warning("docgen cleanup failed: %s", exc)

    logger.info("docgen generated %s (%d bytes) token=%s", file_name, size, token)
    result: dict[str, Any] = {
        "kind": kind,
        "file_name": file_name,
        "doc_token": token,
        "download_url": _download_url(token, file_name),
        "size_bytes": size,
        "mime": genstore.mime_of(ext),
    }
    if image_warnings:
        result["image_warnings"] = image_warnings[:10]
    return result


@tool
async def generate_docx(spec: str) -> dict[str, Any]:
    """生成一份 Word(.docx) 文档并返回下载链接。

    Args:
        spec: JSON 字符串, 字段全部可选: {"title": "标题", "subtitle": "副标题",
            "sections": [{"heading": "小节标题", "body": "正文, 换行分段"}],
            "bullets": ["要点"], "table": {"columns": ["列"], "rows": [["值"]]},
            "images": [{"src": "本地png或图片URL", "caption": "图1", "width": 5.5}]}

    Returns:
        成功: {kind, file_name, doc_token, download_url, size_bytes, mime} ——
        把 download_url 原样完整告诉用户(可点击下载), 不要改写或截断。
        失败: {error, kind}; spec 结构问题会给出修正提示, 修好后重试一次。
    """
    return await _generate("docx", spec, build_docx, "未命名文档")


@tool
async def generate_xlsx(spec: str) -> dict[str, Any]:
    """生成一份 Excel(.xlsx) 工作簿并返回下载链接。

    Args:
        spec: JSON 字符串, 两种写法二选一:
            {"sheets": [{"name": "页名", "headers": ["列"], "rows": [["值"]]}]}
            或单表简写 {"headers": ["列"], "rows": [["值"]]}; 数值请保持数字类型。
            可选 "images": [{"src": "本地png或图片URL", "caption": "图1"}] 会贴到一个独立"附图"表。

    Returns:
        成功: {kind, file_name, doc_token, download_url, size_bytes, mime};
        失败: {error, kind}, spec 结构问题会给出修正提示。
    """
    return await _generate("xlsx", spec, build_xlsx, "数据表")


@tool
async def generate_pptx(spec: str) -> dict[str, Any]:
    """生成一份 PowerPoint(.pptx) 演示文稿并返回下载链接。

    Args:
        spec: JSON 字符串: {"title": "封面标题", "subtitle": "封面副标题",
            "slides": [{"title": "页标题", "bullets": ["要点", {"text": "子要点", "level": 1}]}],
            "images": [{"src": "本地png或图片URL", "caption": "图1"}] 每张图单独占一页}

    Returns:
        成功: {kind, file_name, doc_token, download_url, size_bytes, mime};
        失败: {error, kind}, spec 结构问题会给出修正提示。
    """
    return await _generate("pptx", spec, build_pptx, "演示文稿")


@tool
async def generate_pdf(spec: str) -> dict[str, Any]:
    """生成一份 PDF 文档(中文由内置 CID 字体渲染)并返回下载链接。

    Args:
        spec: JSON 字符串, 字段与 generate_docx 同构: {"title", "subtitle",
            "sections": [{"heading", "body"}], "bullets": ["要点"],
            "table": {"columns": ["列"], "rows": [["值"]]},
            "images": [{"src": "本地png或图片URL", "caption": "图1"}]}

    Returns:
        成功: {kind, file_name, doc_token, download_url, size_bytes, mime};
        失败: {error, kind}, spec 结构问题会给出修正提示。
    """
    return await _generate("pdf", spec, build_pdf, "生成文档")


@tool
async def generate_md(spec: str) -> dict[str, Any]:
    """生成一份 Markdown(.md) 纯文本文档并返回下载链接。

    适合要进 Git / 再编辑 / 当纯文本交付的场景; 与 docx/pdf 共用同一份 spec 结构。

    Args:
        spec: JSON 字符串: {"title", "subtitle", "sections": [{"heading", "body"}],
            "bullets": ["要点"], "table": {"columns": [...], "rows": [[...]]},
            "images": [{"src": "图片URL或文件名", "caption": "图1"]}} —— md 里图片写成
            ![caption](src), src 用原值(URL 保持可移植)。

    Returns:
        成功: {kind, file_name, doc_token, download_url, size_bytes, mime};
        失败: {error, kind}。
    """
    return await _generate("md", spec, build_md, "文档", resolve_imgs=False)


@tool
async def generate_image(spec: str) -> dict[str, Any]:
    """把一张图片(本地文件或图片URL)归一化为 PNG 并作为可下载文件交付。

    用途: 需要把检索/生成的一张图直接作为产物给用户(而非嵌进文档)。图过 SSRF 护栏
    (URL 拒内网)与体积/尺寸限制, 统一输出 PNG。

    Args:
        spec: JSON 字符串: {"title": "可选, 用于命名", "src": "本地png或图片URL"}
            (也可用 images: [{"src": ...}], 取第一张)。

    Returns:
        成功: {kind, file_name, doc_token, download_url, size_bytes, mime};
        失败: {error, kind}(src 缺失/不可达/非图片)。
    """
    return await _generate("png", spec, build_image, "图片")


# 供 app/tools/__init__.py 的能力注册表引用; 文件类型后缀 -> builder 的映射
# 顺便给测试用(生成产物必须能被对应库重新打开)。
BUILDERS: dict[str, Any] = {
    "docx": build_docx,
    "xlsx": build_xlsx,
    "pptx": build_pptx,
    "pdf": build_pdf,
    "md": build_md,
    "png": build_image,
}
