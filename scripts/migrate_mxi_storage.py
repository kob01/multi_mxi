"""一次性存储层迁移: MySQL + Milvus Lite -> PostgreSQL (+pgvector).

用法:
    # 1) 先起 PG 并建好 schema
    uv run python -m scripts.init_db

    # 2) 搬数据 (源 MySQL DSN 从环境变量 MYSQL_URL 或 --mysql-url 传入)
    uv run python -m scripts.migrate_mxi_storage \
        --mysql-url "mysql+pymysql://user:pw@1.2.3.4:3306/dbname?charset=utf8mb4" \
        --milvus-uri ./data/milvus_lite.db

    # 只搬业务/元数据, 向量改为重新入库 (不需要 pymilvus)
    uv run python -m scripts.migrate_mxi_storage --mysql-url ... --skip-vectors

设计:
- 目标表由 ``init_schema()`` 建好 (含 pgvector 扩展), 本脚本只搬数据, 不建表。
- 元数据/业务表逐表批量 INSERT, 主键/唯一键冲突时 ``ON CONFLICT DO NOTHING``,
  因此可安全重跑 (幂等)。
- 向量按 ``documents.doc_key`` 逐文档从 Milvus query 出来后整行写入
  ``knowledge_chunks``, 避免一次性拉全量 (Milvus query 单次有上限); 单文档命中
  上限时会打 WARNING, 此时建议改用 --skip-vectors + 重新入库。
- naive datetime 按 UTC 解释: 原 MySQL 列是无时区 timestamp, 写入值来自应用本地
  时钟; 若你的历史数据实际是别的时区, 改 ``_SOURCE_TZ_OFFSET_HOURS``。
- 表名/列名全部来自 ORM 元数据, 不做字符串拼接 DDL。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import BigInteger, create_engine, inspect, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app.db.models import (
    DepartmentBudget,
    Document,
    DocumentTag,
    Employee,
    HRTicket,
    KnowledgeChunkRow,
    LeaveRecord,
    Reimbursement,
    Tag,
)
from app.db.session import get_session_factory, init_schema

# 迁移顺序: 先父表后子表 (documents -> tags -> document_tags -> 业务表)
METADATA_MODELS: tuple[type, ...] = (
    Document,
    Tag,
    DocumentTag,
    Employee,
    HRTicket,
    LeaveRecord,
    Reimbursement,
    DepartmentBudget,
)

# 原 MySQL 无时区列 -> timestamptz 的偏移假设 (小时)。0 = 历史值本来就是 UTC。
_SOURCE_TZ_OFFSET_HOURS = 0

# 与旧 Milvus collection schema 一致的输出字段 (embedding 单独追加)。
CHUNK_FIELDS = [
    "chunk_id",
    "doc_id",
    "title",
    "content",
    "source",
    "modality",
    "parent_id",
    "is_parent",
    "page_no",
    "section",
    "visibility",
    "owner_id",
    "dept_id",
    "allowed_roles",
]
_MILVUS_PAGE_LIMIT = 16384  # Milvus query 单次返回上限


def _normalize(value: Any) -> Any:
    """MySQL 返回值 -> PostgreSQL 可绑定值 (主要是给 naive datetime 补时区)。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone(timedelta(hours=_SOURCE_TZ_OFFSET_HOURS)))
        return value
    if isinstance(value, (date, time, Decimal, str, bytes, int, float, bool)) or value is None:
        return value
    return str(value)


def source_engine(url: str) -> Engine:
    """Build the read-only source MySQL engine (pymysql, sync)."""
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 15})


def _surrogate_pk(table) -> str | None:
    """单列自增主键名 (如 id); 自然主键 (emp_id / ticket_no / order_no) 不算。"""
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) != 1:
        return None
    col = pk_cols[0]
    is_int = isinstance(col.type, BigInteger) or str(col.type).upper().startswith(("BIGINT", "INT", "SERIAL"))
    return col.name if col.autoincrement in (True, "auto") and is_int else None


def _table_rows(src: Engine, model) -> list[dict[str, Any]]:
    """Read a whole source table, keeping only columns the target model knows.

    自增 id 原样搬 (不重新发号): ``document_tags.tag_id`` 这类列引用的是
    ``tags.id`` 的旧值, 一旦重新发号关联关系就全乱了; 数据灌完再统一
    用 setval 把序列推到 max(id) 之后。
    """
    table_name = model.__tablename__
    table = model.__table__
    if not inspect(src).has_table(table_name):
        print(f"[migrate] 源库无 {table_name} 表, 跳过")
        return []
    wanted = {c.name for c in table.columns}
    with src.connect() as conn:
        raw = conn.execute(text(f"SELECT * FROM {table_name}")).mappings().all()
    return [{k: _normalize(v) for k, v in row.items() if k in wanted} for row in raw]


async def migrate_metadata(src: Engine, batch: int) -> None:
    """Copy metadata / business tables; idempotent via ON CONFLICT (pk) DO NOTHING.

    每张表灌完后的 ``setval`` 保证后续业务写入 (如新文档入库) 的主键不会撞上
    已迁移的旧 id。
    """
    factory = get_session_factory()
    for model in METADATA_MODELS:
        table = model.__table__
        table_name = model.__tablename__
        rows = _table_rows(src, model)
        if not rows:
            print(f"[migrate] {table_name}: 0 行")
            continue
        pk_cols = [c.name for c in table.primary_key.columns]
        async with factory() as session:
            async with session.begin():
                for start in range(0, len(rows), batch):
                    await session.execute(
                        insert(model)
                        .values(rows[start : start + batch])
                        .on_conflict_do_nothing(index_elements=pk_cols)
                    )
                surrogate = _surrogate_pk(table)
                if surrogate:
                    await session.execute(
                        text(
                            "SELECT setval(pg_get_serial_sequence(:t, :c), "
                            f"COALESCE((SELECT MAX(\"{surrogate}\") FROM {table_name}), 1))"
                        ),
                        {"t": table_name, "c": surrogate},
                    )
        print(f"[migrate] {table_name}: {len(rows)} 行 -> PostgreSQL")


def _milvus_client(uri: str):
    """Open the legacy Milvus Lite db file (pymilvus is optional after migration)."""
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:  # pragma: no cover - post-migration environments
        raise SystemExit(
            "向量迁移需要 pymilvus 读取旧 collection; 已卸载则请改用 --skip-vectors "
            "并执行 scripts.ingest_knowledge 重新入库"
        ) from exc
    return MilvusClient(uri=uri)


async def migrate_vectors(client: Any, collection: str, batch: int) -> None:
    """Copy chunk rows document-by-document from Milvus into knowledge_chunks."""
    factory = get_session_factory()
    async with factory() as session:
        doc_keys = (await session.execute(select(Document.doc_key))).scalars().all()
    if not doc_keys:
        print("[migrate] 目标库 documents 为空, 跳过向量 (先跑元数据迁移)")
        return

    total = 0
    async with factory() as session:
        async with session.begin():
            for key in doc_keys:
                hits = client.query(
                    collection_name=collection,
                    filter=f'doc_id == "{key}"',
                    output_fields=[*CHUNK_FIELDS, "embedding"],
                    limit=_MILVUS_PAGE_LIMIT,
                )
                if len(hits) >= _MILVUS_PAGE_LIMIT:
                    print(
                        f"[warn] doc {key} 命中 Milvus query 单次上限 "
                        f"{_MILVUS_PAGE_LIMIT}, 可能有遗漏; 建议对该文档重新入库"
                    )
                if not hits:
                    continue
                rows = [
                    {
                        "chunk_id": h["chunk_id"],
                        "doc_id": h["doc_id"],
                        "title": h.get("title", ""),
                        "content": h.get("content", ""),
                        "source": h.get("source", ""),
                        "modality": h.get("modality") or "text",
                        "parent_id": h.get("parent_id", ""),
                        "is_parent": bool(h.get("is_parent", 0)),
                        "page_no": int(h.get("page_no", -1)),
                        "section": h.get("section", ""),
                        "visibility": h.get("visibility") or "public",
                        "owner_id": h.get("owner_id", ""),
                        "dept_id": h.get("dept_id", ""),
                        "allowed_roles": h.get("allowed_roles", ""),
                        "embedding": list(h["embedding"]),
                    }
                    for h in hits
                ]
                for start in range(0, len(rows), batch):
                    chunk_stmt = pg_insert(KnowledgeChunkRow).values(rows[start : start + batch])
                    await session.execute(
                        chunk_stmt.on_conflict_do_update(
                            index_elements=["chunk_id"],
                            set_={
                                col.name: chunk_stmt.excluded[col.name]
                                for col in KnowledgeChunkRow.__table__.columns
                                if col.name != "chunk_id"
                            },
                        )
                    )
                total += len(rows)
                print(f"[migrate] {key}: {len(rows)} 块")
    print(f"[migrate] knowledge_chunks: 共 {total} 块 -> pgvector")


async def report() -> None:
    """Print target row counts so the operator can eyeball the result."""
    factory = get_session_factory()
    async with factory() as session:
        counts = {}
        for model in (*METADATA_MODELS, KnowledgeChunkRow):
            table = model.__tablename__
            counts[table] = (
                await session.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar_one()
    for table, n in counts.items():
        print(f"[check] {table}: {n} 行")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate MySQL metadata + Milvus vectors into PostgreSQL + pgvector"
    )
    parser.add_argument(
        "--mysql-url",
        default=os.getenv("MYSQL_URL", ""),
        help="源库 DSN (mysql+pymysql://...), 默认取环境变量 MYSQL_URL",
    )
    parser.add_argument("--milvus-uri", default="", help="旧 Milvus Lite db 文件路径 (留空则跳过向量)")
    parser.add_argument("--milvus-collection", default="enterprise_knowledge")
    parser.add_argument("--skip-vectors", action="store_true", help="只搬元数据/业务表")
    parser.add_argument("--batch", type=int, default=500, help="单语句行数")
    args = parser.parse_args()

    if not args.mysql_url:
        raise SystemExit("必须提供 --mysql-url (或环境变量 MYSQL_URL)")

    await init_schema()  # 幂等: 确保扩展与表结构就绪
    src = source_engine(args.mysql_url)
    with src.connect() as conn:
        print(f"[migrate] 源库版本: {conn.execute(text('SELECT VERSION()')).scalar()}")

    await migrate_metadata(src, args.batch)

    if not args.skip_vectors and args.milvus_uri:
        client = _milvus_client(args.milvus_uri)
        await migrate_vectors(client, args.milvus_collection, args.batch)
    else:
        print("[migrate] 跳过向量迁移 (可执行 scripts.ingest_knowledge 重新入库)")

    await report()
    print("[done] 迁移完成; 请重跑 BM25: python -m scripts.ingest_knowledge --dir ./data/knowledge")


if __name__ == "__main__":
    asyncio.run(main())
