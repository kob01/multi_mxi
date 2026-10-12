"""运维脚本: 手工全量重建 ES BM25 索引(入库链路已不再自动做这件事)。

为什么要有独立的脚本而不是"入库时顺手重建": 全量重建的代价是"从 PG 流式读出全部子块 +
逐条 jieba 分词 + bulk 进 ES", 与语料量成正比。把它挂在入库路径上, 文档一多就会变成
"每入库一篇就把全库重写一遍"的成本爆炸。现在常态是单文档增量写
(``app/docs/pipeline.py::index_doc``), 全量重建只在这些场合需要:

- ES 索引被清空/损坏, 或换了索引名/分词口径;
- 存储层迁移/批量修数据后需要让派生索引与 PG 重新对齐;
- 新增/重建 ``doc_chunks`` 后需要一次性灌语料。

必须在容器里跑(唯一验证环境):
    docker compose -f docker/docker-compose.yml exec assistant python -m scripts.rebuild_bm25 --dry-run
    docker compose -f docker/docker-compose.yml exec assistant python -m scripts.rebuild_bm25
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.rag.bm25 import close_es_client, get_es_bm25
from app.rag.vectorstore import ChunkStore


async def _count_live_chunks() -> int:
    return (await ChunkStore().counts())["chunks"]


async def main(dry_run: bool) -> int:
    settings = get_settings()
    store = ChunkStore()
    bm25 = get_es_bm25()
    live = await _count_live_chunks()
    before = await bm25.count()
    print(f"[rebuild_bm25] es_url={settings.es_url} index={settings.es_index}")
    print(f"[rebuild_bm25] PG 现行子块 {live} 条; ES 现有 {before} 条")
    if live == 0:
        print("[rebuild_bm25] PG 里没有可索引的子块, 不做任何事(避免把 ES 清空)")
        return 0
    if dry_run:
        print("[rebuild_bm25] --dry-run: 未执行重建")
        return 0
    await bm25.rebuild(store.iter_chunk_corpus())
    after = await bm25.count()
    print(f"[rebuild_bm25] 重建完成: ES {before} -> {after} 条(应与 PG 现行子块数一致)")
    if after != live:
        print(
            "[rebuild_bm25] 提示: ES 与 PG 条数不等。常见原因是刚发布但 indexing 阶段尚未跑完的"
            "增量写, 或存在待 GC 的陈旧行(陈旧行不进语料源, 不应计入差异)。"
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="手工全量重建 ES BM25 索引")
    parser.add_argument(
        "--dry-run", action="store_true", help="只报 PG/ES 两边条数, 不执行重建"
    )
    args = parser.parse_args()
    try:
        code = asyncio.run(main(args.dry_run))
    finally:
        asyncio.run(close_es_client())
    raise SystemExit(code)
