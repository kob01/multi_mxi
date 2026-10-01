"""RAG knowledge-base build script.

Parses documents under KNOWLEDGE_DIR (txt/md/pdf/docx + video transcripts),
chunks, embeds with bge-m3 and publishes them into the pgvector parent/child
tables (``doc_chunks`` + ``doc_parents``, 正文入 Mongo), then rebuilds the
Elasticsearch BM25 index (also the repair path for an existing corpus:
re-running this script re-syncs PostgreSQL -> ES wholesale).

Usage:
    python -m scripts.ingest_knowledge [--dir ./data/knowledge]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config import get_settings
from app.docs.parsers import modality_of
from app.rag.bm25 import get_es_bm25
from app.rag.embeddings import OllamaEmbedder
from app.rag.ingest import collect_corpus, compute_doc_id, ingest_directory
from app.rag.vectorstore import PgVectorStore


async def _register_documents(report: dict[str, int]) -> None:
    """给每篇入库文档补一行 documents 元数据(status='ready', 全员可见)。

    为什么必须补: 检索出口有发布态门禁(``docs_not_ready``), 没有 documents 行的
    doc_id 会被当成"孤儿向量"默认拒 —— 只跑本脚本不补元数据的话, 块明明在库里,
    检索却永远返回"未找到相关文档"。这与 Web 上传路径(ingest_confirmed 先建元数据)
    对齐: 知识块与元数据必须成对出现。
    """
    from pathlib import Path as _Path

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.models import Document
    from app.db.session import get_session_factory

    factory = get_session_factory()
    for raw_path, chunk_count in report.items():
        path = _Path(raw_path)
        ext = path.suffix.lower()
        doc_id = compute_doc_id(path.stem, ext)
        async with factory() as session:
            stmt = (
                pg_insert(Document)
                .values(
                    doc_key=doc_id,
                    name=path.stem,
                    ext=ext,
                    modality=modality_of(path),
                    file_path=str(path),
                    size_bytes=path.stat().st_size,
                    status="ready",
                    chunk_count=chunk_count,
                    body_stored=True,
                    visibility="public",
                    created_by="system",
                    parsed_text="",
                )
                .on_conflict_do_update(
                    index_elements=["doc_key"],
                    set_={
                        "status": "ready",
                        "chunk_count": chunk_count,
                        "size_bytes": path.stat().st_size,
                        "modality": modality_of(path),
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()
            print(f"[ingest] documents row ready: {path.name} -> {doc_id} ({chunk_count} chunks)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest enterprise knowledge into pgvector")
    parser.add_argument("--dir", default=get_settings().knowledge_dir, help="knowledge directory")
    args = parser.parse_args()

    knowledge_dir = Path(args.dir)
    if not knowledge_dir.exists():
        raise SystemExit(f"knowledge dir not found: {knowledge_dir}")

    store = PgVectorStore()
    embedder = OllamaEmbedder()

    print(f"[ingest] scanning {knowledge_dir} ...")
    failures: list[tuple[str, str]] = []
    report = await ingest_directory(knowledge_dir, store, embedder, failures=failures)
    for path, n in report.items():
        print(f"[ingest] {path}: {n} chunks")
    # 失败不再静默: 以前一批文件"入库数为 0"而日志里什也看不到。
    for path, reason in failures:
        print(f"[ingest] FAILED {path}: {reason}")
    if failures:
        print(f"[ingest] {len(failures)} 个文件未能入库(其余已入库), 请修正后重跑本脚本")
    print(f"[ingest] total chunks in store: {await store.count()}")

    await _register_documents(report)
    await get_es_bm25().rebuild(await collect_corpus(store))
    print("[ingest] Elasticsearch BM25 index rebuilt (assistant also rebuilds on startup)")


if __name__ == "__main__":
    asyncio.run(main())
