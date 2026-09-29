"""Elasticsearch-based BM25 sparse retriever (lexical channel of the hybrid search).

The corpus lives in an ES index (one doc per child chunk). Chinese text is
pre-tokenized with jieba at index/query time and stored space-joined, so the
index needs no IK/analysis plugin: a plain whitespace analyzer yields exactly
the same token stream on both sides.

BM25 ranking runs with NO score threshold — the sparse channel only recalls
candidates; the relevance cutoff is applied exclusively at the rerank stage
(see HybridRetriever). ES unavailable degrades gracefully to an empty channel
(the dense channel still serves), never to a chat failure.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence

import jieba
from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk

from app.config import get_settings
from app.schemas import DocVisibility, KnowledgeChunk
from app.security.acl import Principal

logger = logging.getLogger(__name__)


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text for BM25."""
    text = text.lower()
    tokens = jieba.lcut(text)
    return [t for t in tokens if re.search(r"[a-z0-9一-鿿]", t)]


def _token_field(text: str) -> str:
    """Space-join tokens so ES's whitespace analyzer reproduces them exactly."""
    return " ".join(tokenize(text))


def _acl_filter(principal: Principal | None) -> list[dict]:
    """ES bool-filter mirroring app.rag.vectorstore.build_sql_filter.

    Pre-trims unauthorized documents before TopK — semantically identical to
    the vector channel's pgvector metadata filter, so the two channels never
    disagree on what a principal may read.
    """
    if principal is None or principal.is_admin:
        return []
    clauses: list[dict] = [{"term": {"visibility": DocVisibility.PUBLIC.value}}]
    if principal.user_id:
        clauses.append(
            {
                "bool": {
                    "filter": [
                        {"term": {"visibility": DocVisibility.PRIVATE.value}},
                        {"term": {"owner_id": principal.user_id}},
                    ]
                }
            }
        )
    if principal.department:
        clauses.append(
            {
                "bool": {
                    "filter": [
                        {"term": {"visibility": DocVisibility.DEPT.value}},
                        {"term": {"dept_id": principal.department}},
                    ]
                }
            }
        )
    clauses.append(
        {
            "bool": {
                "filter": [
                    {"term": {"visibility": DocVisibility.ROLE.value}},
                    {"term": {"allowed_roles": principal.role.value}},
                ]
            }
        }
    )
    return [{"bool": {"should": clauses, "minimum_should_match": 1}}]


def _doc_source(chunk: KnowledgeChunk) -> dict:
    """Serialise one child chunk into its ES source document。

    **不存 ``content``**: 正文唯一副本在 PG ``doc_chunks.chunk_text``(子块)与
    Mongo ``parent_texts``(父块); ES 只存派生的 ``content_tokens``(去掉第三副本)。
    """
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "title": chunk.title[:512],
        # 检索文本 = 标题分词 + 正文分词(正文不入库, 仅用于生成分词)。
        "content_tokens": _token_field(f"{chunk.title} {chunk.content}"),
        "source": chunk.source[:512],
        "modality": chunk.modality,
        "parent_id": chunk.parent_id,
        "page_no": chunk.page_no,
        "section": chunk.section[:256],
        "visibility": chunk.visibility or "public",
        "owner_id": chunk.owner_id,
        "dept_id": chunk.dept_id,
        # keyword 数组: 逗号包裹存储 (",hr,admin,") 拆成精确角色项。
        "allowed_roles": [
            r for r in (chunk.allowed_roles or "").strip(",").split(",") if r
        ],
    }


class ElasticBM25Retriever:
    """BM25 lexical channel backed by Elasticsearch.

    高并发下的三个要点(都不是可选的):
    - 索引存在性只探一次: 旧实现每次 ``search`` 都跑一次 ``indices.exists``,
      等于每轮对话白送一个 ES 往返(几百 QPS 时它比查询本身还贵)。
    - 检索超时是秒级而非 30s: ES 半死时 30s 挂钟会把连接和内存拖爆,
      这里宁可快速失败走"稀疏通道为空"的降级。
    - 稀疏通道有并发上限: 超出部分在本地排队而不是把 ES 压到雪崩。
    """

    def __init__(self, url: str | None = None, index: str | None = None) -> None:
        settings = get_settings()
        self.index = index or settings.es_index
        self._search_timeout = max(0.5, float(settings.es_search_timeout))
        self._rebuild_timeout = max(5, int(settings.es_rebuild_timeout))
        self._index_exists = False
        self._ensure_lock: asyncio.Lock | None = None
        self._search_gate = asyncio.Semaphore(max(1, settings.es_search_concurrency))
        # 探询失败只告警一次: ES 长期不可用时不要每轮刷一条 warning 把日志冲掉。
        self._warned = False
        # 两个客户端分开: 检索要快失败, 重建要允许慢(两者共用一个超时会两头不对)。
        self._client = AsyncElasticsearch(
            url or settings.es_url,
            request_timeout=self._search_timeout,
            verify_certs=False,
            max_retries=0,
            retry_on_timeout=False,
        )
        self._bulk_client = AsyncElasticsearch(
            url or settings.es_url,
            request_timeout=self._rebuild_timeout,
            verify_certs=False,
        )

    async def aclose(self) -> None:
        """释放两个连接池(供 lifespan 关停调用)。"""
        for client in (self._client, self._bulk_client):
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("closing elasticsearch client failed: %s", exc)

    async def ensure_index(self) -> None:
        """Create the index + mapping on first use (幂等且只探一次)。

        重建路径(:meth:`rebuild`)会显式重置标记, 因为 ``delete`` + ``create`` 后
        存在性确实变了; 除此之外只有首次真正发一次 ``exists`` 请求。
        """
        if self._index_exists:
            return
        if self._ensure_lock is None:
            self._ensure_lock = asyncio.Lock()
        async with self._ensure_lock:
            if self._index_exists:
                return
            if await self._client.indices.exists(index=self.index):
                self._index_exists = True
                return
            await self._create_index()
            self._index_exists = True

    async def _create_index(self) -> None:
        """只建索引: 存在性判断在 :meth:`ensure_index` 里做(幂等不靠这里抢)。

        多进程/多实例并发同时建会报 resource_already_exists: 语义上就是成功,
        调用方(search/rebuild 已在自己的降级分支里兜异常)不需要区分这个。
        """
        await self._client.indices.create(
            index=self.index,
            mappings={
                "properties": {
                    "chunk_id": {"type": "keyword"},
                    "doc_id": {"type": "keyword"},
                    "title": {"type": "keyword"},
                    # 预分词文本: whitespace + lowercase 复现 jieba 词元, 无需分词插件。
                    # (不再存 content: 正文唯一副本在 PG/Mongo, ES 只存派生倒排)
                    "content_tokens": {"type": "text", "analyzer": "chunk_tokens"},
                    "source": {"type": "keyword"},
                    "modality": {"type": "keyword"},
                    "parent_id": {"type": "keyword"},
                    "page_no": {"type": "integer"},
                    "section": {"type": "keyword"},
                    "visibility": {"type": "keyword"},
                    "owner_id": {"type": "keyword"},
                    "dept_id": {"type": "keyword"},
                    "allowed_roles": {"type": "keyword"},
                }
            },
            settings={
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "analysis": {
                    "analyzer": {
                        "chunk_tokens": {
                            "type": "custom",
                            "tokenizer": "whitespace",
                            "filter": ["lowercase"],
                        }
                    }
                },
            },
        )

    async def rebuild(self, chunks_or_iter) -> None:
        """Drop + rebuild the whole index from a list or an async iterator of batches.

        流式重建时批间不 refresh(最后一批才 refresh), 避免大语料重建的 refresh 抖动。
        语料源 = PG ``doc_chunks``(title + chunk_text), 与向量同一张表, 不依赖 Mongo。

        走重建专用的宽松超时客户端, 并重置存在性标记(delete 后确实不存在了)。
        """
        self._index_exists = False
        await self._bulk_client.indices.delete(index=self.index, ignore=[404])
        await self.ensure_index()
        if hasattr(chunks_or_iter, "__aiter__"):
            async for batch in chunks_or_iter:
                await self.index_chunks(batch, refresh=False)
            await self._bulk_client.indices.refresh(index=self.index)
        else:
            await self.index_chunks(list(chunks_or_iter))

    async def index_chunks(self, chunks: Sequence[KnowledgeChunk], refresh: bool = True) -> None:
        """Upsert child chunks (bulk); refresh=False 供流式分批末尾统一刷新。"""
        if not chunks:
            return
        await self.ensure_index()
        actions = [
            {"_index": self.index, "_id": c.chunk_id, "_source": _doc_source(c)}
            for c in chunks
        ]
        await async_bulk(self._bulk_client, actions, refresh=refresh)

    async def search(
        self, query: str, top_k: int, principal: Principal | None = None
    ) -> list[KnowledgeChunk]:
        """Top-k child chunks ranked by BM25.

        不设任何分数阈值 —— 稀疏通道只负责召回, 相关性裁剪统一在 rerank
        阶段完成; 这里即使低分也按排名返回, 供 RRF 融合。

        并发上限内的查询才发到 ES, 超出部分在本地排队; 任何失败(含超时)都降级为
        空通道由稠密通道兜底, 且同类失败只告警一次(否则 ES 挂了会被刷成日志风暴)。
        """
        tokens = tokenize(query)
        if not tokens:
            return []
        try:
            async with self._search_gate:
                await self.ensure_index()
                bool_query: dict = {"must": [{"match": {"content_tokens": " ".join(tokens)}}]}
                if acl_filter := _acl_filter(principal):
                    bool_query["filter"] = acl_filter
                resp = await self._client.search(
                    index=self.index,
                    size=top_k,
                    query={"bool": bool_query},
                    source=[
                        "chunk_id", "doc_id", "title", "source", "modality",
                        "parent_id", "page_no", "section",
                        "visibility", "owner_id", "dept_id", "allowed_roles",
                    ],
                )
        except Exception as exc:
            # ES 故障不阻断对话: 稀疏通道降级为空, 由稠密通道兜底。
            if not self._warned:
                self._warned = True
                logger.warning(
                    "elasticsearch BM25 search failed, sparse channel empty "
                    "(后续同类失败不再刷屏): %s", exc
                )
            else:
                logger.debug("elasticsearch BM25 search failed: %s", exc)
            return []
        self._warned = False
        chunks: list[KnowledgeChunk] = []
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            roles = src.get("allowed_roles") or []
            chunks.append(
                KnowledgeChunk(
                    chunk_id=src["chunk_id"],
                    doc_id=src["doc_id"],
                    title=src.get("title", ""),
                    content="",  # 正文不存 ES, 后续由 chunk_id 主键回表补
                    source=src.get("source", ""),
                    modality=src.get("modality", "text"),
                    parent_id=src.get("parent_id", ""),
                    page_no=int(src.get("page_no", -1)),
                    section=src.get("section", ""),
                    visibility=src.get("visibility") or "public",
                    owner_id=src.get("owner_id", ""),
                    dept_id=src.get("dept_id", ""),
                    allowed_roles=("," + ",".join(roles) + ",") if roles else "",
                    score=float(hit.get("_score") or 0.0),
                )
            )
        return chunks


_retriever: ElasticBM25Retriever | None = None


def get_es_bm25() -> ElasticBM25Retriever:
    """Process-wide singleton of the ES sparse channel."""
    global _retriever
    if _retriever is None:
        _retriever = ElasticBM25Retriever()
    return _retriever


async def close_es_client() -> None:
    """释放单例里的两个连接池(挂到 lifespan 关停, 与 close_redis/close_mongo 同风格)。"""
    global _retriever
    if _retriever is not None:
        await _retriever.aclose()
        _retriever = None
