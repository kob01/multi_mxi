"""Rerank 冒烟验证: 真 cross-encoder 是否接上、分数是否落在预期标度。

只读检索, 不改任何数据。替代 Ollama 伪 rerank(拿 /api/embed 向量做 cosine)后,
用它确认三件事:

  1) TEI ``/rerank`` 连通性与打分方向(probe: 相关文本确实排在无关文本之前);
  2) 每条查询的 ``score_mode``、逐块 0~1 相关性分、阈值裁剪前后的条数、单查询耗时
     —— 降级回 RRF 时分数标度是 1e-2 量级的秩分, 一眼可辨;
  3) 完全无关的查询应被阈值裁空, 即链路具备"明确拒答"的能力而不是硬凑上下文。

Usage:
    python -m scripts.smoke_rerank                     # 用内置查询
    python -m scripts.smoke_rerank --threshold 0.3     # 试算别的阈值取点
    python -m scripts.smoke_rerank --queries "报销额度" "滑行教程"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from app.config import get_settings
from app.rag.reranker import close_reranker_client, get_reranker
from app.rag.retriever import HybridRetriever

logger = logging.getLogger("smoke_rerank")

# 覆盖知识库主要来源(HR/财务政策、滑行教程)与一条必不相关的对照组。
DEFAULT_QUERIES = [
    "员工报销单怎么提交，需要哪些附件",
    "差旅住宿标准是多少",
    "技术滑行 平行转弯 练习方法",
    "泰坦尼克号是什么时候沉没的",  # 对照组: 知识库里没有, 期望被阈值裁空
]


async def _one(
    retriever: HybridRetriever, query: str, threshold: float
) -> tuple[str, int, float]:
    """跑一条查询并打印逐块分数; 返回 (score_mode, 阈值后条数, 耗时秒)。"""
    settings = retriever._settings  # noqa: SLF001
    orig = settings.retrieval_score_threshold
    settings.retrieval_score_threshold = 0.0  # 先看全量排序, 再按本次试验阈值本地裁剪
    start = time.perf_counter()
    try:
        chunks, score_mode = await retriever.retrieve(query)
    finally:
        settings.retrieval_score_threshold = orig
    dt = time.perf_counter() - start

    kept = [c for c in chunks if c.score >= threshold]
    # 仅 rerank 模式适用相关性阈值; 降级到 RRF 时分数是 1/(60+rank) 量级的秩分,
    # 生产链路不对它做裁剪(见 HybridRetriever.retrieve), 此处不能拿阈值去截。
    in_rrf = score_mode == "rrf"
    print(f"\n查询: {query}")
    print(f"  score_mode={score_mode}  候选={len(chunks)}  "
          + ("阈值不适用(RRF 降级序)" if in_rrf else f"阈值({threshold})后={len(kept)}")
          + f"  耗时={dt:.2f}s")
    for i, c in enumerate(chunks, 1):
        mark = "  " if in_rrf or c.score >= threshold else "× "
        print(f"  {mark}{i}. score={c.score:.4f}  {c.chunk_id[:28]}  {c.title[:24]}")
    return score_mode, (len(chunks) if in_rrf else len(kept)), dt


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    if args.top_k:
        settings.rag_top_k = args.top_k
    if args.top_n:
        settings.rerank_top_n = args.top_n

    print(f"TEI rerank 地址: {settings.tei_rerank_url}")
    print(f"总开关 rerank_enabled={settings.rerank_enabled}  "
          f"生产阈值={settings.retrieval_score_threshold}  本次试算阈值={args.threshold}")

    # 本脚本直调 retrieve() 不经 Retrieval Cache, 但仍先清一次: 随后走 Web 问答时
    # 若命中改造前写入的旧 cosine 标度缓存, 会被误判成"新 rerank 分数异常"。
    from app.cache.retrieval_cache import invalidate_all

    await invalidate_all()
    print("已清空 Retrieval Cache (避免读到改造前的旧分数标度)")

    if not await get_reranker().probe():
        print("\n[FAIL] TEI /rerank 探测失败 —— 检索会逐条降级 RRF。")
        print("       先看容器状态: docker compose -f docker/docker-compose.yml logs tei-rerank")

    retriever = HybridRetriever()
    if args.rebuild_bm25:
        # 本脚本直接构 HybridRetriever, 不经 graph 的惰性重建; ES 索引为空时稀疏通道会默默空转。
        print("重建 ES BM25 索引 (--rebuild-bm25) ...")
        await retriever.rebuild_bm25()

    modes: dict[str, int] = {}
    reranked_hits = 0
    try:
        for query in args.queries:
            mode, kept, dt = await _one(retriever, query, args.threshold)
            modes[mode] = modes.get(mode, 0) + 1
            if mode == "rerank" and kept:
                reranked_hits += 1
            if dt > 5:
                print(f"  [WARN] 单查询 {dt:.2f}s, 疑似在撞 rerank 超时(应 ≤{settings.rerank_timeout}s)")
    finally:
        await close_reranker_client()
        try:
            await retriever.bm25._client.close()  # noqa: SLF001 - 避 aiohttp 'Unclosed client session'
        except Exception:  # noqa: BLE001
            pass

    print(f"\n===== 汇总 =====\n  score_mode 分布: {modes}")
    print(f"  按 rerank 分数命中(>0 块通过阈值)的查询数: {reranked_hits}/{len(args.queries)}")
    if modes.get("rrf") and not modes.get("rerank"):
        print("  [FAIL] 全部查询都降级到了 RRF, rerank 未生效")
        return 1
    print("  [OK] rerank 链路可用(无关查询被裁空属预期行为)")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m scripts.smoke_rerank")
    p.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES, help="待验证查询")
    p.add_argument("--threshold", type=float, default=0.4, help="本次试算的相关性阈值")
    p.add_argument("--top-k", type=int, default=0, help="每通道候选数(0=用配置值)")
    p.add_argument("--top-n", type=int, default=0, help="重排后保留数(0=用配置值)")
    p.add_argument("--rebuild-bm25", action="store_true", help="先全量重建 ES 稀疏索引(刚迁移完/索引为空时用)")
    p.add_argument("--log-level", default="WARNING", help="INFO 可看降级/裁剪明细")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
