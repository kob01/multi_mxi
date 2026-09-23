"""Multi-modal document parsers producing section-level blocks.

Each parser returns a list of ``ParsedBlock`` (section text + page number +
section heading), which the ingestion pipeline turns into parent/child
chunks. Supported types:

- text:  txt / md (md split by headings) / pdf (per real page; pages without a
         text layer are OCR'd) / docx (split by Heading styles) / pptx (per
         slide) / xlsx (per sheet)
- video transcripts: srt / vtt / *.transcript.txt (timeline merged)
- images: jpg / jpeg / png / webp / bmp -> OCR'd by the local MinerU service
  (mineru-api /file_parse) so image content becomes searchable text.
"""

from __future__ import annotations

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


@dataclass
class ParsedBlock:
    """A section-level block of parsed document text."""

    section: str       # heading path / slide title / sheet name; "" if none
    page_no: int       # pdf page / pptx slide / xlsx sheet number; -1 unknown
    text: str


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


async def _parse_pdf(path: Path) -> list[ParsedBlock]:
    """One block per page so chunks keep the real page number.

    Pages without a text layer (scanned/image-only PDF) fall back to OCR'ing
    the page's embedded images via MinerU, so they stay searchable.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    blocks: list[ParsedBlock] = []
    ocr_exc: Exception | None = None
    for idx, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if not text:
            ocr_parts: list[str] = []
            for n, data in enumerate(_page_image_pngs(page), 1):
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


def _parse_xlsx(path: Path) -> list[ParsedBlock]:
    """One block per sheet; rows rendered as `cell | cell` lines."""
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    blocks: list[ParsedBlock] = []
    for idx, sheet in enumerate(wb.worksheets, 1):
        lines: list[str] = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append(" | ".join(cells))
        body = "\n".join(lines).strip()
        if body:
            blocks.append(ParsedBlock(section=sheet.title, page_no=idx, text=body))
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
    """
    settings = get_settings()
    files = {"files": (filename, data)}
    form = {"backend": settings.mineru_backend, "return_md": "true", "lang_list": "ch"}
    async with httpx.AsyncClient(timeout=float(settings.mineru_timeout)) as client:
        try:
            resp = await client.post(
                f"{settings.mineru_base_url.rstrip('/')}/file_parse",
                files=files,
                data=form,
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
    """Parse any supported file into (modality, section blocks)."""
    modality = modality_of(path)
    name = path.name.lower()
    if modality == "video_transcript":
        return modality, _parse_subtitle(path)
    if modality == "image":
        return modality, await _parse_image(path)

    ext = path.suffix.lower()
    if name.endswith(".transcript.txt"):
        return "video_transcript", _parse_subtitle(path)
    if ext == ".txt":
        return modality, _parse_txt(path)
    if ext == ".md":
        return modality, _parse_md(path)
    if ext == ".pdf":
        return modality, await _parse_pdf(path)
    if ext == ".docx":
        return modality, _parse_docx(path)
    if ext == ".pptx":
        return modality, _parse_pptx(path)
    if ext == ".xlsx":
        return modality, _parse_xlsx(path)
    raise ValueError(f"Unsupported file type: {path.name}")
