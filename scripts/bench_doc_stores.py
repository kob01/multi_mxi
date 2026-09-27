"""父子双表规模基准: 验证"数据量大不明显下降"。

**只写独立评测库 (mxi_scale_bench 的 PG 库 + Mongo 库), 绝不碰生产数据。**

合成 1k/10k/50k/100k 子块(随机单位向量 + 平均 ~600 字 chunk_text, 父块 1:4), 每档测
三项 p50/p95:
  (a) 窄列 ANN TopK            —— 新路径 (ChunkStore.search, NARROW_COLUMNS)
  (a') 整实体 ANN TopK         —— 对照组 (select(DocChunkRow, dist), 宽行进入排序扫描)
  (b) ChunkStore.get_texts(8)  —— 主键点查, 应与语料规模近似无关
  (c) BodyStore.get_parent_texts(4) —— Mongo _id $in, 应与语料规模近似无关

验收: (a) 的 p95 随 1k->100k 增长亚线性且不高于 (a'); (b)(c) 近似水平线。

Usage:
    python -m scripts.bench_doc_stores --scales 1000,10000,50000,100000 [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import time
from typing import Sequence

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.bodies.store import ParentTextItem, get_body_store
from app.config import get_settings
from app.db.models import DocChunkRow, DocParentRow
from app.rag.vectorstore import ChunkStore
from scripts.msmarco_eval import harness

logger = logging.getLogger("bench_doc_stores")

BENCH_DB = "mxi_scale_bench"
BENCH_MONGO_DB = "mxi_scale_bench"
BENCH_ES_INDEX = "scale_bench_unused"
TOP_K = 50


_RNG = random.Random(7)


def _rand_vector(dim: int) -> list[float]:
    v = [_RNG.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _synth(doc_count: int, children: int, dim: int) -> tuple[list[dict], list[dict], list[ParentTextItem]]:
    """生成 children 条子块(每 4 子块一个父块) + 父块行 + Mongo 父块正文。"""
    rng = random.Random(42)
    body_chars = "知滑行教程报销政策员工手册财务制度技术文档内容语料基准测试数据样本"
    chunk_rows: list[dict] = []
    parent_rows: list[dict] = []
    parent_items: list[ParentTextItem] = []
    parent_every = 4
    pseq = 0
    for i in range(children):
        if i % parent_every == 0:
            pseq += 1
            doc_id = f"bench{pseq % doc_count:06d}"
            parent_id = f"{doc_id}-p{pseq:04d}"
            ptext = "".join(rng.choice(body_chars) for _ in range(rng.randint(1200, 2400)))
            parent_rows.append(
                {
                    "parent_id": parent_id, "doc_id": doc_id, "ord": pseq, "parent_type": "section",
                    "title": "bench", "section": "", "page_no": -1, "start_offset": 0,
                    "end_offset": len(ptext), "content_hash": "0" * 16, "char_count": len(ptext),
                    "child_count": parent_every, "normalizer_version": "n1",
                    "visibility": "public", "owner_id": "", "dept_id": "", "allowed_roles": "",
                    "extra": {},
                }
            )
            parent_items.append(
                ParentTextItem(parent_id=parent_id, doc_id=doc_id, text=ptext, anchor={},
                               start_offset=0, end_offset=len(ptext), content_hash="0" * 16)
            )
        ctext = "".join(rng.choice(body_chars) for _ in range(rng.randint(400, 800)))
        chunk_rows.append(
            {
                "chunk_id": f"bench-p{pseq:04d}-c{i % parent_every:02d}-{i:08d}"[:80],
                "doc_id": parent_rows[-1]["doc_id"], "parent_id": parent_rows[-1]["parent_id"],
                "chunk_index": i % parent_every, "ord": i, "chunk_text": ctext,
                "content_hash": "0" * 16, "char_count": len(ctext),
                "embedding_model": get_settings().embedding_model, "title": "bench",
                "source": "bench", "modality": "text", "section": "", "page_no": -1,
                "visibility": "public", "owner_id": "", "dept_id": "", "allowed_roles": "",
                "extra": {}, "embedding": _rand_vector(dim),
            }
        )
    return chunk_rows, parent_rows, parent_items


async def _load(store: ChunkStore, chunk_rows: Sequence[dict], parent_rows: Sequence[dict]) -> None:
    batch = 1000
    async with store._sessions()() as session:  # noqa: SLF001
        async with session.begin():
            for start in range(0, len(parent_rows), batch):
                await session.execute(pg_insert(DocParentRow).values(list(parent_rows[start : start + batch])))
            for start in range(0, len(chunk_rows), batch):
                await session.execute(pg_insert(DocChunkRow).values(list(chunk_rows[start : start + batch])))


def _p(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return round(s[max(0, int(len(s) * q) - 1)], 3)


async def _measure(store: ChunkStore, queries: list[list[float]], chunk_ids: list[str]) -> dict:
    dim = get_settings().embedding_dim
    ann_new: list[float] = []
    ann_wide: list[float] = []
    gettexts: list[float] = []
    for qv in queries:
        t = time.perf_counter()
        await store.search(qv, TOP_K, principal=None)
        ann_new.append((time.perf_counter() - t) * 1000)
        # 对照: 整实体宽行进入排序扫描(旧链路的读放大)
        dist = DocChunkRow.embedding.cosine_distance(qv)
        t = time.perf_counter()
        async with store._sessions()() as session:  # noqa: SLF001
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {max(100, TOP_K * 8)}"))
            await session.execute(select(DocChunkRow, dist).order_by(dist).limit(TOP_K))
            await session.rollback()
        ann_wide.append((time.perf_counter() - t) * 1000)
        keys = chunk_ids[:8]
        t = time.perf_counter()
        await store.get_texts(keys)
        gettexts.append((time.perf_counter() - t) * 1000)
    return {
        "ann_narrow_ms": {"p50": _p(ann_new, 0.5), "p95": _p(ann_new, 0.95)},
        "ann_wide_control_ms": {"p50": _p(ann_wide, 0.5), "p95": _p(ann_wide, 0.95)},
        "get_texts_ms": {"p50": _p(gettexts, 0.5), "p95": _p(gettexts, 0.95)},
    }


async def _measure_parents(parent_ids: list[str]) -> dict:
    bodies = get_body_store()
    xs: list[float] = []
    for _ in range(20):
        t = time.perf_counter()
        await bodies.get_parent_texts(parent_ids[:4])
        xs.append((time.perf_counter() - t) * 1000)
    return {"get_parent_texts_ms": {"p50": _p(xs, 0.5), "p95": _p(xs, 0.95)}}


async def bench(scales: list[int], keep: bool) -> None:
    settings = get_settings()
    engine = await harness.provision_eval_database(BENCH_DB)
    harness.install_eval_engine(engine)
    harness.install_eval_mongo(BENCH_MONGO_DB)
    store = ChunkStore()
    bodies = get_body_store()
    dim = settings.embedding_dim
    n_queries = 30
    for children in scales:
        doc_count = max(1, children // 20)
        chunk_rows, parent_rows, parent_items = _synth(doc_count, children, dim)
        # 清档
        async with store._sessions()() as session:  # noqa: SLF001
            async with session.begin():
                await session.execute(delete(DocChunkRow))
                await session.execute(delete(DocParentRow))
        await bodies._db["parent_texts"].delete_many({})  # noqa: SLF001
        t0 = time.perf_counter()
        await _load(store, chunk_rows, parent_rows)
        await bodies.save_parent_texts(parent_items)
        load_s = round(time.perf_counter() - t0, 1)
        queries = [_rand_vector(dim) for _ in range(n_queries)]
        chunk_ids = [r["chunk_id"] for r in chunk_rows]
        parent_ids = [r["parent_id"] for r in parent_rows]
        res = await _measure(store, queries, chunk_ids)
        res |= await _measure_parents(parent_ids)
        total = int((await store.counts())["chunks"])
        print(
            f"[bench] children={total} load={load_s}s "
            f"ann_new={res['ann_narrow_ms']} ann_wide={res['ann_wide_control_ms']} "
            f"get_texts={res['get_texts_ms']} get_parents={res['get_parent_texts_ms']}"
        )
    await engine.dispose()
    if not keep:
        await harness.drop_eval_stores(
            engine, database=BENCH_DB, es_index=BENCH_ES_INDEX,
            drop_database=True, drop_eval_mongo=True, mongo_database=BENCH_MONGO_DB,
        )
        print("[bench] dropped mxi_scale_bench (PG + Mongo)")
    else:
        print("[bench] --keep: 保留 mxi_scale_bench 供人工复查")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="父子双表规模基准 (只写独立评测库)")
    parser.add_argument("--scales", default="1000,10000,50000,100000",
                        help="逗号分隔的子块规模档位")
    parser.add_argument("--keep", action="store_true", help="跑完保留评测库便于复查")
    args = parser.parse_args()
    scales = [int(s) for s in args.scales.split(",") if s.strip()]
    await bench(scales, args.keep)


if __name__ == "__main__":
    asyncio.run(main())
