"""Isolated evaluation harness around the production ``HybridRetriever``.

The whole point is to measure the *real* pipeline (dense bge-m3 via
PostgreSQL/pgvector + sparse BM25 via Elasticsearch -> RRF fusion ->
bge-reranker) without ever touching the production ``knowledge_chunks`` table
or the ``kb_chunks`` ES index. Since the storage layer is a single shared
PostgreSQL database, we isolate on two axes:

* **Vector / document store** -> a dedicated PostgreSQL database
  (``mxi_msmarco_eval``) holding its own ``knowledge_chunks`` table (pgvector +
  HNSW). A separate database (not just a schema) removes any search_path
  ambiguity, so an errant query can never read or write production rows.
* **Lexical store** -> a dedicated Elasticsearch index.

The production code is reused verbatim: ``PgVectorStore`` resolves its session
factory lazily from ``app.db.session``, so we provision the eval database,
install an eval-scoped async engine as the process session factory, and drive
the *unmodified* :class:`HybridRetriever` against it. The retriever is built
via ``__new__`` (bypassing ``__init__``, which would otherwise bind the
production ES singleton).

MS MARCO passages are indexed as *child-only* chunks (``is_parent=false``):
they are already retrieval-grain, so the production parent/child split adds
only a redundant parent row. ``retrieve()`` filters ``is_parent = false`` and
metric comparison happens at the passage (``doc_id``) level, so this is
functionally identical to the production path for ranking.
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
from app.db import session as db_session
from app.db.models import KnowledgeChunkRow
from app.rag.bm25 import ElasticBM25Retriever
from app.rag.embeddings import OllamaEmbedder
from app.rag.reranker import OllamaReranker
from app.rag.retriever import HybridRetriever
from app.rag.vectorstore import PgVectorStore
from app.schemas import KnowledgeChunk

logger = logging.getLogger(__name__)

DEFAULT_EVAL_DB = "mxi_msmarco_eval"
DEFAULT_EVAL_ES_INDEX = "msmarco_eval_chunks"


def corpus_to_chunks(corpus: dict[str, dict]) -> list[KnowledgeChunk]:
    """Turn ``docid -> {title, text}`` into child-only retrievable chunks."""
    chunks: list[KnowledgeChunk] = []
    for doc_id, passage in corpus.items():
        text_ = (passage.get("text") or "").strip()
        if not text_:
            continue
        chunks.append(
            KnowledgeChunk(
                chunk_id=f"marco-{doc_id}"[:80],
                doc_id=str(doc_id)[:64],
                title=(passage.get("title") or "")[:512],
                content=text_[:8192],
                source="msmarco",
                modality="text",
                is_parent=False,
                parent_id="",
                page_no=-1,
                section="",
                visibility="public",
            )
        )
    return chunks


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
        await conn.run_sync(KnowledgeChunkRow.__table__.create, checkfirst=True)
    return engine


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
    retriever.reranker = OllamaReranker()
    retriever.store = PgVectorStore()  # binds to the installed eval engine
    retriever.bm25 = ElasticBM25Retriever(index=es_index)
    return retriever


async def rerank_available(probe_text: str = "relevance probe") -> bool:
    """Best-effort health check for the cross-encoder reranker.

    On some hosts (notably Windows Ollama + the bge-reranker GGUF) the rerank
    model crashes llama-server on *every* call (exit 0xc0000409). Probing once
    lets the eval skip a pathological ~30s-per-query fallback loop and report
    the mode it actually measured. Never raises.
    """
    try:
        await OllamaReranker()._embed([probe_text])  # noqa: SLF001
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[eval] reranker unavailable, will measure RRF order: %s", str(exc)[:160])
        return False


async def _truncate_chunks(store: PgVectorStore) -> None:
    """Remove any rows left by a previous run (single-table eval store)."""
    async with store._sessions()() as session:  # noqa: SLF001
        await session.execute(text("truncate table knowledge_chunks"))
        await session.commit()


async def ingest_corpus(
    retriever: HybridRetriever,
    corpus: dict[str, dict],
) -> dict[str, Any]:
    """Embed + upsert the corpus into pgvector and rebuild the ES BM25 index."""
    chunks = corpus_to_chunks(corpus)
    store: PgVectorStore = retriever.store  # type: ignore[assignment]
    await _truncate_chunks(store)

    t0 = time.perf_counter()
    vectors = await retriever.embedder.embed([c.content for c in chunks])
    embed_dt = time.perf_counter() - t0

    t1 = time.perf_counter()
    written = await store.upsert(chunks, vectors)
    upsert_dt = time.perf_counter() - t1

    t2 = time.perf_counter()
    await retriever.bm25.rebuild(chunks)
    es_dt = time.perf_counter() - t2

    logger.info(
        "[eval] indexed %d chunks (embed %.1fs, upsert %.1fs, es %.1fs)",
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
    score_modes: dict[str, int] = {}
    rankings: dict[str, list[str]] = {}

    async def one(item: dict) -> None:
        qid = str(item["query_id"])
        async with sem:
            start = time.perf_counter()
            try:
                chunks, mode = await retriever.retrieve(
                    item["query"], top_k=top_k, top_n=top_n, principal=None
                )
                ranked = [c.doc_id for c in chunks]
            except Exception as exc:  # noqa: BLE001 - one query must not abort run
                logger.warning("[eval] query %s failed: %s", qid, exc)
                ranked, mode = [], "error"
            timings.append(time.perf_counter() - start)
        rankings[qid] = ranked
        score_modes[mode] = score_modes.get(mode, 0) + 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one(q) for q in queries))
    wall = time.perf_counter() - t0

    settings.retrieval_score_threshold = orig_threshold
    settings.rerank_enabled = orig_rerank

    ordered = sorted(timings)
    stats = {
        "queries_run": len(queries),
        "wall_seconds": round(wall, 2),
        "avg_latency_seconds": round(sum(timings) / len(timings), 3) if timings else 0.0,
        "p95_latency_seconds": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3)
        if ordered
        else 0.0,
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
) -> dict[str, Any]:
    """Drop the eval ES index (always) and optionally the whole eval database."""
    report: dict[str, Any] = {}
    await engine.dispose()
    try:
        bm25 = ElasticBM25Retriever(index=es_index)
        await bm25._client.indices.delete(index=es_index, ignore=[404])  # noqa: SLF001
        await bm25._client.close()  # noqa: SLF001
        report["elasticsearch"] = f"dropped {es_index}"
    except Exception as exc:  # noqa: BLE001
        report["elasticsearch"] = f"error: {exc}"
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
