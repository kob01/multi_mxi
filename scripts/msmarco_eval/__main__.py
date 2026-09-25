"""CLI entry point: ``python -m scripts.msmarco_eval``.

End-to-end MS MARCO retrieval evaluation of the project's hybrid RAG pipeline:

    load/sample dataset  ->  build isolated Milvus + ES index  ->
    run queries through HybridRetriever  ->  score IR metrics  ->
    write JSON + Markdown report

Examples
--------
Quick smoke test (20 queries, keep stores for inspection)::

    python -m scripts.msmarco_eval --num-queries 20

Fuller run (200 queries, dense+sparse+RRF+rerank, clean up afterwards)::

    python -m scripts.msmarco_eval --num-queries 200 --top-k 50 --top-n 10 --cleanup

Ablation: skip the cross-encoder reranker (dense + BM25 + RRF only)::

    python -m scripts.msmarco_eval --num-queries 200 --no-rerank
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from scripts.msmarco_eval import harness
from scripts.msmarco_eval.dataset import DEFAULT_HEAD_MB, load_sample
from scripts.msmarco_eval.metrics import evaluate_run
from scripts.msmarco_eval.report import build_report, write_reports


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scripts.msmarco_eval",
        description="Evaluate the enterprise RAG pipeline on a MS MARCO sample.",
    )
    p.add_argument("--num-queries", type=int, default=200, help="queries to sample (default 200)")
    p.add_argument("--seed", type=int, default=42, help="sampling seed (default 42)")
    p.add_argument("--top-k", type=int, default=50, help="candidates per channel before fusion (default 50)")
    p.add_argument("--top-n", type=int, default=10, help="final ranked list depth (default 10)")
    p.add_argument(
        "--ks", type=int, nargs="+", default=[1, 3, 5, 10],
        help="metric cutoff depths (default 1 3 5 10)",
    )
    p.add_argument("--threshold", type=float, default=0.0, help="rerank confidence cutoff; 0 keeps full ranked list")
    p.add_argument("--no-rerank", dest="rerank", action="store_false", help="disable cross-encoder rerank (RRF order)")
    p.set_defaults(rerank=True)
    p.add_argument("--concurrency", type=int, default=4, help="concurrent queries (default 4)")
    p.add_argument("--head-mb", type=int, default=DEFAULT_HEAD_MB, help="MiB cap for the raw download when uncached")
    p.add_argument("--force-download", action="store_true", help="re-download and rebuild the cached sample")
    p.add_argument(
        "--sample-file", type=Path, default=None,
        help="optional local MS MARCO train.jsonl(.gz) to use instead of downloading",
    )
    p.add_argument("--out-dir", type=Path, default=None, help="report output dir (default <root>/reports)")
    p.add_argument("--cleanup", action="store_true", help="drop the eval Milvus collection + ES index when done")
    p.add_argument("--log-level", default="INFO", help="logging level (INFO/DEBUG/...)")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1) dataset
    print(f"[1/4] loading MS MARCO sample (num_queries={args.num_queries}, seed={args.seed}) ...")
    sample = load_sample(
        num_queries=args.num_queries,
        seed=args.seed,
        head_mb=args.head_mb,
        head_path=args.sample_file,
        force_download=args.force_download,
    )
    print(f"      queries={sample.n_queries} corpus_passages={sample.n_corpus}")

    # 2) isolated index (separate PostgreSQL db + separate ES index)
    print(f"[2/4] provisioning isolated eval store: pg={harness.DEFAULT_EVAL_DB} "
          f"es={harness.DEFAULT_EVAL_ES_INDEX} ...")
    engine = await harness.provision_eval_database()
    harness.install_eval_engine(engine)
    retriever = harness.make_retriever(es_index=harness.DEFAULT_EVAL_ES_INDEX)
    index_stats = await harness.ingest_corpus(retriever, sample.corpus)
    print(f"      indexed {index_stats['chunks_indexed']} chunks "
          f"(embed {index_stats['embed_seconds']}s)")

    # 3) retrieval run
    rerank_effective = args.rerank
    if args.rerank:
        rerank_effective = await harness.rerank_available()
        if not rerank_effective:
            print("      NOTE: cross-encoder reranker unavailable on this host "
                  "(Ollama GGUF crash); measuring dense+BM25+RRF order instead.")
    print(f"[3/4] running {len(sample.queries)} queries "
          f"(top_k={args.top_k} top_n={args.top_n} rerank={rerank_effective}) ...")
    rankings, run_stats = await harness.run_queries(
        retriever,
        sample.queries,
        top_k=args.top_k,
        top_n=args.top_n,
        threshold=args.threshold,
        rerank=rerank_effective,
        concurrency=args.concurrency,
    )
    print(f"      done in {run_stats['wall_seconds']}s "
          f"(avg {run_stats['avg_latency_seconds']}s/query)")

    # 4) metrics + report
    eval_result = evaluate_run(rankings, sample.qrels, ks=args.ks)
    config = {
        "num_queries": sample.n_queries,
        "corpus_passages": sample.n_corpus,
        "top_k": args.top_k,
        "top_n": args.top_n,
        "ks": sorted(set(args.ks)),
        "rerank_requested": args.rerank,
        "rerank_effective": rerank_effective,
        "threshold": args.threshold,
        "seed": args.seed,
        "eval_database": harness.DEFAULT_EVAL_DB,
        "es_index": harness.DEFAULT_EVAL_ES_INDEX,
    }
    report = build_report(
        sample_summary={**sample.summary(), "counts": eval_result["counts"]},
        config=config,
        eval_result=eval_result,
        index_stats=index_stats,
        run_stats=run_stats,
    )
    report["counts"] = eval_result["counts"]
    json_path, md_path = write_reports(report, out_dir=args.out_dir)
    print("[4/4] report written:")
    print(f"      {md_path}")
    print(f"      {json_path}")

    agg = eval_result["aggregate"]
    print("\n===== HEADLINE METRICS =====")
    for key in ("recall@10", "mrr@10", "ndcg@10", "hit@10", "precision@10", "map@10"):
        if key in agg:
            print(f"  {key:14s} = {agg[key]:.4f}")

    if args.cleanup:
        res = await harness.drop_eval_stores(
            engine,
            database=harness.DEFAULT_EVAL_DB,
            es_index=harness.DEFAULT_EVAL_ES_INDEX,
        )
        print(f"[cleanup] {res}")
    # Best-effort ES client close (avoids the aiohttp 'unclosed session' warning).
    try:
        await retriever.bm25._client.close()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[aborted]", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - surface a clean failure to the CLI
        logging.getLogger(__name__).exception("evaluation run failed")
        print(f"\n[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
