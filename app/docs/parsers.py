"""Multi-modal document parsers producing section-level blocks.

Each parser returns a list of ``ParsedBlock`` (section text + page number +
section heading), which the ingestion pipeline turns into parent/child
chunks. Supported types:

- text:  txt / md (md split by headings) / pdf (per real page; pages without a
         text layer are OCR'd) / docx (split by Heading styles) / pptx (per
         slide) / xlsx (per sheet, table-row group chunks)
- video transcripts: srt / vtt / *.transcript.txt (timeline merged)
- images: jpg / jpeg / png / webp / bmp -> OCR'd by the local MinerU service
  (mineru-api /file_parse) so image content becomes searchable text.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

SUPPORTED_TEXT_EXT = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx"}
SUPPORTED_VIDEO_EXT = (".srt", ".vtt", ".transcript.txt")
SUPPORTED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# MinerU 客户端也进程级复用: 入库一篇扫描 PDF 会发 N 次 OCR(每页一次), 每次都新建
# 客户端 = N 次握手 + N 个 TIME_WAIT; OCR 又是分钟级慢服务, 并发上限要卡住。
_mineru_client: httpx.AsyncClient | None = None


def _get_mineru_client() -> httpx.AsyncClient:
    """Lazily build the process-wide MinerU client (bounded pool)."""
    global _mineru_client
    if _mineru_client is None or _mineru_client.is_closed:
        s = get_settings()
        _mineru_client = httpx.AsyncClient(
            # 默认不设总超时: 单次 OCR 由调用点传 MINERU_TIMEOUT(分钟级),
            # 而建连必须快失败(服务没起时不要把入队列挂在 TCP 上)。
            timeout=httpx.Timeout(float(s.mineru_timeout), connect=10.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
    return _mineru_client


async def close_mineru_client() -> None:
    """释放 OCR 连接池(供 lifespan 关停调用, 与 close_web_client 同风格)。"""
    global _mineru_client
    if _mineru_client is not None and not _mineru_client.is_closed:
        try:
            await _mineru_client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("closing mineru client failed: %s", exc)
    _mineru_client = None


@dataclass
class ParsedBlock:
    """A section-level block of parsed document text.

    ``start_offset/end_offset/anchor/parent_type`` 是正文外置后的定位字段, 默认未填:
    offset 基准是 normalized_text, 由入库侧统一回算(build_parent_child 的 _locate);
    无锦点时 parent_type 退化为 "section"。
    """

    section: str       # heading path / slide title / sheet name; "" if none
    page_no: int       # pdf page / pptx slide / xlsx sheet number; -1 unknown
    text: str
    start_offset: int = -1
    end_offset: int = -1
    anchor: dict | None = None
    parent_type: str = "section"  # section/clause/table/faq


def supported_extensions() -> set[str]:
    """All uploadable extensions."""
    return SUPPORTED_TEXT_EXT | set(SUPPORTED_VIDEO_EXT) | SUPPORTED_IMAGE_EXT


def modality_of(path: Path) -> str:
    """Map a file to its modality: text / video_transcript / image."""
    name = path.name.lower()
    if any(name.endswith(ext) for ext in SUPPORTED_VIDEO_EXT):
        return "video_transcript"
    if path.suffix.lower() in SUPPORTED_IMAGE_EXT:
        return "image"
    return "text"


# ---------------- text-family parsers ----------------


def _parse_txt(path: Path) -> list[ParsedBlock]:
    text = path.read_text(encoding="utf-8")
    return [ParsedBlock(section="", page_no=-1, text=text)] if text.strip() else []


def _parse_md(path: Path) -> list[ParsedBlock]:
    """Split markdown by headings; section = current heading path."""
    text = path.read_text(encoding="utf-8")
    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            section = "/".join(h for _, h in heading_stack)
            blocks.append(ParsedBlock(section=section, page_no=-1, text=body))
        buf.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, m.group(2).strip()))
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return blocks


def _page_image_pngs(page) -> list[bytes]:
    """Extract a PDF page's embedded images as PNG bytes (best effort)."""
    out: list[bytes] = []
    for img in page.images:
        try:
            buf = io.BytesIO()
            img.image.convert("RGB").save(buf, format="PNG")
            out.append(buf.getvalue())
        except Exception as exc:  # skip undecodable image, keep the other pages
            logger.warning("pdf page image decode failed: %s", exc)
    return out


def _extract_page_payload(page) -> tuple[str, list[bytes]]:
    """一页 PDF 的 CPU 部分: 取文本层 + 抽内嵌图为 PNG(一并交给线程)。

    pypdf 的 extract_text/images 是纯 CPU 且无 GIL 释放, 整篇循环留在事件循环里
    会按页数线性卡住共进程的所有对话; OCR 的等待仍然在循环外做(不进这一层)。
    """
    text = (page.extract_text() or "").strip()
    return text, ([] if text else _page_image_pngs(page))


async def _parse_pdf(path: Path) -> list[ParsedBlock]:
    """One block per page so chunks keep the real page number.

    Pages without a text layer (scanned/image-only PDF) fall back to OCR'ing
    the page's embedded images via MinerU, so they stay searchable.
    """
    from pypdf import PdfReader

    reader = await asyncio.to_thread(PdfReader, str(path))
    blocks: list[ParsedBlock] = []
    ocr_exc: Exception | None = None
    for idx, page in enumerate(reader.pages, 1):
        text, pngs = await asyncio.to_thread(_extract_page_payload, page)
        if not text and pngs:
            ocr_parts: list[str] = []
            for n, data in enumerate(pngs, 1):
                try:
                    ocr_parts.append(await _mineru_parse(f"page{idx}-{n}.png", data))
                except Exception as exc:  # e.g. MinerU service unavailable
                    ocr_exc = exc
                    logger.warning("pdf page %d OCR failed: %s", idx, exc)
            text = "\n\n".join(p for p in ocr_parts if p).strip()
        if text:
            blocks.append(ParsedBlock(section="", page_no=idx, text=text))
    if not blocks and ocr_exc is not None:
        # No text layer anywhere and OCR was unavailable: surface the real cause
        # instead of the misleading "no valid content" error.
        raise ocr_exc
    return blocks


def _parse_docx(path: Path) -> list[ParsedBlock]:
    """Split docx by Heading styles (supports EN/CN style names)."""
    import docx

    document = docx.Document(str(path))
    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            section = "/".join(h for _, h in heading_stack)
            blocks.append(ParsedBlock(section=section, page_no=-1, text=body))
        buf.clear()

    for para in document.paragraphs:
        style = para.style.name if para.style else ""
        m = re.match(r"^(?:Heading|标题)\s*(\d+)$", style)
        if m and para.text.strip():
            flush()
            level = int(m.group(1))
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, para.text.strip()))
            buf.append(para.text.strip())
        elif para.text.strip():
            buf.append(para.text.strip())
    flush()
    return blocks


def _parse_pptx(path: Path) -> list[ParsedBlock]:
    """One block per slide; section = slide title, page_no = slide number."""
    from pptx import Presentation

    prs = Presentation(str(path))
    blocks: list[ParsedBlock] = []
    for idx, slide in enumerate(prs.slides, 1):
        title = ""
        texts: list[str] = []
        if slide.shapes.title is not None and slide.shapes.title.text.strip():
            title = slide.shapes.title.text.strip()
        for shape in slide.shapes:
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in p.runs).strip()
                    if line:
                        texts.append(line)
            if shape.has_table:
                for row in shape.table.rows:
                    line = " | ".join(cell.text.strip() for cell in row.cells)
                    if line.strip(" |"):
                        texts.append(line)
        body = "\n".join(texts).strip()
        if body:
            blocks.append(ParsedBlock(section=title or f"第{idx}页", page_no=idx, text=body))
    return blocks


# 表格父块预算: 与 settings.parent_chunk_max 保持一致, 保证每个块都是单 child
# (父块 <= parent_chunk_max 时不再滑窗切分), 且切分只发生在行边界。
_TABLE_PARENT_BUDGET = 1200


def _parse_xlsx(path: Path) -> list[ParsedBlock]:
    """Render each sheet as `列名: 值` lines, grouped into row-aligned table blocks.

    两个设计点都直接决定表格问答的正确率:
    1. 合并单元格回填 + 每行携带列名 —— 托管商类表格把同组公用的地址/电话/账号
       做成纵向合并区, openpyxl 只在区域左上角存值, 其余行读出来是 None;
       不回填则"某分公司账号是多少"根本拼不出完整一行。裸 `值 | 值` 行里
       电话/账号/纳税人识别号全是数字串, 列含义只能靠位置推断, LLM 极易取错列;
    2. 按行边界分块(每块 <= _TABLE_PARENT_BUDGET) —— 整表一个 block 会被 512
       滑窗拦腰截断目标行, 且父块重装配时 24 行相似记录互相干扰。
    """
    from openpyxl import load_workbook

    # 不用 read_only: 只读模式拿不到 merged_cells 布局, 无法回填合并区值
    wb = load_workbook(str(path), data_only=True)
    blocks: list[ParsedBlock] = []
    for idx, sheet in enumerate(wb.worksheets, 1):
        # 合并区锚点(左上角)值回填到区内每个格子, 让每一行自包含
        merged: dict[tuple[int, int], object] = {}
        for rng in sheet.merged_cells.ranges:
            anchor = sheet.cell(rng.min_row, rng.min_col).value
            if anchor is None or str(anchor).strip() == "":
                continue
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if (r, c) != (rng.min_row, rng.min_col):
                        merged[(r, c)] = anchor

        lines: list[str] = []
        headers: list[str] = []
        for r, row in enumerate(sheet.iter_rows(values_only=True), 1):
            cells: list[str] = []
            for c, val in enumerate(row, 1):
                if val is None:
                    val = merged.get((r, c))
                cells.append(str(val).strip() if val is not None else "")
            if not any(cells):
                continue
            if not headers:  # 首行非空行视为表头
                headers = cells
                continue
            if headers:
                pairs = [
                    f"{h}: {v}"
                    for h, v in zip(headers, cells)
                    if v and h and h != v  # 空单元格丢列; 与表头同值的列(公司名自引用)省略
                ]
                line = " | ".join(pairs)
            else:
                line = " | ".join(c for c in cells if c)
            if line:
                lines.append(line)
        # 按行贪心装桶: 单行超预算时保持完整不切开(带列名的长行仍自解释)
        group: list[str] = []
        size = 0
        for line in lines:
            span = len(line) + (1 if group else 0)
            if group and size + span > _TABLE_PARENT_BUDGET:
                blocks.append(ParsedBlock(
                    section=sheet.title, page_no=idx, text="\n".join(group),
                    parent_type="table",
                ))
                group, size = [], 0
            group.append(line)
            size += span
        if group:
            blocks.append(ParsedBlock(
                section=sheet.title, page_no=idx, text="\n".join(group),
                parent_type="table",
            ))
    wb.close()
    return blocks


def _parse_subtitle(path: Path) -> list[ParsedBlock]:
    """Parse srt/vtt (or plain transcript) into one timeline-annotated block."""
    raw = path.read_text(encoding="utf-8").replace("WEBVTT", "")
    block_re = re.compile(
        r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
        r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n(?P<text>.*?)(?=\n\s*\n|\Z)",
        re.DOTALL,
    )
    lines: list[str] = []
    for m in block_re.finditer(raw):
        text = re.sub(r"<[^>]+>", "", m.group("text")).strip()
        if text:
            lines.append(f"[{m.group('start')} -> {m.group('end')}] {text}")
    if not lines and raw.strip():  # plain transcript fallback
        lines.append(raw.strip())
    return [ParsedBlock(section="", page_no=-1, text="\n".join(lines))] if lines else []


# ---------------- OCR via the MinerU service ----------------


async def _mineru_parse(filename: str, data: bytes) -> str:
    """OCR one file with the MinerU service (mineru-api POST /file_parse).

    Returns the markdown text MinerU produced ("" when it recognised nothing).

    客户端复用进程级共享池(见 _get_mineru_client): OCR 在入库路径上可能一次发
    几十页, 每页新建客户端会把握手与 TIME_WAIT 乘上页数。
    """
    settings = get_settings()
    files = {"files": (filename, data)}
    form = {"backend": settings.mineru_backend, "return_md": "true", "lang_list": "ch"}
    client = _get_mineru_client()
    try:
        resp = await client.post(
            f"{settings.mineru_base_url.rstrip('/')}/file_parse",
            files=files,
            data=form,
            timeout=float(settings.mineru_timeout),
        )
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"图片解析超时(MinerU backend={settings.mineru_backend}, 阈值 "
            f"{settings.mineru_timeout}s): 模型加载较慢或图片过大, "
            f"可在 .env 调大 MINERU_TIMEOUT 后重试"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"图片解析失败(MinerU 服务 {settings.mineru_base_url} 不可用, "
            f"请确认已启动 mineru-api, 详见 README 的 MinerU 部署说明): {exc}"
        ) from exc
    # mineru-api 3.x 返回 {"results": {"<文件名去后缀>": {"md_content": ...}}},
    # 以 file_names 为准取第一份结果的 markdown。
    payload = resp.json()
    results = payload.get("results") or {}
    names = payload.get("file_names") or list(results.keys())
    for name in names:
        text = (results.get(name, {}).get("md_content") or "").strip()
        if text:
            return text
    return ""


async def _parse_image(path: Path) -> list[ParsedBlock]:
    """OCR an image with the MinerU service (mineru-api POST /file_parse)."""
    try:
        data = path.read_bytes()
    except Exception as exc:
        raise RuntimeError(f"图片文件无法读取或已损坏: {path.name} ({exc})") from exc
    text = await _mineru_parse(path.name, data)
    return [ParsedBlock(section="图片内容", page_no=-1, text=text)] if text else []


# ---------------- unified entry ----------------


async def parse_blocks(path: Path) -> tuple[str, list[ParsedBlock]]:
    """Parse any supported file into (modality, section blocks).

    除 pdf/image 外的解析器都是同步库(python-docx / openpyxl / python-pptx)且很吃
    CPU: 一个几十页的 pptx 能占住线程几十毫秒到秒级。它们必须走 ``asyncio.to_thread``,
    否则一个入库请求就能把同一事件循环上所有人的流式回复卡住(入库与对话同进程)。
    """
    modality = modality_of(path)
    name = path.name.lower()
    if modality == "video_transcript":
        return modality, await asyncio.to_thread(_parse_subtitle, path)
    if modality == "image":
        return modality, await _parse_image(path)

    ext = path.suffix.lower()
    if name.endswith(".transcript.txt"):
        return "video_transcript", await asyncio.to_thread(_parse_subtitle, path)
    if ext == ".txt":
        return modality, await asyncio.to_thread(_parse_txt, path)
    if ext == ".md":
        return modality, await asyncio.to_thread(_parse_md, path)
    if ext == ".pdf":
        return modality, await _parse_pdf(path)
    if ext == ".docx":
        return modality, await asyncio.to_thread(_parse_docx, path)
    if ext == ".pptx":
        return modality, await asyncio.to_thread(_parse_pptx, path)
    if ext == ".xlsx":
        return modality, await asyncio.to_thread(_parse_xlsx, path)
    raise ValueError(f"Unsupported file type: {path.name}")
