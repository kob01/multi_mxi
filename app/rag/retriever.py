"""Hybrid retrieval: dense (pgvector 窄列) + sparse (ES BM25) -> RRF -> 回表 -> rerank.

Retrieval operates on child chunks (``doc_chunks``) only — the table no longer
holds parent rows, so there is no ``is_parent`` filter. TopK 只取 NARROW_COLUMNS
(不含 chunk_text/embedding), 命中后在 rerank **之前**按 chunk_id 主键批量回表补正文;
hit children are then assembled back into their parent section blocks (正文从
MongoDB ``parent_texts`` 取回, 不可得时降级为子块文本) so the LLM receives complete
section context instead of truncated fragments.

Threshold policy: neither retrieval channel applies a score cutoff — the
sparse (BM25) channel purely recalls candidates and RRF only fuses ranks.
The ONLY relevance cutoff lives at the rerank stage
(``settings.retrieval_score_threshold``), which scores candidates with a real
cross-encoder (TEI ``/rerank``, 0~1 relevance) so the threshold has a stable
meaning; chunks scoring below it are dropped before context building, and an
empty result means "no relevant document" so the upper layer can
rewrite-and-retry or refuse to answer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.config import get_settings
from app.bodies.store import get_body_store
from app.rag.bm25 import ElasticBM25Retriever, get_es_bm25
from app.rag.embeddings import OllamaEmbedder
from app.rag.reranker import get_reranker
from app.rag.vectorstore import ChunkStore, ParentStore
from app.schemas import KnowledgeChunk
from app.security.acl import Principal

logger = logging.getLogger(__name__)

RRF_K = 60  # Reciprocal Rank Fusion constant


def _rrf_fuse(
    dense: Sequence[KnowledgeChunk], sparse: Sequence[KnowledgeChunk], top_k: int
) -> list[KnowledgeChunk]:
    """Merge two ranked lists with Reciprocal Rank Fusion."""
    fused: dict[str, tuple[KnowledgeChunk, float]] = {}
    for rank, chunk in enumerate(dense):
        score = 1.0 / (RRF_K + rank + 1)
        if chunk.chunk_id in fused:
            c, s = fused[chunk.chunk_id]
            fused[chunk.chunk_id] = (c, s + score)
        else:
            fused[chunk.chunk_id] = (chunk, score)
    for rank, chunk in enumerate(sparse):
        score = 1.0 / (RRF_K + rank + 1)
        if chunk.chunk_id in fused:
            c, s = fused[chunk.chunk_id]
            fused[chunk.chunk_id] = (c, s + score)
        else:
            fused[chunk.chunk_id] = (chunk, score)
    ranked = sorted(fused.values(), key=lambda x: x[1], reverse=True)[:top_k]
    return [chunk.model_copy(update={"score": score}) for chunk, score in ranked]


class HybridRetriever:
    """Enterprise knowledge retriever used by the Assistant's KB path."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self.embedder = OllamaEmbedder()
        self.store = ChunkStore()
        self.parent_store = ParentStore()
        self.bm25: ElasticBM25Retriever = get_es_bm25()
        self.reranker = get_reranker()
        self.bodies = get_body_store()

    async def rebuild_bm25(self) -> None:
        """Rebuild the ES BM25 index from child chunks stored in PostgreSQL.

        PostgreSQL stays the source of truth for the corpus; 文本全在 PG
        ``doc_chunks``, 不依赖 Mongo; 流式逐批索引(语料上量后不一次性加载)。
        """
        await self.bm25.rebuild(self.store.iter_chunk_corpus())

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        top_n: int | None = None,
        principal: Principal | None = None,
    ) -> tuple[list[KnowledgeChunk], str]:
        """Full hybrid pipeline for one query (child-chunk granularity).

        Args:
            query: User query text.
            top_k: Candidates per channel before fusion (default: settings).
            top_n: Final chunks after rerank (default: settings).
            principal: Caller identity for the document-level ACL trim.
                None means no filtering (trusted internal callers only).

        Returns:
            ``(chunks, score_mode)`` where ``score_mode`` is ``"rerank"`` or
            ``"rrf"`` — which score scale ``chunk.score`` reflects (rerank 为
            TEI cross-encoder 的 0~1 相关性, RRF 为融合秩分)。

        Threshold policy: retrieval channels (dense ANN / ES BM25) and RRF
        fusion apply NO relevance cutoff. Only the rerank stage filters by
        ``settings.retrieval_score_threshold``; when rerank is disabled or
        fails, the RRF fallback returns candidates unfiltered (score_mode
        ``"rrf"``) — degrading to "answer with candidates" instead of
        silently dropping everything on a scale mismatch.
        """
        top_k = top_k or self._settings.rag_top_k
        top_n = top_n or self._settings.rerank_top_n

        # 权限前置裁剪: 向量通道用 SQL Metadata Filter (build_sql_filter),
        # ES 稀疏通道用等价的 bool filter —— 两条通道在 TopK 之前语义严格一致。
        query_vec = await self.embedder.embed_query(query)
        dense_hits = await self.store.search(query_vec, top_k, principal=principal)
        sparse_hits = await self.bm25.search(query, top_k, principal=principal)
        fused = _rrf_fuse(dense_hits, sparse_hits, top_k)
        # 关键不变量 2: 正文必须在 rerank **之前**就位(一次 chunk_id 主键批量回表)。
        # 放在 rerank_enabled 判断之前, 保证 RRF 降级路径同样有正文(format_context 需要);
        # 若回表迟于 rerank, rerank 拿到空串 -> 被阈值裁空 -> 静默变"未找到相关文档"。
        fused = await self.store.attach_texts(fused)

        if not self._settings.rerank_enabled:
            return fused[:top_n], "rrf"
        try:
            reranked = await self.reranker.rerank(query, fused, top_n)
        except Exception as exc:  # graceful degradation to RRF order
            logger.warning("rerank failed (TEI %s), fallback to RRF fusion order: %s", self.reranker.base_url, exc)
            return fused[:top_n], "rrf"
        # 置信度裁剪(全链路唯一阈值): 只保留 rerank 判定真正相关的块,
        # 低分噪声不进入 Context Builder; 结果为空 = 未检索到相关文档,
        # 由上层 judge 决定改写重检或明确拒答。
        threshold = self._settings.retrieval_score_threshold
        if threshold > 0:
            kept = [c for c in reranked if c.score >= threshold]
            if len(kept) != len(reranked):
                logger.info(
                    "rerank confidence filter: %d -> %d chunks (threshold=%.3f)",
                    len(reranked), len(kept), threshold,
                )
            return kept, "rerank"
        return reranked, "rerank"

    async def assemble_parents(self, chunks: Sequence[KnowledgeChunk]) -> list[KnowledgeChunk]:
        """把命中的子块组装回完整父块章节(父块正文从 Mongo parent_texts 取回)。

        保留 rerank 顺序: 每个不同父块只出现一次, 位于其最高分子块的位置。
        - 一次 PG 窄列取父块结构(get_blocks) + 一次 Mongo $in 取正文(get_parent_texts);
        - Mongo 不可用/缺键 -> 退回该父块下得分最高子块的 chunk_text(不拒答, 记 warning)。
        """
        best_child: dict[str, KnowledgeChunk] = {}
        order: list[str] = []
        for c in chunks:
            pid = c.parent_id or c.chunk_id
            if pid not in best_child:
                best_child[pid] = c
                order.append(pid)
        blocks = await self.parent_store.get_blocks(order)
        texts = await self.bodies.get_parent_texts(order)
        assembled: list[KnowledgeChunk] = []
        for pid in order:
            block = blocks.get(pid)
            child = best_child[pid]
            text = texts.get(pid, "")
            if not text:
                # 降级: 父块全文不可得, 退回得分最高的子块正文(答案变碎但不阻断)。
                logger.warning("parent context degraded to child text: %s", pid)
                text = child.content
            if block is not None:
                assembled.append(
                    KnowledgeChunk(
                        chunk_id=pid, doc_id=block.doc_id, parent_id="", is_parent=True,
                        title=block.title, section=block.section, page_no=block.page_no,
                        modality=child.modality, source=child.source, content=text,
                        parent_type=block.parent_type, content_hash=block.content_hash,
                        start_offset=block.start_offset, end_offset=block.end_offset,
                        visibility=block.visibility, owner_id=block.owner_id,
                        dept_id=block.dept_id, allowed_roles=block.allowed_roles,
                        score=child.score,
                    )
                )
            else:
                assembled.append(child.model_copy(update={"content": text}))
        return assembled

    def format_context(
        self,
        chunks: Sequence[KnowledgeChunk],
        meta_map: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """Render chunks as grounded context for the LLM prompt.

        Each block cites title / section / page and (when document metadata is
        available) the document tags.
        """
        blocks = []
        for i, c in enumerate(chunks, 1):
            cite = f"《{c.title}》"
            if c.section:
                cite += f" 章节:{c.section}"
            if c.page_no > 0:
                cite += f" 第{c.page_no}页"
            meta = (meta_map or {}).get(c.doc_id) or {}
            if meta.get("tags"):
                cite += f" (标签: {', '.join(meta['tags'])})"
            blocks.append(f"[资料{i}] {cite}\n{c.content}")
        return "\n\n".join(blocks)
