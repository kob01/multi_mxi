"""Knowledge ingestion pipeline (父子双表 + 正文外置)。

Stages: parse into section-level blocks -> normalize -> build structure ->
parent/child split -> (parent full text -> Mongo) -> embed changed children ->
single-transaction publish into ``doc_parents``/``doc_chunks`` -> BM25 rebuild.

Document identity: ``doc_id = sha1(file_name + ext)`` — path-independent, so
re-uploading the same name+type overwrites the previous copy (single-version
semantics, no version column).

expand-then-contract(顺序不得违背, 见方案 2.3/2.6):
1. 父块全文先写 Mongo(不删旧) —— PG 可见的行一定能取到正文;
2. 只对 content_hash 变化的子块 embed(embedding 走 Ollama, 是最慢一环);
3. PG 单事务发布父子块 + 陈旧剪除;
4. 收缩: 删掉 PG 不再引用的父块 Mongo 正文。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Sequence

from app.bodies.store import ParentTextItem, get_body_store
from app.config import get_settings
from app.docs.normalize import content_hash, normalize_text
from app.docs.parsers import ParsedBlock, parse_blocks
from app.rag.embeddings import OllamaEmbedder
from app.rag.vectorstore import ChunkStore, ParentStore
from app.schemas import KnowledgeChunk, ParentBlock

logger = logging.getLogger(__name__)

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
# 单块正文长度上限(取代旧 ``text[:8192]`` 静默截断): 超限记 error + warnings 带出,
# 不再默默丢字。
CHUNK_MAX_CHARS = 20000


def compute_doc_id(name: str, ext: str) -> str:
    """Stable document identity from file name + extension (path-independent)."""
    return hashlib.sha1(f"{name}|{ext.lower()}".encode()).hexdigest()[:16]


def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Paragraph-aware sliding-window chunking (child-splitting fallback)."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}".strip()
        else:
            if buf:
                chunks.append(buf)
            while len(para) > size:
                chunks.append(para[:size])
                para = para[size - overlap :]
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


def _locate(needle: str, haystack: str, cursor: int) -> tuple[int, int]:
    """在 normalized_text 里以单调游标定位块文本 -> (start, end); miss 返回 (-1, -1)。

    一级: 直接 find; 二级: 去全部空白后定位再映射回原始下标(仅位置变换, 不改内容)。
    """
    idx = haystack.find(needle, cursor)
    if idx >= 0:
        return idx, idx + len(needle)
    stripped = re.sub(r"\s+", "", needle)
    if stripped:
        comp_positions = [i for i, ch in enumerate(haystack) if not ch.isspace()]
        compact = "".join(haystack[i] for i in comp_positions)
        c = compact.find(stripped)
        if c >= 0:
            return comp_positions[c], comp_positions[c + len(stripped) - 1] + 1
    return -1, -1


def build_structure(blocks: Sequence[ParsedBlock], normalized_text: str) -> list[dict]:
    """把父块 section/page/offset 组装成结构树(单向派生源, 不回写 Mongo)。

    ``block.section`` 按 "/" 或 " > " 拆层级; 无 heading 的块归入单根节点。
    **不为"好看"编造章节** —— 无层级时退化为扁平列表。
    """
    cursor = 0
    nodes: list[dict] = []
    for seq, block in enumerate(blocks):
        text = block.text.strip()
        if not text:
            continue
        start, end = _locate(text, normalized_text, cursor)
        if start >= 0:
            cursor = start + 1
        parts = [p for p in re.split(r"\s*(?:/|>)\s*", block.section) if p]
        nodes.append(
            {
                "node_id": f"p{seq:04d}",
                "title": parts[-1] if parts else "",
                "level": len(parts) or 1,
                "path": parts,
                "parent_type": getattr(block, "parent_type", "section") or "section",
                "page_no": block.page_no,
                "start_offset": start,
                "end_offset": end,
            }
        )
    return nodes


def build_parent_child(
    doc_id: str,
    title: str,
    source: str,
    modality: str,
    blocks: Sequence[ParsedBlock],
    normalized_text: str,
    parent_max: int | None = None,
    acl: dict[str, str] | None = None,
) -> tuple[list[ParentBlock], list[ParentTextItem], list[KnowledgeChunk], list[str]]:
    """把 section 块拆成父块(ParentBlock) + 父块正文(ParentTextItem) + 子块。

    返回 (parents, parent_items, children, warnings)。父块正文唯一副本进 Mongo,
    PG 父表只存定位; 子块 chunk_text 留 PG 且携带 content_hash 供增量 embed。
    """
    parent_max = parent_max or get_settings().parent_chunk_max
    acl = acl or {}
    parents: list[ParentBlock] = []
    parent_items: list[ParentTextItem] = []
    children: list[KnowledgeChunk] = []
    warnings: list[str] = []
    cursor = 0
    ord_ = 0
    for seq, block in enumerate(blocks):
        text = block.text.strip()
        if not text:
            continue
        if len(text) > CHUNK_MAX_CHARS:
            msg = f"block p{seq:04d} exceeds CHUNK_MAX_CHARS ({len(text)}>{CHUNK_MAX_CHARS}), kept in full"
            logger.error("%s (doc_id=%s)", msg, doc_id)
            warnings.append(msg)
        parent_id = f"{doc_id}-p{seq:04d}"
        start, end = _locate(text, normalized_text, cursor)
        if start >= 0:
            cursor = start + 1
        anchor = {
            "type": getattr(block, "parent_type", "section") or "section",
            "value": block.section,
            "node_id": f"p{seq:04d}",
            "locator": f"page:{block.page_no}" if block.page_no > 0 else "",
        }
        phash = content_hash(text)
        parents.append(
            ParentBlock(
                parent_id=parent_id, doc_id=doc_id, title=title, section=block.section,
                page_no=block.page_no,
                parent_type=getattr(block, "parent_type", "section") or "section",
                ord=seq, start_offset=start, end_offset=end, content_hash=phash,
                content=text, visibility=acl.get("visibility", "public"),
                owner_id=acl.get("owner_id", ""), dept_id=acl.get("dept_id", ""),
                allowed_roles=acl.get("allowed_roles", ""),
            )
        )
        parent_items.append(
            ParentTextItem(
                parent_id=parent_id, doc_id=doc_id, text=text, anchor=anchor,
                start_offset=start, end_offset=end, content_hash=phash,
            )
        )
        pieces = [text] if len(text) <= parent_max else split_chunks(text)
        for idx, piece in enumerate(pieces):
            children.append(
                KnowledgeChunk(
                    chunk_id=f"{parent_id}-c{idx:02d}", doc_id=doc_id, parent_id=parent_id,
                    chunk_index=idx, ord=ord_, title=title, content=piece, source=source,
                    modality=modality, section=block.section, page_no=block.page_no,  # type: ignore[arg-type]
                    content_hash=content_hash(piece),
                    visibility=acl.get("visibility", "public"),
                    owner_id=acl.get("owner_id", ""), dept_id=acl.get("dept_id", ""),
                    allowed_roles=acl.get("allowed_roles", ""),
                )
            )
            ord_ += 1
    return parents, parent_items, children, warnings


async def ingest_blocks(
    doc_id: str,
    filename: str,
    title: str,
    source: str,
    modality: str,
    blocks: Sequence[ParsedBlock],
    store: ChunkStore,
    embedder: OllamaEmbedder,
    acl: dict[str, str] | None = None,
    *,
    parent_store: ParentStore | None = None,
    bodies=None,
    normalized_text: str | None = None,
) -> int:
    """expand-then-contract 发布一篇文档的父子块; 返回写入的子块数。"""
    parent_store = parent_store or ParentStore()
    bodies = bodies or get_body_store()
    raw = "\n\n".join(b.text for b in blocks)
    normalized_text = normalized_text if normalized_text is not None else normalize_text(raw)
    parents, parent_items, children, _warn = build_parent_child(
        doc_id, title, source, modality, blocks, normalized_text, acl=acl
    )
    if not children:
        return 0

    # 1) expand: 父块全文先入 Mongo(不删旧), 保证 PG 可见行一定取得到文本。
    await bodies.save_parent_texts(parent_items)

    # 2) 只对 content_hash 变化的子块 embed; 未变化块从 PG 原样回填向量。
    old_hashes = await store.existing_hashes(doc_id)
    if get_settings().rag_incremental_ingest:
        changed = [c for c in children if old_hashes.get(c.chunk_id) != c.content_hash]
    else:
        changed = list(children)
    changed_ids = {c.chunk_id for c in changed}
    vectors_map: dict[str, list[float]] = {}
    if changed:
        new_vecs = await embedder.embed([c.content for c in changed])
        vectors_map.update({c.chunk_id: v for c, v in zip(changed, new_vecs)})
    unchanged_ids = [c.chunk_id for c in children if c.chunk_id not in changed_ids]
    if unchanged_ids:
        existing = await store.get_embeddings(unchanged_ids)
        vectors_map.update(existing)
    # 缺向量的块(增量下新出现或旧向量丢失)兜底 embed, 避免发布出"有元数据无向量"。
    need = [c for c in children if c.chunk_id not in vectors_map]
    if need:
        nv = await embedder.embed([c.content for c in need])
        vectors_map.update({c.chunk_id: v for c, v in zip(need, nv)})
        logger.info("incremental ingest: embedded %d/%d children for doc_id=%s",
                    len(set(changed_ids) | {c.chunk_id for c in need}), len(children), doc_id)
    vectors = [vectors_map[c.chunk_id] for c in children]

    # 3) PG 单事务: 父块 + 子块 upsert + 陈旧剪除。
    written = await store.publish_parent_child(parent_store, doc_id, parents, children, vectors)

    # 4) contract: 清掉 PG 不再引用的父块 Mongo 正文。
    await bodies.delete_stale_parents(doc_id, {p.parent_id for p in parents})
    return written


async def ingest_file(
    path: Path, store: ChunkStore, embedder: OllamaEmbedder, bodies=None
) -> int:
    """Ingest a single file; returns number of child chunks written."""
    doc_id = compute_doc_id(path.stem, path.suffix)
    bodies = bodies or get_body_store()
    modality, blocks = await parse_blocks(path)
    raw = "\n\n".join(b.text for b in blocks)
    normalized = normalize_text(raw)
    structure = build_structure(blocks, normalized)
    await bodies.save_doc_body(
        doc_id, raw=raw, normalized=normalized, structure=structure,
        meta={"name": path.name, "source": str(path)},
    )
    return await ingest_blocks(
        doc_id, path.name, path.stem, str(path), modality, blocks, store, embedder,
        bodies=bodies, normalized_text=normalized,
    )


async def ingest_directory(
    dir_path: Path, store: ChunkStore, embedder: OllamaEmbedder
) -> dict[str, int]:
    """Ingest every supported file under a directory (recursive)."""
    report: dict[str, int] = {}
    for path in sorted(dir_path.rglob("*")):
        if not path.is_file():
            continue
        try:
            n = await ingest_file(path, store, embedder)
        except ValueError:
            continue  # unsupported extension
        if n:
            report[str(path)] = n
    return report


async def collect_corpus(store: ChunkStore, limit: int = 100000) -> list[KnowledgeChunk]:
    """Fetch the child-chunk corpus (for BM25 rebuild)."""
    return await store.iter_child_chunks(limit)
