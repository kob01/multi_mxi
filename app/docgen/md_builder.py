"""Markdown(.md) 构建器: 纯字符串拼接, 无任何第三方依赖。

存在的意义: office 三件套(docx/pptx/xlsx)适合"给人看/汇报", 但很多场景用户要的是能直接
进 Git / 再编辑 / 喂给别的东西的**纯文本**。Markdown 是唯一"零依赖 + 可移植 + 可 diff"的
交付格式, 且与 docx/pdf 共用同一份 spec(title/subtitle/sections/bullets/table/images),
模型不用为换格式重写一遍数据结构。

images 写成 ``![caption](src)``: 这里用**原始 src**(URL 或文件名), 而不是解析层的本地
绝对路径 —— 那种路径出了这台机器就没意义, 反而毁掉 Markdown 的可移植性。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MAX_ITEMS = 200


def _table_md(table: dict[str, Any]) -> list[str]:
    columns = [str(c) for c in (table.get("columns") or [])]
    if not columns:
        return []
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join([" --- "] * len(columns)) + "|"]
    for row in (table.get("rows") or [])[:_MAX_ITEMS]:
        cells = [str((row or [])[i]) if i < len(row or []) else "" for i in range(len(columns))]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def build_md(path: Path, spec: dict[str, Any]) -> None:
    title = str(spec.get("title") or "").strip()
    sections = [s for s in (spec.get("sections") or []) if isinstance(s, dict)]
    if not title and not sections and not (spec.get("table") or spec.get("images")):
        raise ValueError("spec 至少需要 title/sections/table/images 之一, 否则生成的是空文档")

    lines: list[str] = []
    if title:
        lines += [f"# {title}", ""]
    subtitle = str(spec.get("subtitle") or "").strip()
    if subtitle:
        lines += [f"> {subtitle}", ""]

    for section in sections[:_MAX_ITEMS]:
        heading = str(section.get("heading") or "").strip()
        if heading:
            lines += [f"## {heading}", ""]
        body = str(section.get("body") or "").strip()
        if body:
            lines += [body, ""]

    bullets = spec.get("bullets") or []
    if bullets:
        for bullet in bullets[:_MAX_ITEMS]:
            text = str(bullet).strip()
            if text:
                lines.append(f"- {text}")
        lines.append("")

    table_lines = _table_md(spec.get("table") or {})
    if table_lines:
        lines += table_lines + [""]

    for img in (spec.get("images") or [])[:_MAX_ITEMS]:
        src = str((img or {}).get("src") or (img or {}).get("path") or "").strip()
        if not src:
            continue
        caption = str((img or {}).get("caption") or "").strip()
        lines.append(f"![{caption}]({src})")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
