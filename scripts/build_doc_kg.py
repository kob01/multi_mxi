"""文档知识图谱一次性回填脚本。

为数据库中所有已入库文档从 MongoDB ``doc_bodies``(head) 取正文抽取实体关系并写入
Neo4j 文档图谱(``:KgDoc``/``:KgEntity``)。用于开启 ``doc_kg_enabled`` 后把存量文档灌入
图谱, 无需重新上传; 新文档会在入库时自动建图, 不必再跑本脚本。

需先启用图谱(``DOC_KG_ENABLED=true``)且 Neo4j 可达; PG 密码按 db.session 的既有
方式从环境变量 / .env / secrets 解析。

Usage:
    python -m scripts.build_doc_kg [--limit 50]
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.db.session import get_engine
from app.kg import service, store


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill document knowledge graph")
    parser.add_argument("--limit", type=int, default=None, help="只回填最近 N 篇文档")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.doc_kg_enabled:
        raise SystemExit(
            "doc_kg_enabled=false: 请先设环境变量 DOC_KG_ENABLED=true 再运行本脚本"
        )

    get_engine()  # 触发 PG 连接/密码解析(与 init_db 一致)
    await store.ensure_schema()

    print("[kg] backfilling document knowledge graph ...")
    report = await service.rebuild_all(limit_docs=args.limit)
    print(f"[kg] done: built {report.get('built')}/{report.get('total')} documents")


if __name__ == "__main__":
    asyncio.run(main())
