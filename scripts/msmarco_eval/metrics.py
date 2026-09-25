"""Standard information-retrieval metrics for graded-free (binary) relevance.

All functions take a *ranked list of retrieved doc ids* (best first) and a set
of *relevant doc ids* (the qrels positives for one query). Cutoff ``k`` limits
the depth considered. Implementations are pure Python (no scipy/sklearn) and
follow the TREC / pytrec conventions used by the official
``msmarco_eval.py`` (MRR@10 in particular).
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def hit_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """1.0 if at least one relevant doc appears in the top-k, else 0.0."""
    return 1.0 if any(doc in relevant for doc in ranked[:k]) else 0.0


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-k that is relevant."""
    if k <= 0:
        return 0.0
    top = ranked[:k]
    return sum(1 for doc in top if doc in relevant) / k


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of all relevant docs recovered within the top-k."""
    if not relevant:
        return 0.0
    found = sum(1 for doc in ranked[:k] if doc in relevant)
    return found / len(relevant)


def mrr_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Reciprocal rank of the first relevant doc within top-k (0 if none)."""
    for i, doc in enumerate(ranked[:k]):
        if doc in relevant:
            return 1.0 / (i + 1)
    return 0.0


def dcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Discounted cumulative gain with binary gains (log2 position discount)."""
    return sum(
        1.0 / math.log2(i + 2)
        for i, doc in enumerate(ranked[:k])
        if doc in relevant
    )


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Normalized DCG@k against the ideal ordering of the relevant docs."""
    if not relevant:
        return 0.0
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return dcg_at_k(ranked, relevant, k) / idcg if idcg else 0.0


def ap_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Average precision @k (mean of precision at each relevant hit)."""
    if not relevant:
        return 0.0
    hits = 0
    score = 0.0
    for i, doc in enumerate(ranked[:k]):
        if doc in relevant:
            hits += 1
            score += hits / (i + 1)
    denom = min(len(relevant), k)
    return score / denom if denom else 0.0


_METRIC_FUNCS = {
    "hit": hit_at_k,
    "precision": precision_at_k,
    "recall": recall_at_k,
    "mrr": mrr_at_k,
    "ndcg": ndcg_at_k,
    "map": ap_at_k,
}


def evaluate_run(
    rankings: dict[str, Sequence[str]],
    qrels: dict[str, Sequence[str]],
    ks: Sequence[int] = (1, 3, 5, 10),
) -> dict:
    """Score a full retrieval run.

    Args:
        rankings: ``query_id -> ranked doc ids`` (best first) from the system.
        qrels:    ``query_id -> relevant doc ids`` (ground truth positives).
        ks:       cutoff depths to report.

    Returns:
        ``{"aggregate": {metric@k: mean}, "per_query": {qid: {metric@k: val}},
        "counts": {...}}`` — aggregate values are means over all scored queries.
    """
    ks = sorted(set(ks))
    metric_names = list(_METRIC_FUNCS)
    aggregate: dict[str, float] = {
        f"{name}@{k}": 0.0 for name in metric_names for k in ks
    }
    per_query: dict[str, dict[str, float]] = {}
    n_scored = 0
    n_with_relevant = 0
    for qid, relevant_seq in qrels.items():
        relevant = set(relevant_seq)
        if not relevant:
            continue
        n_with_relevant += 1
        ranked = list(rankings.get(qid, []))
        row: dict[str, float] = {}
        for name in metric_names:
            fn = _METRIC_FUNCS[name]
            for k in ks:
                val = fn(ranked, relevant, k)
                row[f"{name}@{k}"] = val
                aggregate[f"{name}@{k}"] += val
        per_query[qid] = row
        n_scored += 1

    denom = n_scored or 1
    for key in aggregate:
        aggregate[key] = round(aggregate[key] / denom, 4)

    return {
        "aggregate": aggregate,
        "per_query": per_query,
        "counts": {
            "queries_scored": n_scored,
            "queries_with_relevant": n_with_relevant,
            "ks": ks,
        },
    }
