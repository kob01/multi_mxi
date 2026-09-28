"""Excel(.xlsx) 构建器: openpyxl, 纯同步函数(由 tools/docgen.py 用 to_thread 卸载)。

spec 结构(两种写法二选一):
    {"sheets": [{"name": "季度费用", "headers": ["部门", "金额"], "rows": [["研发", 42]]}]}
    或单表简写: {"headers": [...], "rows": [...]}(自动命名 Sheet1)

可选 "images": [{"path": "本地png", "caption": "图1"}] —— 图片统一贴到一个单独的
"附图"工作表(不混在数据里), 图不可读则跳过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter


def _normalize_sheets(spec: dict[str, Any]) -> list[dict[str, Any]]:
    if spec.get("sheets"):
        return [s for s in spec["sheets"] if isinstance(s, dict)]
    if spec.get("headers") or spec.get("rows"):
        return [{"name": "Sheet1", "headers": spec.get("headers"), "rows": spec.get("rows")}]
    # 与 docx/pdf 同构的 table 区块也接受(单一工作表): 四个 builder 共享同一份 spec 时,
    # 不该因为换了文件类型就让模型重写一遍数据结构。
    table = spec.get("table") or {}
    if table.get("columns") or table.get("rows"):
        name = str(spec.get("title") or "Sheet1")[:31] or "Sheet1"
        return [{"name": name, "headers": table.get("columns"), "rows": table.get("rows")}]
    return []


def build_xlsx(path: Path, spec: dict[str, Any]) -> None:
    sheets = _normalize_sheets(spec)
    if not sheets:
        raise ValueError('spec 需要 {"sheets": [{"name", "headers", "rows"}]} 或顶层 headers/rows')

    wb = Workbook()
    wb.remove(wb.active)  # 删掉默认 Sheet, 全部按 spec 建
    header_font = Font(bold=True)

    for idx, sheet_spec in enumerate(sheets):
        name = str(sheet_spec.get("name") or f"Sheet{idx + 1}")[:31] or f"Sheet{idx + 1}"
        headers = [str(h) for h in (sheet_spec.get("headers") or [])]
        rows = list(sheet_spec.get("rows") or [])
        if not headers and not rows:
            continue
        ws = wb.create_sheet(title=name)
        if headers:
            ws.append(headers)
            for col in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=col)
                cell.font = header_font
            ws.freeze_panes = "A2"
        for row in rows:
            ws.append(list(row))
        # 列宽按内容估一个上限值: 完全不设会让中文列挤成一列宽的"#####"; 自动宽算法很贵。
        for col in range(1, (len(headers) or 1) + 1):
            width = max(
                [len(str(headers[col - 1])) if headers and col <= len(headers) else 0]
                + [len(str(row[col - 1])) if len(row) >= col else 0 for row in rows[:50]]
            )
            ws.column_dimensions[get_column_letter(col)].width = min(max(width + 4, 10), 42)

    if not wb.sheetnames:
        raise ValueError("spec 里没有任何可写入的工作表")

    _add_images(wb, spec.get("images") or [])
    wb.save(str(path))


def _add_images(wb: Any, images: list[dict[str, Any]]) -> None:
    """把所有可读书的图贴到一个新增的"附图"工作表, 逐行错开锚点。

    openpyxl 的 Image 会惰性依赖 PIL; 图不可读就跳过, 不为一坏图毁整个工作簿。
    没有一张能读的图时不建空表。
    """
    usable = [str((img or {}).get("path") or "") for img in images if (img or {}).get("path")]
    usable = [p for p in usable if Path(p).is_file()]
    if not usable:
        return
    try:
        from openpyxl.drawing.image import Image as XLImage
    except Exception:  # noqa: BLE001 - openpyxl/Pillow 不可用时放弃嵌图, 不影响数据表
        return
    ws = wb.create_sheet(title="附图")
    row = 1
    for img, path in zip(images, usable):
        try:
            picture = XLImage(path)
            picture.anchor = f"A{row}"
            ws.add_image(picture)
        except Exception:  # noqa: BLE001 - 单张坏图不毁整个表
            continue
        caption = str(img.get("caption") or "").strip()
        if caption:
            ws.cell(row=row, column=6, value=caption)
        row += 20
