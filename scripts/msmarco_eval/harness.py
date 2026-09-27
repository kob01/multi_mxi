"""Isolated evaluation harness around the production ``HybridRetriever``.

The whole point is to measure the *real* pipeline (narrow-column dense bge-m3 via
PostgreSQL/pgvector + sparse BM25 via Elasticsearch -> RRF fusion -> chunk_text
主键回表 -> bge-reranker, 父块组装走 MongoDB) without ever touching production
data. 本方案新增的两根轴(父子双表 + 父块正文)也一并隔离, 共三条轴:

* **Vector / chunk store** -> a dedicated PostgreSQL database
  (``mxi_msmarco_eval``) holding its own ``doc_parents`` / ``doc_chunks`` tables
  (pgvector + HNSW). A separate database (not just a schema) removes any
  search_path ambiguity, so an errant query can never read or write production rows.
* **Body store (父块全文)** -> a dedicated MongoDB database
  (``mxi_msmarco_eval``), via ``install_eval_mongo`` overriding the BodyStore
  singleton's db handle.
* **Lexical store** -> a dedicated Elasticsearch index.

The production code is reused verbatim: ``ChunkStore`` / ``ParentStore`` resolve
their session factory lazily from ``app.db.session``, so we provision the eval
database, install an eval-scoped async engine + eval Mongo db, and drive the
*unmodified* :class:`HybridRetriever` against it. The retriever is built via
``__new__`` (bypassing ``__init__``, which would otherwise bind the production
ES singleton).

MS MARCO passages are each indexed as one parent + one child (passages 无层级 ->
单父块包单于块), 与生产链路同一形状(子块走窄列 ANN + chunk_text, 父块走 Mongo),
metric comparison happens at the passage (``doc_id``) level, so ranking is
functionally identical to the production path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.bodies.client import get_mongo_client
from app.bodies.store import ParentTextItem, get_body_store
from app.db import session as db_session
from app.db.models import DocChunkRow, DocParentRow
from app.docs.normalize import content_hash
from app.rag.bm25 import ElasticBM25Retriever
from app.rag.embeddings import OllamaEmbedder
from app.rag.reranker import get_reranker
from app.rag.retriever import HybridRetriever
from app.rag.vectorstore import ChunkStore, ParentStore
from app.schemas import KnowledgeChunk, ParentBlock

logger = logging.getLogger(__name__)

DEFAULT_EVAL_DB = "mxi_msmarco_eval"
DEFAULT_EVAL_ES_INDEX = "msmarco_eval_chunks"
DEFAULT_EVAL_MONGO_DB = "mxi_msmarco_eval"


def corpus_to_chunks(
    corpus: dict[str, dict],
) -> tuple[list[KnowledgeChunk], list[ParentBlock], list[ParentTextItem]]:
    """每条 passage 生成一个单父块 + 一个子块(passages 无层级)。

    offset 由本条 normalized 文本现算(整段即父块), 与生产链路同一形状: 子块带
    content_hash/parent_id, 父块走 Mongo parent_texts + PG doc_parents(不存正文)。
    """
    from app.docs.normalize import normalize_text

    chunks: list[KnowledgeChunk] = []
    parents: list[ParentBlock] = []
    items: list[ParentTextItem] = []
    for doc_id, passage in corpus.items():
        text_ = (passage.get("text") or "").strip()
        if not text_:
            continue
        parent_id = f"marco-{doc_id}-p0000"[:80]
        normalized = normalize_text(text_)
        parents.append(
            ParentBlock(
                parent_id=parent_id, doc_id=str(doc_id)[:64],
                title=(passage.get("title") or "")[:512], parent_type="section",
                ord=0, start_offset=0, end_offset=len(normalized),
                content_hash=content_hash(text_), content=text_, visibility="public",
            )
        )
        items.append(
            ParentTextItem(
                parent_id=parent_id, doc_id=str(doc_id), text=text_, anchor={},
                start_offset=0, end_offset=len(normalized), content_hash=content_hash(text_),
            )
        )
        chunks.append(
            KnowledgeChunk(
                chunk_id=f"{parent_id}-c00"[:80], doc_id=str(doc_id)[:64],
                parent_id=parent_id, chunk_index=0, ord=0,
                title=(passage.get("title") or "")[:512], content=text_,
                source="msmarco", modality="text", is_parent=False, page_no=-1, section="",
                content_hash=content_hash(text_), visibility="public",
            )
        )
    return chunks, parents, items


def _database_url_with(database: str) -> str:
    """Resolve the async DSN for ``database`` reusing the app's PG settings."""
    settings = get_settings()
    if settings.database_url:
        return make_url(settings.database_url).set(database=database).render_as_string(
            hide_password=False
        )
    dsn = (
        f"postgresql+asyncpg://{settings.pg_user}:{quote_plus(settings.pg_password)}"
        f"@{settings.pg_host}:{settings.pg_port}/{database}"
    )
    return dsn


def _connect_args() -> dict:
    """Mirror app.db.session connect args (explicit ssl mode + timeout)."""
    settings = get_settings()
    if not settings.pg_password and not settings.database_url:
        raise RuntimeError("缺少 PostgreSQL 密码：请设置环境变量 PG_PASSWORD 后再运行评测")
    return {
        "timeout": settings.pg_connect_timeout,
        "server_settings": {"application_name": "mxi-msmarco-eval"},
        "ssl": settings.pg_sslmode,
    }


async def provision_eval_database(database: str = DEFAULT_EVAL_DB) -> AsyncEngine:
    """Create the eval database (if absent) + its knowledge_chunks table.

    Runs ``CREATE DATABASE`` on an autocommit maintenance connection (the app
    role has CREATEDB), then opens the eval engine and creates the pgvector
    extension + the ORM table with its HNSW index (``checkfirst`` -> idempotent).
    """
    settings = get_settings()
    maint = create_async_engine(
        _database_url_with(settings.pg_database),
        connect_args=_connect_args(),
        execution_options={"isolation_level": "AUTOCOMMIT"},
    )
    try:
        async with maint.connect() as conn:
            exists = (
                await conn.execute(
                    text("select 1 from pg_database where datname = :db"), {"db": database}
                )
            ).scalar()
            if not exists:
                await conn.execute(
                    text(f'create database "{database}" owner "{settings.pg_user}"')
                )
                logger.info("[eval] created database %s", database)
    finally:
        await maint.dispose()

    engine = create_async_engine(_database_url_with(database), connect_args=_connect_args())
    async with engine.begin() as conn:
        await conn.execute(text("create extension if not exists vector"))
        await conn.run_sync(DocParentRow.__table__.create, checkfirst=True)
        await conn.run_sync(DocChunkRow.__table__.create, checkfirst=True)
    return engine


def install_eval_mongo(database: str = DEFAULT_EVAL_MONGO_DB) -> None:
    """将 BodyStore 单例的 db 指向 eval 库(与 install_eval_engine 同构的受控 hack)。

    Mongo 侧不需要建库(惰性), 但父块正文必须落在隔离的 eval 数据库, 否则评测会
    往生产 ``mxi`` 库写 parent_texts。``_db`` 是 BodyStore 的惰性属性, 直接覆写即可。
    """
    store = get_body_store()
    store._db = get_mongo_client()[database]  # noqa: SLF001 - eval owns this process


def install_eval_engine(engine: AsyncEngine) -> None:
    """Point the process-wide session factory at the eval engine.

    ``PgVectorStore`` calls ``app.db.session.get_session_factory()`` lazily, so
    swapping these module globals routes every store operation to the eval
    database for the lifetime of this process.
    """
    db_session._engine = engine  # noqa: SLF001 - eval owns this process
    db_session._session_factory = async_sessionmaker(engine, expire_on_commit=False)  # noqa: SLF001


def make_retriever(es_index: str = DEFAULT_EVAL_ES_INDEX) -> HybridRetriever:
    """Build an evaluation-scoped retriever reusing the production pipeline."""
    retriever = object.__new__(HybridRetriever)  # skip __init__ (prod ES singleton)
    retriever._settings = get_settings()  # noqa: SLF001
    retriever.embedder = OllamaEmbedder()
    retriever.reranker = get_reranker()
    retriever.store = ChunkStore()  # binds to the installed eval engine
    retriever.parent_store = ParentStore()
    retriever.bm25 = ElasticBM25Retriever(index=es_index)
    retriever.bodies = get_body_store()  # 已被 install_eval_mongo 接管
    return retriever


async def rerank_available() -> bool:
    """Best-effort health check for the cross-encoder reranker (TEI ``/rerank``).

    The rerank stage now runs as a separate TEI container; if it is not up, still
    loading weights, or serving a model without a single-class sequence head (424),
    every query would pay the request timeout before falling back. Probing once lets
    the eval skip that loop and report the mode it actually measured. Never raises.
    """
    return await get_reranker().probe()


async def _truncate_chunks(store: ChunkStore) -> None:
    """清空上一轮残留(新两张表 + Mongo parent_texts)。"""
    async with store._sessions()() as session:  # noqa: SLF001
        await session.execute(text("truncate table doc_chunks"))
        await session.execute(text("truncate table doc_parents"))
        await session.commit()
    db = get_body_store()._db  # noqa: SLF001 - eval 已接管单例
    if db is not None:
        await db["parent_texts"].delete_many({})


async def ingest_corpus(
    retriever: HybridRetriever,
    corpus: dict[str, dict],
) -> dict[str, Any]:
    """Embed + publish 父子块到 pg 双表 + Mongo, 并重建 ES BM25 索引。"""
    chunks, parents, items = corpus_to_chunks(corpus)
    store: ChunkStore = retriever.store  # type: ignore[assignment]
    await _truncate_chunks(store)

    t0 = time.perf_counter()
    vectors = await retriever.embedder.embed([c.content for c in chunks])
    embed_dt = time.perf_counter() - t0

    t1 = time.perf_counter()
    # 父块正文先入 Mongo(eval 库), 再单事务发布父子块(与生产 expand-then-contract 同构)。
    await retriever.bodies.save_parent_texts(items)
    # corpus_to_chunks 中每个 passage 的 parent/child/item 一一对齐, 逐个发布(1 父 1 子)。
    written = 0
    for p, c, v in zip(parents, chunks, vectors):
        written += await store.publish_parent_child(
            retriever.parent_store, p.doc_id, [p], [c], [v]
        )
    upsert_dt = time.perf_counter() - t1

    t2 = time.perf_counter()
    await retriever.bm25.rebuild(chunks)
    es_dt = time.perf_counter() - t2

    logger.info(
        "[eval] indexed %d chunks (embed %.1fs, publish %.1fs, es %.1fs)",
        written, embed_dt, upsert_dt, es_dt,
    )
    return {
        "chunks_indexed": written,
        "embed_seconds": round(embed_dt, 2),
        "upsert_seconds": round(upsert_dt, 2),
        "es_rebuild_seconds": round(es_dt, 2),
    }


async def run_queries(
    retriever: HybridRetriever,
    queries: Sequence[dict],
    *,
    top_k: int,
    top_n: int,
    threshold: float = 0.0,
    rerank: bool = True,
    concurrency: int = 4,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Run every query through the retriever; return rankings + timing.

    Args:
        threshold: rerank confidence cutoff; 0.0 keeps the full ranked list so
            @k ranking metrics reflect ordering, not confidence filtering.
        rerank: when False, run dense+sparse+RRF only (skips the cross-encoder).

    Returns:
        ``(rankings, stats)`` where ``rankings`` maps ``query_id -> ranked
        doc_ids`` and ``stats`` carries latency + score-mode breakdown.
    """
    settings = retriever._settings  # noqa: SLF001
    orig_threshold = settings.retrieval_score_threshold
    orig_rerank = settings.rerank_enabled
    settings.retrieval_score_threshold = threshold
    settings.rerank_enabled = rerank

    sem = asyncio.Semaphore(max(1, concurrency))
    timings: list[float] = []
    body_fetch: list[float] = []      # 本方案新增: PG chunk_text 主键批量回表耗时(ms)
    parent_fetch: list[float] = []    # 本方案新增: Mongo parent_texts $in 耗时(ms)
    score_modes: dict[str, int] = {}
    rankings: dict[str, list[str]] = {}

    # 受控包装: 采样正文回表与父块组装两次定点读(本方案相比旧链路的唯一新增开销)。
    orig_attach = retriever.store.attach_texts

    async def _timed_attach(seq):
        t = time.perf_counter()
        out = await orig_attach(seq)
        body_fetch.append((time.perf_counter() - t) * 1000)
        return out

    retriever.store.attach_texts = _timed_attach  # type: ignore[method-assign]

    async def one(item: dict) -> None:
        qid = str(item["query_id"])
        async with sem:
            start = time.perf_counter()
            try:
                chunks, mode = await retriever.retrieve(
                    item["query"], top_k=top_k, top_n=top_n, principal=None
                )
                ranked = [c.doc_id for c in chunks]
                # 组装父块一次(生产出口本就会做): 采样 Mongo parent_texts 耗时。
                pa = time.perf_counter()
                await retriever.assemble_parents(chunks)
                parent_fetch.append((time.perf_counter() - pa) * 1000)
            except Exception as exc:  # noqa: BLE001 - one query must not abort run
                logger.warning("[eval] query %s failed: %s", qid, exc)
                ranked, mode = [], "error"
            timings.append(time.perf_counter() - start)
        rankings[qid] = ranked
        score_modes[mode] = score_modes.get(mode, 0) + 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one(q) for q in queries))
    wall = time.perf_counter() - t0
    retriever.store.attach_texts = orig_attach  # type: ignore[method-assign]

    settings.retrieval_score_threshold = orig_threshold
    settings.rerank_enabled = orig_rerank

    def _p95(xs: list[float]) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        return round(s[max(0, int(len(s) * 0.95) - 1)], 3)

    ordered = sorted(timings)
    stats = {
        "queries_run": len(queries),
        "wall_seconds": round(wall, 2),
        "avg_latency_seconds": round(sum(timings) / len(timings), 3) if timings else 0.0,
        "p95_latency_seconds": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3)
        if ordered
        else 0.0,
        "body_fetch_ms": {
            "avg": round(sum(body_fetch) / len(body_fetch), 3) if body_fetch else 0.0,
            "p95": _p95(body_fetch),
        },
        "parent_fetch_ms": {
            "avg": round(sum(parent_fetch) / len(parent_fetch), 3) if parent_fetch else 0.0,
            "p95": _p95(parent_fetch),
        },
        "score_modes": score_modes,
        "top_k": top_k,
        "top_n": top_n,
        "threshold": threshold,
        "rerank": rerank,
        "concurrency": concurrency,
    }
    return rankings, stats


async def drop_eval_stores(
    engine: AsyncEngine,
    *,
    database: str = DEFAULT_EVAL_DB,
    es_index: str = DEFAULT_EVAL_ES_INDEX,
    drop_database: bool = True,
    drop_eval_mongo: bool = True,
    mongo_database: str = DEFAULT_EVAL_MONGO_DB,
) -> dict[str, Any]:
    """Drop the eval ES index (always) + optionally the eval PG db 与 eval Mongo 库。"""
    report: dict[str, Any] = {}
    await engine.dispose()
    try:
        bm25 = ElasticBM25Retriever(index=es_index)
        await bm25._client.indices.delete(index=es_index, ignore=[404])  # noqa: SLF001
        await bm25._client.close()  # noqa: SLF001
        report["elasticsearch"] = f"dropped {es_index}"
    except Exception as exc:  # noqa: BLE001
        report["elasticsearch"] = f"error: {exc}"
    if drop_eval_mongo:
        try:
            await get_mongo_client()[mongo_database].drop_database()
            report["mongo"] = f"dropped {mongo_database}"
        except Exception as exc:  # noqa: BLE001
            report["mongo"] = f"error: {exc}"
        # 还原 BodyStore 单例的 db 指向(下轮或生产路径重新惰性取默认库)。
        get_body_store()._db = None  # noqa: SLF001
    if drop_database:
        settings = get_settings()
        # Reset the singleton first so nothing else is holding the eval engine.
        db_session._engine = None  # noqa: SLF001
        db_session._session_factory = None  # noqa: SLF001
        maint = create_async_engine(
            _database_url_with(settings.pg_database),
            connect_args=_connect_args(),
            execution_options={"isolation_level": "AUTOCOMMIT"},
        )
        try:
            async with maint.connect() as conn:
                await conn.execute(
                    text(f'drop database if exists "{database}" with (force)')
                )
            report["database"] = f"dropped {database}"
        except Exception as exc:  # noqa: BLE001
            report["database"] = f"error: {exc}"
        finally:
            await maint.dispose()
    return report
