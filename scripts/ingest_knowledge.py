"""RAG knowledge-base build script.

Parses documents under KNOWLEDGE_DIR (txt/md/pdf/docx + video transcripts),
chunks, embeds with bge-m3 and upserts into the pgvector ``knowledge_chunks``
table, then rebuilds the Elasticsearch BM25 index (also the repair path for an
existing corpus: re-running this script re-syncs PostgreSQL -> ES wholesale).

Usage:
    python -m scripts.ingest_knowledge [--dir ./data/knowledge]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config import get_settings
from app.rag.bm25 import get_es_bm25
from app.rag.embeddings import OllamaEmbedder
from app.rag.ingest import collect_corpus, ingest_directory
from app.rag.vectorstore import PgVectorStore


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
    report = await ingest_directory(knowledge_dir, store, embedder)
    for path, n in report.items():
        print(f"[ingest] {path}: {n} chunks")
    print(f"[ingest] total chunks in store: {await store.count()}")

    await get_es_bm25().rebuild(await collect_corpus(store))
    print("[ingest] Elasticsearch BM25 index rebuilt (assistant also rebuilds on startup)")


if __name__ == "__main__":
    asyncio.run(main())
