"""PDF 构建器: reportlab platypus, 纯同步函数(由 tools/docgen.py 用 to_thread 卸载)。

中文渲染走 **内置 CID 字体** ``STSong-Light``(计划 A1 的硬性要求): 不打包任何 TTF,
镜像里没有字体文件也能出中文 —— 代价是字形由阅读器侧的 CID 映射提供, 极少数阅读器
显示效果略糙; 需要强嵌入字形时再引入 TTF(计划已列为后续项)。

spec 结构(与 docx 同构):
    {"title": "标题", "subtitle": "副标题",
     "sections": [{"heading": "小节标题", "body": "正文, 换行分段"}],
     "bullets": ["要点1"],
     "table": {"columns": [...], "rows": [[...]]},
     "images": [{"path": "本地png", "caption": "图1"}]}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# A4 内容宽(mm): 210 - 左右边距各 20; 图片按此宽度上限缩放。
_CONTENT_W_MM = 170.0

# 模块级注册一次即可: CID 字体注册是进程级全局表, 重复注册会抛 duplicate 错。
_FONT_REGISTERED = False

_CJK_FONT = "STSong-Light"
_TITLE_STYLE = ParagraphStyle("GenTitle", fontName=_CJK_FONT, fontSize=18, leading=26, spaceAfter=6)
_SUBTITLE_STYLE = ParagraphStyle("GenSubtitle", fontName=_CJK_FONT, fontSize=11, leading=16, textColor=colors.HexColor("#67718a"), spaceAfter=12)
_HEADING_STYLE = ParagraphStyle("GenHeading", fontName=_CJK_FONT, fontSize=14, leading=20, spaceBefore=10, spaceAfter=4)
_BODY_STYLE = ParagraphStyle("GenBody", fontName=_CJK_FONT, fontSize=10.5, leading=17, wordWrap="CJK")
_CELL_STYLE = ParagraphStyle("GenCell", fontName=_CJK_FONT, fontSize=9.5, leading=13, wordWrap="CJK")


def _ensure_font() -> None:
    global _FONT_REGISTERED
    if not _FONT_REGISTERED:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
        _FONT_REGISTERED = True


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    # Paragraph 会把文本当 mini-HTML 解析, 用户内容必须先转义, 否则一个 "<" 就毁整份文档。
    return Paragraph(escape(str(text)), style)


def _table_flowable(table: dict[str, Any]) -> Table | None:
    columns = [str(c) for c in (table.get("columns") or [])]
    if not columns:
        return None
    rows = [[str(c) for c in (row or [])] for row in (table.get("rows") or [])][:200]
    data = [[_para(c, _CELL_STYLE) for c in columns]]
    for row in rows:
        data.append([_para(row[idx] if idx < len(row) else "", _CELL_STYLE) for idx in range(len(columns))])
    flow = Table(data, colWidths=[160 * mm / len(columns)] * len(columns), repeatRows=1)
    flow.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d8dcea")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2fb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return flow


def build_pdf(path: Path, spec: dict[str, Any]) -> None:
    _ensure_font()
    title = str(spec.get("title") or "").strip()
    sections = [s for s in (spec.get("sections") or []) if isinstance(s, dict)]
    if not title and not sections:
        raise ValueError("spec 至少需要 title 或 sections, 否则生成的是空文档")

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        title=title or "生成文档",
        author="mxi-docgen",
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
    )
    story: list[Any] = []
    if title:
        story.append(_para(title, _TITLE_STYLE))
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle:
        story.append(_para(subtitle, _SUBTITLE_STYLE))

    for section in sections:
        heading = str(section.get("heading") or "").strip()
        if heading:
            story.append(_para(heading, _HEADING_STYLE))
        for line in str(section.get("body") or "").splitlines():
            if line.strip():
                story.append(_para(line.strip(), _BODY_STYLE))

    for bullet in spec.get("bullets") or []:
        text = str(bullet).strip()
        if text:
            story.append(_para(f"• {text}", _BODY_STYLE))

    flow = _table_flowable(spec.get("table") or {})
    if flow is not None:
        story.append(Spacer(1, 6))
        story.append(flow)

    _append_images(story, spec.get("images") or [])

    doc.build(story)


def _append_images(story: list[Any], images: list[dict[str, Any]]) -> None:
    """逐张把本地 PNG 缩到内容宽以内后追加, 图后跟一行题注。

    取 PIL 得原图宽高比再按页宽定尺寸(reportlab 的 Image 不自己读比例); 图不可读
    跳过这一张, 不影响其余内容。
    """
    _caption_style = ParagraphStyle("GenCaption", fontName=_CJK_FONT, fontSize=9, leading=13,
                                    textColor=colors.HexColor("#67718a"), spaceAfter=8)
    for img in images:
        p = str((img or {}).get("path") or "")
        if not p or not Path(p).is_file():
            continue
        try:
            from PIL import Image as PILImage

            with PILImage.open(p) as im:
                w_px, h_px = im.size
            if w_px <= 0:
                continue
            max_w = _CONTENT_W_MM * mm
            width = max_w
            height = width * h_px / w_px
            # 超高图缩到页宽一半以内, 避免一张竖长图占满一整页还装不下。
            max_h = 240 * mm
            if height > max_h:
                ratio = max_h / height
                width *= ratio
                height *= ratio
            story.append(Spacer(1, 4))
            story.append(RLImage(p, width=width, height=height))
        except Exception:  # noqa: BLE001 - 单张图坏不毁整份 PDF
            continue
        caption = str(img.get("caption") or "").strip()
        if caption:
            story.append(_para(caption, _caption_style))
    
