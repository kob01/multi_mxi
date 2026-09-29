"""MS MARCO retrieval evaluation for the enterprise RAG pipeline.

This package turns the project's production hybrid retriever
(dense bge-m3 via PostgreSQL/pgvector + sparse BM25 via Elasticsearch -> RRF
fusion -> bge-reranker) into a measurable information-retrieval system by
running it against a sample of the MS MARCO Passage Ranking dataset and scoring
the ranked lists with standard IR metrics (Recall/Precision/Hit/MRR/nDCG/MAP@k),
then emitting a JSON + Markdown report.

Isolation: the evaluation provisions a *separate* PostgreSQL database
(``mxi_msmarco_eval``), a *separate* Elasticsearch index and a *separate*
Mongo database, so the production ``doc_chunks``/``doc_parents`` tables,
``kb_chunks`` index and body collections are never touched.

Run with (from the repo root)::

    python -m scripts.msmarco_eval --num-queries 200

See :mod:`scripts.msmarco_eval.__main__` for the full CLI.
"""

from scripts.msmarco_eval.dataset import MSMarcoSample, load_sample
from scripts.msmarco_eval.metrics import evaluate_run
from scripts.msmarco_eval.report import build_report, write_reports

__all__ = [
    "MSMarcoSample",
    "load_sample",
    "evaluate_run",
    "build_report",
    "write_reports",
]
