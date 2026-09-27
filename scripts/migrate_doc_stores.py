"""正文外置 MongoDB + PG 父子双表: 存量迁移与校验脚本。

一次性把旧单表 ``knowledge_chunks``(父子混表) + ``documents.parsed_text`` 回填到
新链路: 整篇正文 -> Mongo ``doc_bodies``; 父块 -> PG ``doc_parents``(不存正文) +
Mongo ``parent_texts``; 子块 -> PG ``doc_chunks``(向量纯 SQL 搬运, 不经 Python)。

Usage:
    python -m scripts.migrate_doc_stores [--batch 500] [--doc-key K]
        [--dry-run] [--verify-only] [--prune] [--drop-legacy]

阶段: 迁移(默认) -> --verify-only 复核 -> 观察一个发布周期 -> --drop-legacy 下线旧表。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.bodies.client import init_body_schema
from app.bodies.store import ParentTextItem, get_body_store
from app.config import get_settings
from app.db.models import DocChunkRow, DocParentRow, Document, KnowledgeChunkRow
from app.db.session import get_session_factory, init_schema
from app.docs.normalize import content_hash, normalize_text
from app.rag.ingest import _locate

logger = logging.getLogger("migrate_doc_stores")

REINGEST_LIST = Path("data/logs/reingest_needed.json")


def _sha1(text_: str) -> str:
    return hashlib.sha1(text_.encode("utf-8")).hexdigest()


async def _provision() -> None:
    await init_schema()
    await init_body_schema()
    factory = get_session_factory()
    bodies = get_body_store()
    async with factory() as session:
        old_children = int(
            (
                await session.execute(
                    select(func.count()).select_from(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.is_parent.is_(False)
                    )
                )
            ).scalar_one()
        )
        old_parents = int(
            (
                await session.execute(
                    select(func.count()).select_from(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.is_parent.is_(True)
                    )
                )
            ).scalar_one()
        )
        new_chunks = int((await session.execute(select(func.count()).select_from(DocChunkRow))).scalar_one())
        new_parents = int((await session.execute(select(func.count()).select_from(DocParentRow))).scalar_one())
    mongo_counts = await bodies.count()
    print(
        f"[counts] old children={old_children} old parents={old_parents} | "
        f"new doc_chunks={new_chunks} doc_parents={new_parents} | mongo={mongo_counts}"
    )


async def _derive_structure(session, doc_key: str) -> list[dict]:
    """由现存父块的 section/page_no 反推一棵扁平章节树, 标 derived=True(旧数据无真实层级)。"""
    rows = (
        await session.execute(
            select(KnowledgeChunkRow.chunk_id, KnowledgeChunkRow.section, KnowledgeChunkRow.page_no)
            .where(KnowledgeChunkRow.doc_id == doc_key, KnowledgeChunkRow.is_parent.is_(True))
            .order_by(KnowledgeChunkRow.chunk_id)
        )
    ).all()
    nodes = []
    for i, r in enumerate(rows):
        parts = [p for p in (r.section or "").split("/") if p]
        nodes.append(
            {
                "node_id": r.chunk_id, "title": parts[-1] if parts else "",
                "level": len(parts) or 1, "path": parts, "parent_type": "section",
                "page_no": int(r.page_no), "derived": True,
            }
        )
    return nodes


async def _migrate_one(session, bodies, doc_key: str, parsed_text: str, dry: bool) -> dict:
    """迁移单篇: 整篇正文入 Mongo + 父块拆分 + 子块纯 SQL 搬运; 返回统计。"""
    normalized = normalize_text(parsed_text or "")
    structure = await _derive_structure(session, doc_key)
    stats = {"doc_key": doc_key, "parents": 0, "offset_failed": 0, "children": 0}

    # --- 3.2 整篇正文 -> doc_bodies ---
    if not dry:
        await bodies.save_doc_body(
            doc_key, raw=parsed_text or "", normalized=normalized, structure=structure,
            meta={"migrated": True},
        )

    # --- 3.3/3.4 父块: 读旧父块 -> 回算 offset -> doc_parents(不存正文) + Mongo parent_texts ---
    parent_rows = (
        await session.execute(
            select(KnowledgeChunkRow)
            .where(KnowledgeChunkRow.doc_id == doc_key, KnowledgeChunkRow.is_parent.is_(True))
            .order_by(KnowledgeChunkRow.chunk_id)
        )
    ).scalars().all()
    cursor = 0
    p_dicts: list[dict] = []
    p_items: list[ParentTextItem] = []
    for seq, pr in enumerate(parent_rows):
        needle = (pr.content or "").strip()
        start, end = _locate(needle, normalized, cursor) if needle else (-1, -1)
        if start >= 0:
            cursor = start + 1
        else:
            stats["offset_failed"] += 1
            REINGEST_LIST.parent.mkdir(parents=True, exist_ok=True)
            _append_reingest(doc_key)
        phash = content_hash(pr.content or "")
        ord_ = int((seq_hint(pr.chunk_id) or seq))
        p_dicts.append(
            {
                "parent_id": pr.chunk_id[:80], "doc_id": pr.doc_id[:64], "ord": ord_,
                "parent_type": "section", "title": (pr.title or "")[:512],
                "section": (pr.section or "")[:256], "page_no": int(pr.page_no),
                "start_offset": start, "end_offset": end, "content_hash": phash[:32],
                "char_count": len(pr.content or ""), "child_count": 0,
                "normalizer_version": get_settings().normalizer_version[:8],
                "visibility": pr.visibility or "public", "owner_id": pr.owner_id or "",
                "dept_id": pr.dept_id or "", "allowed_roles": pr.allowed_roles or "", "extra": {},
            }
        )
        p_items.append(
            ParentTextItem(
                parent_id=pr.chunk_id, doc_id=pr.doc_id, text=pr.content or "",
                anchor={"type": "section", "value": pr.section or "", "node_id": pr.chunk_id,
                        "locator": f"page:{pr.page_no}" if pr.page_no > 0 else ""},
                start_offset=start, end_offset=end, content_hash=phash,
            )
        )
    stats["parents"] = len(p_dicts)
    if not dry and p_dicts:
        batch = max(1, get_settings().upsert_batch_size)
        parent_fields = [k for k in p_dicts[0] if k != "parent_id"]
        for start_i in range(0, len(p_dicts), batch):
            stmt = pg_insert(DocParentRow).values(p_dicts[start_i : start_i + batch])
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[DocParentRow.parent_id],
                    set_={k: stmt.excluded[k] for k in parent_fields},
                )
            )
        if p_items:
            await bodies.save_parent_texts(p_items)

    # --- 3.3 子块(含向量)走纯 SQL 搬, 不经 Python(避开 1024 维浮点序列化漂移) ---
    if not dry:
        await session.execute(
            text(
                """
                INSERT INTO doc_chunks (
                    chunk_id, doc_id, parent_id, chunk_index, ord, chunk_text, content_hash,
                    char_count, embedding_model, title, source, modality, section, page_no,
                    visibility, owner_id, dept_id, allowed_roles, extra, embedding,
                    created_at, updated_at)
                SELECT chunk_id, doc_id, parent_id,
                    COALESCE(substring(chunk_id from '-c([0-9]+)$')::int, 0),
                    COALESCE(substring(chunk_id from '-p([0-9]+)-')::int, 0) * 100
                        + COALESCE(substring(chunk_id from '-c([0-9]+)$')::int, 0),
                    content, substring(md5(coalesce(content,'')) from 1 for 16),
                    length(coalesce(content,'')), :emodel, title, source, modality, section, page_no,
                    visibility, owner_id, dept_id, allowed_roles, '{}'::json, embedding,
                    now(), now()
                FROM knowledge_chunks WHERE doc_id = :k AND is_parent = FALSE
                ON CONFLICT (chunk_id) DO UPDATE SET
                    chunk_text = EXCLUDED.chunk_text, embedding = EXCLUDED.embedding,
                    content_hash = EXCLUDED.content_hash
                """
            ),
            {"k": doc_key, "emodel": get_settings().embedding_model},
        )
    stats["children"] = int(
        (
            await session.execute(
                select(func.count()).select_from(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.doc_id == doc_key, KnowledgeChunkRow.is_parent.is_(False)
                )
            )
        ).scalar_one()
    )
    if not dry:
        await session.execute(
            text("UPDATE documents SET body_stored = true WHERE doc_key = :k"), {"k": doc_key}
        )
    return stats


def seq_hint(chunk_id: str) -> int | None:
    import re

    m = re.search(r"-p(\d+)", chunk_id)
    return int(m.group(1)) if m else None


def _append_reingest(doc_key: str) -> None:
    try:
        data: list[str] = []
        if REINGEST_LIST.exists():
            data = json.loads(REINGEST_LIST.read_text(encoding="utf-8"))
        if doc_key not in data:
            data.append(doc_key)
            REINGEST_LIST.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("append reingest list failed: %s", exc)


async def migrate(doc_key_filter: str | None, batch: int, dry: bool) -> None:
    bodies = get_body_store()
    factory = get_session_factory()
    async with factory() as session:
        if doc_key_filter:
            docs = (
                await session.execute(
                    select(Document.doc_key, Document.parsed_text).where(
                        Document.doc_key == doc_key_filter
                    )
                )
            ).all()
        else:
            docs = (await session.execute(select(Document.doc_key, Document.parsed_text))).all()
        for r in docs:
            stats = await _migrate_one(session, bodies, r.doc_key, r.parsed_text or "", dry)
            flag = "DRY" if dry else "OK "
            print(
                f"[{flag}] {r.doc_key}: parents={stats['parents']} "
                f"offset_failed={stats['offset_failed']} children={stats['children']}"
            )
        await session.commit()


async def verify_only() -> int:
    """校验: 行数/正文长度/抽样 sha1/向量计数; 任一不一致返回 1(不删任何数据)。"""
    factory = get_session_factory()
    bodies = get_body_store()
    ok = True
    async with factory() as session:
        old_children = int((await session.execute(select(func.count()).select_from(KnowledgeChunkRow).where(KnowledgeChunkRow.is_parent.is_(False)))).scalar_one())
        new_children = int((await session.execute(select(func.count()).select_from(DocChunkRow))).scalar_one())
        old_parents = int((await session.execute(select(func.count()).select_from(KnowledgeChunkRow).where(KnowledgeChunkRow.is_parent.is_(True)))).scalar_one())
        new_parents = int((await session.execute(select(func.count()).select_from(DocParentRow))).scalar_one())
        mongo = await bodies.count()
        old_len = int((await session.execute(select(func.coalesce(func.sum(func.length(KnowledgeChunkRow.content)), 0)).where(KnowledgeChunkRow.is_parent.is_(False)))).scalar_one())
        new_len = int((await session.execute(select(func.coalesce(func.sum(func.length(DocChunkRow.chunk_text)), 0)))).scalar_one())
        old_vec = int((await session.execute(select(func.count()).select_from(KnowledgeChunkRow).where(KnowledgeChunkRow.is_parent.is_(False), KnowledgeChunkRow.embedding.isnot(None)))).scalar_one())
        new_vec = int((await session.execute(select(func.count()).select_from(DocChunkRow).where(DocChunkRow.embedding.isnot(None)))).scalar_one())
        body_stored = int((await session.execute(select(func.count()).select_from(Document).where(Document.body_stored.is_(True)))).scalar_one())

    checks = [
        ("doc_chunks == old children", new_children, old_children),
        ("doc_parents == old parents", new_parents, old_parents),
        ("mongo parent_texts == doc_parents", mongo.get("parent_texts", -1), new_parents),
        ("mongo doc_bodies == body_stored", mongo.get("doc_bodies", -1), body_stored),
        ("children text length", new_len, old_len),
        ("children with vector", new_vec, old_vec),
    ]
    for name, a, b in checks:
        match = a == b
        ok = ok and match
        print(f"[verify] {'OK ' if match else 'BAD'} {name}: {a} vs {b}")

    # 抽样 200 行逐字符 sha1 比对(子块正文)
    async with factory() as session:
        pairs = (
            await session.execute(
                select(KnowledgeChunkRow.chunk_id, KnowledgeChunkRow.content, DocChunkRow.chunk_text)
                .join(DocChunkRow, DocChunkRow.chunk_id == KnowledgeChunkRow.chunk_id)
                .where(KnowledgeChunkRow.is_parent.is_(False))
                .limit(200)
            )
        ).all()
    diff = [cid for cid, old, new in pairs if (old or "") != (new or "")]
    if diff:
        ok = False
        print(f"[verify] BAD content sha mismatch: {len(diff)} sample ids, e.g. {diff[:5]}")
    else:
        print(f"[verify] OK content sample compare ({len(pairs)} rows)")

    print(f"[verify] exit={'0' if ok else '1'}")
    return 0 if ok else 1


async def prune(dry: bool) -> None:
    """回收 Mongo 中 PG 不再引用的孤儿正文(PG 是事实来源)。"""
    factory = get_session_factory()
    bodies = get_body_store()
    async with factory() as session:
        live_doc_keys = set((await session.execute(select(Document.doc_key))).scalars().all())
        live_parent_ids = set((await session.execute(select(DocParentRow.parent_id))).scalars().all())
    if dry:
        removed = await bodies.count()
        print(f"[prune dry] mongo counts now={removed}; live docs={len(live_doc_keys)} parents={len(live_parent_ids)}")
        return
    removed = await bodies.prune_orphans(live_doc_keys, live_parent_ids)
    print(f"[prune] removed orphans: {removed}")


async def drop_legacy() -> None:
    """阶段 5 下线: 改名旧表(可回滚) + 备份并删 documents.parsed_text 列。

    前置: --verify-only 通过且已观察一个发布周期。本函数只动数据库, 代码侧旧表/旧列
    引用需另行清理(见方案 3.7 步骤 4), 观察无异常后再手工 DROP 备份表。
    """
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            sizes = (
                await session.execute(
                    text(
                        "SELECT pg_size_pretty(pg_total_relation_size('knowledge_chunks')) AS total, "
                        "pg_size_pretty(COALESCE(SUM(pg_column_size(content)),0)) AS body_bytes "
                        "FROM knowledge_chunks"
                    )
                )
            ).first()
            print(f"[drop-legacy] knowledge_chunks total={sizes.total} body_bytes={sizes.body_bytes}")
            await session.execute(
                text("ALTER TABLE knowledge_chunks RENAME TO knowledge_chunks_legacy_bak")
            )
            await session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS documents_parsed_text_bak AS "
                    "SELECT doc_key, parsed_text FROM documents WHERE parsed_text IS NOT NULL"
                )
            )
            await session.execute(text("ALTER TABLE documents DROP COLUMN IF EXISTS parsed_text"))
    print("[drop-legacy] 旧表已改名 + parsed_text 已备份删列; 观察无异常后再手工 DROP 备份表")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="正文外置迁移与校验")
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--doc-key", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--drop-legacy", action="store_true")
    args = parser.parse_args()

    await _provision()
    if args.verify_only:
        raise SystemExit(await verify_only())
    if args.prune:
        await prune(args.dry_run)
        return
    if args.drop_legacy:
        await drop_legacy()
        return
    await migrate(args.doc_key, args.batch, args.dry_run)
    print("[migrate] done; 运行 --verify-only 复核")


if __name__ == "__main__":
    asyncio.run(main())
