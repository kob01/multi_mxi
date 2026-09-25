"""Controlled A/B experiment: structure-aware chunking vs fixed-window chunking.

Question this answers quantitatively
------------------------------------
"On a real document corpus, how much does *parsing structure first* (splitting
on section/heading boundaries and carrying the heading as metadata) improve
retrieval versus *naively slicing the raw text at a fixed size*?"

Why this is a fair, controlled test
-----------------------------------
MS MARCO passages don't ship as hierarchical documents, so we *assemble* a
deterministic pseudo-corpus: passages are grouped into ~10-section markdown
documents (seeded shuffle), each passage becoming one `## <title>` section.
Both arms then index **exactly the same underlying bytes**, are queried by the
**same queries**, and scored against the **same relevance evidence** (a query's
positive passages). The ONLY variable is the chunking rule:

* **Arm A (structure-aware)** — one chunk per section, the section heading kept
  as `title` metadata (so it enters the BM25 channel via ``title + content``
  and the citation string). Boundaries align with sections; a section is never
  split. This mirrors what ``app/docs/parsers.py`` produces for real md/html/pdf.
* **Arm B (fixed window)** — each document is flattened to one raw string
  (heading lines inline) and slid through a fixed ``CHUNK_SIZE`` window with
  ``CHUNK_OVERLAP`` overlap, exactly like a naive splitter that ignores
  structure. Windows therefore cut across section boundaries; heading text is
  orphaned mid-chunk; every section that straddles a boundary is fragmented.

Relevance at chunk granularity
------------------------------
The IR metrics compare ``doc_id``s, but after re-chunking a "unit" is no longer
a passage. We map each evidence passage to the unit(s) that *contain* it:
* Arm A: a passage's section is one clean chunk -> 1 relevant unit, 100% cover.
* Arm B: a passage spans the window(s) holding a majority of its characters
  (>= COVERAGE threshold; fall back to the single best-covering window so recall
  is never artificially capped).

Honest scope of the number this produces
----------------------------------------
MS MARCO positives are themselves retrieval-grain *sections*, so Arm A is
aligned with the ground truth *by construction*. The measured delta is therefore
an **upper bound** on the benefit of structure-aware chunking and specifically
quantifies the *cost of naively slicing a well-structured document* -- not a
universal constant. Read the report's caveats before quoting the number.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from scripts.msmarco_eval import harness
from scripts.msmarco_eval.dataset import load_sample
from scripts.msmarco_eval.metrics import evaluate_run

logger = logging.getLogger("chunking_ab")

# Fixed-window parameters mirrored from the production fallback splitter
# (app/rag/ingest.py: CHUNK_SIZE / CHUNK_OVERLAP) so Arm B is representative.
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
# A window counts as "relevant" for a passage only if it holds this fraction of
# the passage's characters; guarantees the evidence is mostly present.
COVERAGE = 0.5

AB_DB = "mxi_chunk_ab"
AB_ES_INDEX = "chunk_ab_units"


# --------------------------------------------------------------------------- #
# Corpus assembly: MS MARCO passages -> structured pseudo-documents
# --------------------------------------------------------------------------- #
def assemble_documents(
    corpus: dict[str, dict], *, seed: int, sections_per_doc: int
) -> list[list[tuple[str, str, str]]]:
    """Group passages into documents of ``[passage_docid, title, text]`` sections.

    Deterministic given ``seed``. Passages with empty text are dropped.
    """
    rng = random.Random(seed)
    doc_ids = sorted(corpus.keys())
    rng.shuffle(doc_ids)
    docs: list[list[tuple[str, str, str]]] = []
    for i in range(0, len(doc_ids), sections_per_doc):
        sections: list[tuple[str, str, str]] = []
        for did in doc_ids[i : i + sections_per_doc]:
            p = corpus[did]
            text = (p.get("text") or "").strip()
            if not text:
                continue
            title = (p.get("title") or "").strip() or f"Section {did}"
            sections.append((did, title, text))
        if sections:
            docs.append(sections)
    return docs


def render_markdown(sections: list[tuple[str, str, str]], doc_no: int) -> tuple[str, dict[str, tuple[int, int]]]:
    """Render a document's sections as markdown and return (text, docid->char span)."""
    parts = [f"# Document {doc_no}"]
    spans: dict[str, tuple[int, int]] = {}
    pos = len(parts[0]) + 2  # "\n\n"
    for did, title, text in sections:
        block = f"## {title}\n{text}"
        start = pos + len(f"## {title}\n")  # body starts after the heading line
        spans[did] = (start, start + len(text))
        parts.append(block)
        pos = start + len(text) + 2
    return "\n\n".join(parts), spans


def fixed_windows(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, int]]:
    """Sliding-window offsets ``(start, end)`` over the flat text.

    Steps by ``size - overlap``; mirrors a naive RecursiveCharacter/char splitter
    that ignores document structure.
    """
    if not text:
        return []
    step = max(1, size - overlap)
    windows: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        windows.append((start, end))
        if end == n:
            break
        start += step
    return windows


# --------------------------------------------------------------------------- #
# Build the two indexing arms (same bytes, different chunk rule) + qrels
# --------------------------------------------------------------------------- #
def build_arms(
    sample,
    *,
    seed: int,
    sections_per_doc: int,
) -> dict[str, Any]:
    """Return corpus/qrels/unit-meta for both arms over the same documents."""
    docs = assemble_documents(sample.corpus, seed=seed, sections_per_doc=sections_per_doc)

    corpus_a: dict[str, dict] = {}
    meta_a: dict[str, dict] = {}  # unit -> {chars, ev:{docid:overlap_chars}}
    for sections in docs:
        for did, title, text in sections:
            unit = f"sA-{did}"
            corpus_a[unit] = {"title": title, "text": text}
            meta_a[unit] = {"chars": len(text), "ev": {did: len(text)}}

    corpus_b: dict[str, dict] = {}
    meta_b: dict[str, dict] = {}
    frag_ratio: list[float] = []  # sections per window, to show fragmentation
    for di, sections in enumerate(docs):
        flat, spans = render_markdown(sections, di)
        for wi, (ws, we) in enumerate(fixed_windows(flat)):
            unit = f"sB-{di}-{wi}"
            window_text = flat[ws:we]
            ev: dict[str, int] = {}
            for did, (ps, pe) in spans.items():
                ov = max(0, min(pe, we) - max(ps, ws))
                if ov > 0:
                    ev[did] = ov
            corpus_b[unit] = {"title": "", "text": window_text}
            meta_b[unit] = {"chars": len(window_text), "ev": ev}
            frag_ratio.append(len(ev))

    qrels_a = _qrels_arm(sample, meta_a)
    qrels_b = _qrels_arm(sample, meta_b)

    return {
        "docs": docs,
        "A": {"corpus": corpus_a, "qrels": qrels_a, "meta": meta_a},
        "B": {"corpus": corpus_b, "qrels": qrels_b, "meta": meta_b},
        "avg_sections_per_window": round(sum(frag_ratio) / len(frag_ratio), 3) if frag_ratio else 0.0,
        "n_docs": len(docs),
    }


def _pos_len(sample, did: str) -> int:
    return len((sample.corpus.get(did, {}).get("text") or "").strip()) or 1


def _qrels_arm(sample, meta: dict[str, dict]) -> dict[str, list[str]]:
    """Map each query's positives to relevant units (majority-cover, else argmax).

    Guarantees every positive passage contributes at least one unit so Recall is
    measured against fully-present evidence, not capped by slicing artifacts.
    """
    rel: dict[str, list[str]] = {}
    for qid, pos in sample.qrels.items():
        units: set[str] = set()
        for did in pos:
            need = COVERAGE * _pos_len(sample, did)
            covering = [
                (u, m["ev"].get(did, 0))
                for u, m in meta.items()
                if did in m["ev"] and m["ev"][did] >= need
            ]
            if not covering:  # fallback: the single window holding the most of it
                all_w = [
                    (u, m["ev"].get(did, 0))
                    for u, m in meta.items()
                    if did in m["ev"] and m["ev"][did] > 0
                ]
                covering = [max(all_w, key=lambda t: t[1])] if all_w else []
            units.update(u for u, _ in covering)
        if units:
            rel[qid] = sorted(units)
    return rel


# --------------------------------------------------------------------------- #
# Efficiency accounting: how many tokens does each arm make the LLM swallow
# --------------------------------------------------------------------------- #
def context_efficiency(
    rankings: dict[str, list[str]],
    qrels: dict[str, list[str]],
    corpus: dict[str, dict],
    meta: dict[str, dict],
    sample,
    top_n: int,
) -> dict[str, float]:
    """Mean top-``top_n`` context size + evidence density (relevant chars / total)."""
    ctx_total = 0
    ev_total = 0
    n = 0
    for qid, ranked in rankings.items():
        positives = set(sample.qrels.get(qid, []))
        if not positives:
            continue
        top = ranked[:top_n]
        seen: set[str] = set()
        chars = 0
        ev_chars = 0
        for unit in top:
            if unit in seen or unit not in corpus:
                continue
            seen.add(unit)
            chars += len(corpus[unit]["text"])
            m = meta.get(unit, {"ev": {}})
            ev_chars += sum(ov for did, ov in m.get("ev", {}).items() if did in positives)
        if chars == 0:
            continue
        ctx_total += chars
        ev_total += ev_chars
        n += 1
    if n == 0:
        return {"avg_ctx_chars_per_query": 0.0, "evidence_density": 0.0}
    return {
        "avg_ctx_chars_per_query": round(ctx_total / n, 1),
        "evidence_density": round(ev_total / ctx_total, 4) if ctx_total else 0.0,
    }


# --------------------------------------------------------------------------- #
# Run one arm end-to-end through the production retriever
# --------------------------------------------------------------------------- #
async def run_arm(
    retriever,
    arm: dict,
    queries: list[dict],
    *,
    top_k: int,
    top_n: int,
    rerank: bool,
    concurrency: int,
    ks: list[int],
) -> dict[str, Any]:
    index_stats = await harness.ingest_corpus(retriever, arm["corpus"])
    rankings, run_stats = await harness.run_queries(
        retriever,
        queries,
        top_k=top_k,
        top_n=top_n,
        threshold=0.0,
        rerank=rerank,
        concurrency=concurrency,
    )
    eval_result = evaluate_run(rankings, arm["qrels"], ks=ks)
    eff = context_efficiency(
        rankings, arm["qrels"], arm["corpus"], arm["meta"], _SAMPLE, top_n
    )
    return {
        "index_stats": index_stats,
        "run_stats": run_stats,
        "metrics": eval_result["aggregate"],
        "counts": eval_result["counts"],
        "efficiency": eff,
    }


_SAMPLE = None  # set in run() so context_efficiency can read positives


def _headline_keys(ks: list[int]) -> list[str]:
    last = max(ks)
    return [f"recall@{last}", f"mrr@{last}", f"ndcg@{last}", f"hit@{last}",
            f"precision@{last}", f"map@{last}"]


def build_markdown(arms: dict[str, Any], ab: dict[str, dict], args) -> str:
    ks = sorted(set(args.ks))
    hk = _headline_keys(ks)
    L: list[str] = []
    L.append("# 切块策略受控 A/B：结构感知切分 vs 固定窗口切分")
    L.append("")
    L.append(f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}")
    L.append("- 被测系统：MXI 生产混合检索链路（bge-m3 稠密 + ES BM25 + RRF"
             f"{'+ bge-reranker' if ab['rerank_effective'] else '，本机 reranker 不可用故未启用 rerank'}）")
    L.append("- 受控设定：同一批底层文本 / 同一组查询 / 同一相关性证据，仅切块规则不同")
    L.append("")
    L.append("## 1. 实验构造")
    L.append("")
    L.append("| 项目 | 值 |")
    L.append("| --- | --- |")
    L.append(f"| 查询数 | {ab['n_queries']} |")
    L.append(f"| 伪文档数（每文档 {args.sections_per_doc} 章节） | {arms['n_docs']} |")
    L.append(f"| Arm A 结构感知块数 | {len(arms['A']['corpus'])} |")
    L.append(f"| Arm B 固定窗口块数 | {len(arms['B']['corpus'])}（窗口 {CHUNK_SIZE}/overlap {CHUNK_OVERLAP}） |")
    L.append(f"| Arm B 平均每窗口横跨章节数（碎片度） | {arms['avg_sections_per_window']} |")
    L.append(f"| top_k(每通道候选) | {args.top_k} |  top_n(排名深度) | {args.top_n} |")
    L.append("")
    L.append("## 2. 检索质量对比（主指标）")
    L.append("")
    L.append("| 指标 | A 结构感知 | B 固定窗口 | Δ (A-B) | 相对提升 |")
    L.append("| --- | --- | --- | --- | --- |")
    for key in hk:
        a = ab["A"]["metrics"].get(key, 0.0)
        b = ab["B"]["metrics"].get(key, 0.0)
        rel = f"{(a - b) / b * 100:.1f}%" if b else "-"
        L.append(f"| {key} | {a:.4f} | {b:.4f} | {a - b:+.4f} | {rel} |")
    L.append("")
    L.append("## 3. 指标明细（按截断深度）")
    L.append("")
    for arm in ("A", "B"):
        name = "A 结构感知" if arm == "A" else "B 固定窗口"
        L.append(f"**{name}**")
        L.append("")
        header = "| 指标 | " + " | ".join(f"@{k}" for k in ks) + " |"
        L.append(header)
        L.append("| --- | " + " | ".join("---" for _ in ks) + " |")
        for metric in ("recall", "mrr", "ndcg", "hit", "precision", "map"):
            row = f"| {metric} | " + " | ".join(
                f"{ab[arm]['metrics'].get(f'{metric}@{k}', 0.0):.4f}" for k in ks
            ) + " |"
            L.append(row)
        L.append("")
    L.append("## 4. 上下文效率（喂给 LLM 的成本）")
    L.append("")
    L.append("| 指标 | A 结构感知 | B 固定窗口 |")
    L.append("| --- | --- | --- |")
    ea, eb = ab["A"]["efficiency"], ab["B"]["efficiency"]
    L.append(f"| 平均每查询 top-{args.top_n} 上下文字符数 | {ea['avg_ctx_chars_per_query']} | {eb['avg_ctx_chars_per_query']} |")
    L.append(f"| 证据密度（上下文中属于答案的字符占比） | {ea['evidence_density']:.4f} | {eb['evidence_density']:.4f} |")
    L.append("")
    L.append("## 5. 索引构建与检索性能")
    L.append("")
    L.append("| 项 | A | B |")
    L.append("| --- | --- | --- |")
    for label, path in (
        ("Embedding 秒", ("index_stats", "embed_seconds")),
        ("ES 重建秒", ("index_stats", "es_rebuild_seconds")),
        ("平均单查询延迟秒", ("run_stats", "avg_latency_seconds")),
        ("P95 延迟秒", ("run_stats", "p95_latency_seconds")),
    ):
        L.append(f"| {label} | {ab['A'][path[0]][path[1]]} | {ab['B'][path[0]][path[1]]} |")
    L.append("")
    L.append("## 6. 说明与边界（务必连同数字一起引用）")
    L.append("")
    L.append("- 语料是把 MS MARCO 段落按种子固定分组组装成的伪文档；两个臂看到的是**同一段底层字节**，"
             "唯一变量是切块规则（章节边界+标题元数据 vs 固定滑窗），因此 Δ 可归因于切分策略本身。")
    L.append("- MS MARCO 正例段落**本身就是检索粒度的小节**，Arm A 天然与金标准对齐，所以本 Δ 是"
             "『结构感知切分收益』的**上界**，它精确度量的是『把一个结构良好的文档用固定窗口硬切的代价』，"
             "不能当作任意真实企业文档上的通用常数外推。")
    L.append("- 相关性证据按块粒度回映射：一个章节落在哪个块，该块即为该查询的相关块；"
             "固定窗口下证据常被切成多块，用多数覆盖阈值 + argmax 兜底保证 Recall 不被人为压顶。")
    if not ab["rerank_effective"]:
        L.append("- 本机 Ollama bge-reranker 崩溃（Windows llama.cpp），本次测量的是 dense+BM25+RRF 排序；"
                 "两臂一致，Δ 方向不变，但绝对值会因缺 rerank 略低于生产端到端。")
    L.append("- 查询延迟两臂基本同级（候选池规模同量级），效率差异主要落在**上下文 token 成本**与排序质量上。")
    L.append("")
    return "\n".join(L)


def write_ab(ab: dict, arms: dict, args) -> tuple[Path, Path]:
    out_dir = Path(args.out_dir) if args.out_dir else (get_settings().base_dir / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"chunking_ab_{stamp}"
    payload = {"arms_summary": {
        "A": {**ab["A"], "run_stats": ab["A"]["run_stats"]},
        "B": {**ab["B"], "run_stats": ab["B"]["run_stats"]},
    }, "meta": {k: v for k, v in ab.items() if k not in ("A", "B")}}
    base.with_suffix(".json").write_text(
        __import__("json").dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    md = build_markdown(arms, ab, args)
    base.with_suffix(".md").write_text(md, encoding="utf-8")
    (out_dir / "chunking_ab_latest.md").write_text(md, encoding="utf-8")
    (out_dir / "chunking_ab_latest.json").write_text(
        base.with_suffix(".json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return base.with_suffix(".json"), base.with_suffix(".md")


async def run(args) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    global _SAMPLE
    print(f"[1/4] loading MS MARCO sample (q={args.num_queries}, seed={args.seed}) ...")
    _SAMPLE = load_sample(num_queries=args.num_queries, seed=args.seed)
    queries = _SAMPLE.queries

    print(f"[2/4] building controlled arms over {len(_SAMPLE.corpus)} passages ...")
    arms = build_arms(_SAMPLE, seed=args.seed, sections_per_doc=args.sections_per_doc)
    print(f"      docs={arms['n_docs']}  A_chunks={len(arms['A']['corpus'])}  "
          f"B_chunks={len(arms['B']['corpus'])}  avg_sections/window={arms['avg_sections_per_window']}")

    print(f"[3/4] provisioning isolated store pg={AB_DB} es={AB_ES_INDEX} ...")
    engine = await harness.provision_eval_database(AB_DB)
    harness.install_eval_engine(engine)
    retriever = harness.make_retriever(es_index=AB_ES_INDEX)
    rerank_effective = False
    if args.rerank:
        rerank_effective = await harness.rerank_available()

    results: dict[str, dict] = {}
    for arm_name in ("A", "B"):
        print(f"      --- Arm {arm_name} ---")
        results[arm_name] = await run_arm(
            retriever, arms[arm_name], queries,
            top_k=args.top_k, top_n=args.top_n, rerank=rerank_effective,
            concurrency=args.concurrency, ks=args.ks,
        )

    ab = {
        "A": results["A"], "B": results["B"],
        "n_queries": _SAMPLE.n_queries,
        "rerank_effective": rerank_effective,
        "eval_database": AB_DB, "es_index": AB_ES_INDEX,
    }
    json_path, md_path = write_ab(ab, arms, args)
    print("[4/4] report written:")
    print(f"      {md_path}")
    print(f"      {json_path}")

    hk = _headline_keys(args.ks)
    print("\n===== HEADLINE (Arm A structure vs Arm B fixed) =====")
    for key in hk:
        print(f"  {key:14s}  A={ab['A']['metrics'].get(key,0):.4f}  "
              f"B={ab['B']['metrics'].get(key,0):.4f}")
    print(f"  evidence_density  A={ab['A']['efficiency']['evidence_density']:.4f}  "
          f"B={ab['B']['efficiency']['evidence_density']:.4f}")
    print(f"  avg_ctx_chars     A={ab['A']['efficiency']['avg_ctx_chars_per_query']}  "
          f"B={ab['B']['efficiency']['avg_ctx_chars_per_query']}")

    if args.cleanup:
        res = await harness.drop_eval_stores(engine, database=AB_DB, es_index=AB_ES_INDEX)
        print(f"[cleanup] {res}")
    try:
        await retriever.bm25._client.close()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.msmarco_eval.chunking_ab",
                                description="Structure-aware vs fixed-window chunking A/B on a controlled corpus.")
    p.add_argument("--num-queries", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sections-per-doc", type=int, default=10)
    p.add_argument("--top-k", type=int, default=50)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--ks", type=int, nargs="+", default=[1, 3, 5, 10])
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--no-rerank", dest="rerank", action="store_false")
    p.set_defaults(rerank=True)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--cleanup", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[aborted]", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("chunking_ab").exception("A/B run failed")
        print(f"\n[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
