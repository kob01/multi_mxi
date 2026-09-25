"""MS MARCO Passage Ranking sample loader for RAG retrieval evaluation.

Data source: ``Tevatron/msmarco-passage`` (an Apache-2.0 repackaging of the
official Microsoft MS MARCO passage-ranking dataset). Its ``train.jsonl.gz``
split is *self-contained*: every record carries the query text, its labelled
positive passage(s) and ~30 BM25 hard negatives, each as ``{docid, title,
text}``. That lets us assemble a realistic retrieval benchmark (rank the
labelled-relevant passage above a pool of lexical near-miss distractors)
without pulling the full 8.8M-passage corpus.

Why not the official URLs
--------------------------
The Microsoft Azure blob host (``msmarco.blob.core.windows.net``) now returns
``PublicAccessNotPermitted`` and ``huggingface.co`` is unreachable from this
network, so we stream from the ``hf-mirror.com`` mirror and read only the
first ``head_mb`` megabytes of the gzip member (enough for thousands of
records) via an HTTP Range request / early stream break.

Caching (all under ``data/msmarco/``)
--------------------------------------
* ``train_head.gz``         - the truncated raw download (survives across runs)
* ``sample_q<N>_s<seed>.json`` - the built sample (queries + corpus + qrels),
  so a re-run with the same parameters is byte-for-byte reproducible and fast.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import random
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Mirror first; huggingface.co is blocked on CN networks. Tried in order.
MIRROR_URLS: list[str] = [
    "https://hf-mirror.com/datasets/Tevatron/msmarco-passage/resolve/main/train.jsonl.gz",
    "https://huggingface.co/datasets/Tevatron/msmarco-passage/resolve/main/train.jsonl.gz",
]
DATASET_NAME = "Tevatron/msmarco-passage (MS MARCO Passage Ranking)"
DEFAULT_HEAD_MB = 20  # ~ a few thousand records: plenty to sample from


@dataclass
class MSMarcoSample:
    """A self-contained MS MARCO retrieval benchmark sample.

    Attributes:
        queries: ordered list of ``{"query_id", "query"}``.
        corpus:  ``docid -> {"title", "text"}`` for every passage that appears
            as a positive OR a negative of the sampled queries (the pool the
            retriever has to search).
        qrels:   ``query_id -> [relevant docids]`` (the positives only).
        meta:    provenance / build info (source, sizes, seed, ...).
    """

    queries: list[dict]
    corpus: dict[str, dict]
    qrels: dict[str, list[str]]
    meta: dict = field(default_factory=dict)

    @property
    def n_queries(self) -> int:
        return len(self.queries)

    @property
    def n_corpus(self) -> int:
        return len(self.corpus)

    def summary(self) -> dict:
        n_rel = [len(self.qrels.get(q["query_id"], [])) for q in self.queries]
        return {
            "queries": self.n_queries,
            "corpus_passages": self.n_corpus,
            "relevant_per_query_avg": round(sum(n_rel) / len(n_rel), 3) if n_rel else 0.0,
            "source": self.meta.get("source", DATASET_NAME),
        }


def cache_dir() -> Path:
    """Return (creating if needed) the project-local MS MARCO cache dir."""
    d = get_settings().base_dir / "data" / "msmarco"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_head(dest: Path, max_bytes: int, force: bool = False) -> Path:
    """Stream at most ``max_bytes`` of the gzip member into ``dest``.

    Idempotent: an existing non-empty ``dest`` is reused unless ``force``.
    Tries each mirror in order and raises only if all fail.
    """
    if dest.exists() and dest.stat().st_size > 0 and not force:
        logger.info("[msmarco] using cached download %s (%d bytes)", dest, dest.stat().st_size)
        return dest
    last_err: Exception | None = None
    for url in MIRROR_URLS:
        try:
            logger.info("[msmarco] streaming %s (cap=%d MiB)", url, max_bytes // (1024 * 1024))
            got = 0
            with httpx.Client(follow_redirects=True, timeout=180.0) as client:
                with client.stream(
                    "GET", url, headers={"Range": f"bytes=0-{max_bytes - 1}"}
                ) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(65536):
                            if not chunk:
                                continue
                            f.write(chunk)
                            got += len(chunk)
                            if got >= max_bytes:
                                break
            if dest.stat().st_size == 0:
                raise RuntimeError("empty response body")
            logger.info("[msmarco] saved %d bytes -> %s", got, dest)
            return dest
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            last_err = exc
            logger.warning("[msmarco] mirror failed (%s): %s", url, exc)
    raise RuntimeError(f"all MS MARCO mirrors failed; last error: {last_err}")


def iter_records(path: Path) -> Iterator[dict]:
    """Yield fully-parsed JSON records from a (possibly truncated) .jsonl.gz.

    A Range-limited download ends mid-gzip-stream, so decompression stops at
    the truncation point; ``zlib`` returns every decodable byte without
    raising and we simply drop the final (incomplete) line.
    """
    raw = path.read_bytes()
    try:
        data = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    except Exception:  # noqa: BLE001 - truncated tail: fall back to lenient zlib
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            data = dec.decompress(raw)
        except zlib.error as exc:
            raise RuntimeError(f"cannot decompress {path}: {exc}") from exc
    for line in data.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated last record from the partial download
        if isinstance(rec, dict) and rec.get("positive_passages"):
            yield rec


def load_sample(
    num_queries: int = 200,
    seed: int = 42,
    head_mb: int = DEFAULT_HEAD_MB,
    head_path: Path | None = None,
    force_download: bool = False,
    persist: bool = True,
) -> MSMarcoSample:
    """Build (or load cached) an MS MARCO retrieval sample.

    Args:
        num_queries: how many queries to keep in the sample.
        seed: RNG seed for reproducible sampling.
        head_mb: cap for the raw download when a fresh head is needed.
        head_path: override for the cached ``train_head.gz`` location.
        force_download: re-download even if the head cache exists.
        persist: write the built sample to the on-disk sample cache.

    Returns:
        An :class:`MSMarcoSample` with queries, the candidate corpus and qrels.
    """
    cdir = cache_dir()
    sample_cache = cdir / f"sample_q{num_queries}_s{seed}.json"
    if sample_cache.exists() and not force_download:
        obj = json.loads(sample_cache.read_text(encoding="utf-8"))
        logger.info("[msmarco] loaded cached sample %s", sample_cache.name)
        return MSMarcoSample(
            queries=obj["queries"],
            corpus=obj["corpus"],
            qrels=obj["qrels"],
            meta=obj.get("meta", {}),
        )

    head = Path(head_path) if head_path else cdir / "train_head.gz"
    download_head(head, head_mb * 1024 * 1024, force=force_download)
    records = list(iter_records(head))
    if not records:
        raise RuntimeError(
            f"no complete records parsed from {head}; increase --head-mb"
        )
    rng = random.Random(seed)
    rng.shuffle(records)
    chosen = records[:num_queries]
    if len(chosen) < num_queries:
        logger.warning(
            "[msmarco] only %d records available (< requested %d); "
            "increase --head-mb for a larger pool",
            len(chosen), num_queries,
        )

    queries: list[dict] = []
    corpus: dict[str, dict] = {}
    qrels: dict[str, list[str]] = {}
    for rec in chosen:
        qid = str(rec["query_id"])
        qtext = str(rec.get("query", "")).strip()
        if not qtext:
            continue
        rel: list[str] = []
        for pos in rec["positive_passages"]:
            did = str(pos["docid"])
            corpus[did] = {
                "title": str(pos.get("title", "")).strip(),
                "text": str(pos.get("text", "")).strip(),
            }
            rel.append(did)
        for neg in rec.get("negative_passages", []):
            did = str(neg["docid"])
            corpus.setdefault(
                did,
                {
                    "title": str(neg.get("title", "")).strip(),
                    "text": str(neg.get("text", "")).strip(),
                },
            )
        if not rel:
            continue
        queries.append({"query_id": qid, "query": qtext})
        qrels[qid] = sorted(set(rel))

    sample = MSMarcoSample(
        queries=queries,
        corpus=corpus,
        qrels=qrels,
        meta={
            "source": DATASET_NAME,
            "url": MIRROR_URLS[0],
            "seed": seed,
            "requested_queries": num_queries,
            "head_mb": head_mb,
            "records_parsed": len(records),
        },
    )
    if persist:
        sample_cache.write_text(
            json.dumps(
                {
                    "queries": sample.queries,
                    "corpus": sample.corpus,
                    "qrels": sample.qrels,
                    "meta": sample.meta,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info("[msmarco] wrote sample cache %s", sample_cache.name)
    return sample
