"""Evaluation report assembly: machine-readable JSON + human-readable Markdown.

``build_report`` bundles the dataset summary, run configuration, aggregate IR
metrics, worst-performing queries and timing into a single dict; ``write_reports``
persists it as ``*.json`` (full detail) and ``*.md`` (glanceable tables) under
``reports/``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

# Metric display order / labels for the Markdown summary.
_HEADLINE = [
    ("recall@10", "Recall@10"),
    ("mrr@10", "MRR@10"),
    ("ndcg@10", "nDCG@10"),
    ("hit@10", "Hit Rate@10"),
    ("precision@10", "P@10"),
    ("map@10", "MAP@10"),
]
_KS = (1, 3, 5, 10)


def build_report(
    *,
    sample_summary: dict,
    config: dict,
    eval_result: dict,
    index_stats: dict,
    run_stats: dict,
) -> dict:
    """Assemble the full evaluation report dict."""
    aggregate = eval_result["aggregate"]
    per_query = eval_result["per_query"]

    # List queries that missed the relevant doc in the top-10, worst first.
    missed = [
        (qid, row)
        for qid, row in per_query.items()
        if row.get("hit@10", 0.0) < 1.0
    ]
    missed.sort(key=lambda kv: (kv[1].get("mrr@10", 0.0), kv[1].get("ndcg@10", 0.0)))
    worst = []
    for qid, row in missed[:20]:
        worst.append(
            {
                "query_id": qid,
                "mrr@10": row.get("mrr@10", 0.0),
                "recall@10": row.get("recall@10", 0.0),
                "ndcg@10": row.get("ndcg@10", 0.0),
            }
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "system": "MXI Enterprise RAG (HybridRetriever: bge-m3 + ES BM25 + RRF + bge-reranker)",
        "config": config,
        "dataset": sample_summary,
        "indexing": index_stats,
        "retrieval": run_stats,
        "metrics": aggregate,
        "worst_queries": worst,
        "per_query": per_query,
    }


def _fmt(v: float) -> str:
    return f"{v:.4f}"


def render_markdown(report: dict) -> str:
    """Render a compact Markdown report from the assembled dict."""
    m = report["metrics"]
    ds = report["dataset"]
    cfg = report["config"]
    idx = report["indexing"]
    run = report["retrieval"]
    counts = report.get("counts", {})

    lines: list[str] = []
    lines.append("# MS MARCO RAG 检索评测报告")
    lines.append("")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 被测系统：{report['system']}")
    lines.append(f"- 数据集：{ds.get('source')}")
    lines.append("")

    lines.append("## 1. 评测配置")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    for key in (
        "num_queries", "corpus_passages", "top_k", "top_n",
        "rerank_requested", "rerank_effective", "threshold", "seed",
        "eval_database", "eval_mongo_database", "es_index", "chunk_store",
    ):
        if key in cfg:
            lines.append(f"| {key} | {cfg[key]} |")
    lines.append("")

    lines.append("## 2. 数据集规模")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 查询数 | {ds.get('queries')} |")
    lines.append(f"| 语料段落数(corpus) | {ds.get('corpus_passages')} |")
    lines.append(f"| 每查询平均相关段落 | {ds.get('relevant_per_query_avg')} |")
    lines.append(f"| 参与打分查询数 | {counts.get('queries_scored', run.get('queries_run'))} |")
    lines.append("")

    lines.append("## 3. 索引构建耗时")
    lines.append("")
    lines.append("| 阶段 | 秒 |")
    lines.append("| --- | --- |")
    lines.append(f"| Embedding | {idx.get('embed_seconds')} |")
    lines.append(f"| pgvector upsert | {idx.get('upsert_seconds')} |")
    lines.append(f"| ES BM25 重建 | {idx.get('es_rebuild_seconds')} |")
    lines.append(f"| 索引段落总数 | {idx.get('chunks_indexed')} |")
    lines.append("")

    lines.append("## 4. 检索指标（主）")
    lines.append("")
    lines.append("| 指标 | 分值 |")
    lines.append("| --- | --- |")
    for key, label in _HEADLINE:
        if key in m:
            lines.append(f"| {label} | {_fmt(m[key])} |")
    lines.append("")

    lines.append("## 5. 指标明细（按截断深度）")
    lines.append("")
    header = "| 指标 | " + " | ".join(f"@{k}" for k in _KS) + " |"
    sep = "| --- | " + " | ".join("---" for _ in _KS) + " |"
    lines.append(header)
    lines.append(sep)
    for name, label in (
        ("recall", "Recall"), ("precision", "Precision"), ("hit", "Hit Rate"),
        ("mrr", "MRR"), ("ndcg", "nDCG"), ("map", "MAP"),
    ):
        row = f"| {label} | " + " | ".join(
            _fmt(m[f"{name}@{k}"]) if f"{name}@{k}" in m else "-" for k in _KS
        ) + " |"
        lines.append(row)
    lines.append("")

    lines.append("## 6. 检索性能")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 端到端墙钟 (s) | {run.get('wall_seconds')} |")
    lines.append(f"| 平均单查询延迟 (s) | {run.get('avg_latency_seconds')} |")
    lines.append(f"| P95 延迟 (s) | {run.get('p95_latency_seconds')} |")
    bf = run.get("body_fetch_ms") or {}
    pf = run.get("parent_fetch_ms") or {}
    lines.append(f"| 正文主键回表 body_fetch (ms avg/p95) | {bf.get('avg')} / {bf.get('p95')} |")
    lines.append(f"| 父块 Mongo 取回 parent_fetch (ms avg/p95) | {pf.get('avg')} / {pf.get('p95')} |")
    lines.append(f"| 打分模式分布 | {run.get('score_modes')} |")
    lines.append("")

    worst = report.get("worst_queries") or []
    lines.append("## 7. 表现最差的查询（Top-10 未命中相关段落）")
    lines.append("")
    if not worst:
        lines.append("无 —— 所有查询在 Top-10 内均命中了相关段落。")
    else:
        lines.append("| query_id | MRR@10 | Recall@10 | nDCG@10 |")
        lines.append("| --- | --- | --- | --- |")
        for w in worst:
            lines.append(
                f"| {w['query_id']} | {w['mrr@10']:.3f} | "
                f"{w['recall@10']:.3f} | {w['ndcg@10']:.3f} |"
            )
    lines.append("")

    lines.append("## 8. 说明与边界")
    lines.append("")
    lines.append("- 数据取自 MS MARCO Passage Ranking（Tevatron 重打包，非 gated）。"
                 "官方 Azure Blob 源已禁止公共访问、HuggingFace 主站在当前网络不可达，"
                 "故经 hf-mirror 镜像以 HTTP Range 流式下载 `train.jsonl.gz` 头部。")
    lines.append("- 每查询的候选池 = 该查询的正例段落 + 其 30 条 BM25 难负例（跨查询去重后合并为共享语料），"
                 "因此这是一个『在词面近似干扰项中召回标注相关段落』的真实检索任务。")
    lines.append("- 评测使用与生产完全相同的混合检索链路（bge-m3 稠密 + ES BM25 稀疏 + RRF + "
                 "bge-reranker），仅指向独立的 PostgreSQL 评测库与独立 ES 索引，不触碰生产数据。")
    lines.append("- 采用 train split 的标注查询做检索排序评估：被测系统（bge-m3 + BM25 + reranker）"
                 "并非在 MS MARCO 上训练，无数据泄漏，排序质量评估成立。")
    lines.append("")
    return "\n".join(lines)


def write_reports(report: dict, out_dir: Path | None = None) -> tuple[Path, Path]:
    """Write ``*.json`` + ``*.md`` into ``out_dir`` (default ``<root>/reports``).

    Returns the (json_path, md_path) tuple.
    """
    out_dir = Path(out_dir) if out_dir else (get_settings().base_dir / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"msmarco_eval_{stamp}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    # Stable "latest" copies for easy discovery.
    (out_dir / "msmarco_eval_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "msmarco_eval_latest.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    return json_path, md_path
