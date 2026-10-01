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


class _Locator:
    """把块文本定位回 normalized_text 的一次性定位器(游标单调, 空白映射只算一遍)。

    为什么不是一个无状态函数: 旧写法 ``_locate(needle, haystack, cursor)`` 在直接
    find 未中时, 对**整篇文档**重建一次空白位置表与压缩串。而 ``normalize_text(raw)``
    与 ``block.text.strip()`` 并不保证逐字节相等, 所以退化路径在真实文档上是常态而不
    是例外: 一千个块 × 百万字符 = 每入库一篇都要做上千次全串扫描+全串压缩(请求级
    挂死)。压缩映射改为懒建且全篇只建一次。

    游标也不得丢: 旧写法的 ``compact.find(stripped)`` 不带游标, 内容重复的块恒返回
    首次出现位置, start/end 会倒退或重复, 结构树与父块定位随之失真。
    """

    __slots__ = ("_raw", "_compact", "_positions", "_built", "_raw_cursor", "_compact_cursor")

    def __init__(self, haystack: str) -> None:
        self._raw = haystack
        self._compact = ""
        self._positions: list[int] = []
        self._built = False
        self._raw_cursor = 0
        self._compact_cursor = 0

    def _ensure_compact(self) -> None:
        """去空白压缩串 + 下标映射: 全篇只算一次(仅退化路径需要)。"""
        if self._built:
            return
        positions = [i for i, ch in enumerate(self._raw) if not ch.isspace()]
        self._positions = positions
        self._compact = "".join(self._raw[i] for i in positions)
        self._built = True

    def locate(self, needle: str) -> tuple[int, int]:
        """返回 ``(start, end)``; 定位不到返回 ``(-1, -1)``(调用方计入 warnings)。"""
        if not needle:
            return -1, -1
        idx = self._raw.find(needle, self._raw_cursor)
        if idx >= 0:
            self._raw_cursor = idx + 1
            return idx, idx + len(needle)
        stripped = re.sub(r"\s+", "", needle)
        if not stripped:
            return -1, -1
        self._ensure_compact()
        c = self._compact.find(stripped, self._compact_cursor)
        if c < 0:
            # 游标后没有不代表全篇没有: 块顺序与文本顺序不一致时退回全串重找,
            # 但不能因此把游标往回推(否则后面每个块都从同一位置重扫)。
            c = self._compact.find(stripped)
        if c < 0:
            return -1, -1
        start = self._positions[c]
        end = self._positions[c + len(stripped) - 1] + 1
        self._compact_cursor = c + 1
        if start + 1 > self._raw_cursor:
            self._raw_cursor = start + 1
        return start, end


def build_structure(blocks: Sequence[ParsedBlock], normalized_text: str) -> list[dict]:
    """把父块 section/page/offset 组装成结构树(单向派生源, 不回写 Mongo)。

    ``block.section`` 按 "/" 或 " > " 拆层级; 无 heading 的块归入单根节点。
    **不为"好看"编造章节** —— 无层级时退化为扁平列表。
    """
    locator = _Locator(normalized_text)
    nodes: list[dict] = []
    for seq, block in enumerate(blocks):
        text = block.text.strip()
        if not text:
            continue
        start, end = locator.locate(text)
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
    locator = _Locator(normalized_text)
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
        start, end = locator.locate(text)
        if start < 0:
            # 静默写 -1 偏移会让"父块存在但永远定位不到正文"这类问题事后查不到,
            # 必须回到入库报告里。
            msg = f"block p{seq:04d} 在归一化正文中定位失败, offset 置 -1"
            logger.warning("%s (doc_id=%s)", msg, doc_id)
            warnings.append(msg)
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
    dir_path: Path,
    store: ChunkStore,
    embedder: OllamaEmbedder,
    failures: list[tuple[str, str]] | None = None,
) -> dict[str, int]:
    """Ingest every supported file under a directory (recursive).

    失败必须留痕: 旧写法只 ``except ValueError: continue`` 且不记日志 —— 损坏的 docx /
    编码异常常常也以 ValueError 从 ``parse_blocks`` 抛出, 于是一整批文件"入库数为 0"
    却查不到原因; 而非 ValueError 的异常(OSError/解析库内部错误)则会直接中断整个目录。
    现在逐文件捕获所有异常并记 WARNING, 同时把原因回给调用方(``failures``)。
    """
    report: dict[str, int] = {}
    for path in sorted(dir_path.rglob("*")):
        if not path.is_file():
            continue
        try:
            n = await ingest_file(path, store, embedder)
        except Exception as exc:  # noqa: BLE001 - 单个文件坏不能拖垮整批入库
            logger.warning("ingest failed %s: %s: %s", path, exc.__class__.__name__, exc)
            if failures is not None:
                failures.append((str(path), f"{exc.__class__.__name__}: {str(exc)[:160]}"))
            continue
        if n:
            report[str(path)] = n
    return report


async def collect_corpus(store: ChunkStore, limit: int = 100000) -> list[KnowledgeChunk]:
    """Fetch the child-chunk corpus (for BM25 rebuild)."""
    return await store.iter_child_chunks(limit)
