"""PostgreSQL + pgvector knowledge store (父子双表)。

拆分自旧单表 ``knowledge_chunks``:
- ``ChunkStore``  —— 子块表 ``doc_chunks``: 稠密向量 ANN、chunk_text 主键回表、
  增量 embed 判定、BM25 语料流式导出、文档级 ACL 前置裁剪。
- ``ParentStore`` —— 父块表 ``doc_parents``: 只存结构定位(不存正文), 正文在 Mongo。

关键约束(实现时不得违背):
- **ANN 扫描列白名单不含 ``chunk_text`` / ``embedding``**: TopK 只取窄列, 命中后按
  ``chunk_id`` 主键点查批量取文本 —— 既保住"向量与其 embedding 输入同行", 又不让
  宽行进入排序扫描(本方案的核心收益)。
- 权限字段永远是标量列, ``build_sql_filter``(PG) 与 ``_acl_filter``(ES) 逐条对应;
  ``extra``(JSONB) 一律只承载展示字段, 权限字段禁止入内。

``PgVectorStore`` 作为 ``ChunkStore`` 的兼容别名保留(旧 import 与评测脚本仍可用)。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.config import get_settings
from app.db.models import DocChunkRow, DocParentRow
from app.db.session import get_session_factory
from app.schemas import DocVisibility, KnowledgeChunk, ParentBlock
from app.security.acl import Principal

logger = logging.getLogger(__name__)

# ACL 标量列: 权限变更只需 UPDATE 这些列(向量与 HNSW 索引不受影响)。
ACL_COLUMNS = ("visibility", "owner_id", "dept_id", "allowed_roles")

# 子块 ANN 扫描的列白名单: **不含 chunk_text / embedding**(核心约束)。
_NARROW_ATTRS = (
    "chunk_id", "doc_id", "parent_id", "chunk_index", "ord", "title",
    "source", "modality", "section", "page_no", "content_hash",
)
NARROW_COLUMNS = tuple(getattr(DocChunkRow, a) for a in _NARROW_ATTRS) + tuple(
    getattr(DocChunkRow, a) for a in ACL_COLUMNS
)

# 父块取回的列: 结构与定位(不取正文)。
_PARENT_ATTRS = (
    "parent_id", "doc_id", "ord", "parent_type", "title", "section", "page_no",
    "start_offset", "end_offset", "content_hash", "char_count",
)
PARENT_COLUMNS = tuple(getattr(DocParentRow, a) for a in _PARENT_ATTRS) + tuple(
    getattr(DocParentRow, a) for a in ACL_COLUMNS
)

# 子块主键冲突时整体覆盖的列(chunk_text / embedding 都在内 -> 重入库即刷新)。
CHUNK_UPSERT_COLUMNS = (
    "doc_id", "parent_id", "chunk_index", "ord", "chunk_text", "content_hash",
    "char_count", "embedding_model", "title", "source", "modality", "section",
    "page_no", "embedding", "extra", *ACL_COLUMNS,
)
PARENT_UPSERT_COLUMNS = (
    "doc_id", "ord", "parent_type", "title", "section", "page_no", "start_offset",
    "end_offset", "content_hash", "char_count", "child_count", "normalizer_version",
    "extra", *ACL_COLUMNS,
)


def _acl_predicate(R: Any, principal: Principal | None) -> ColumnElement[bool] | None:
    """构造与 ES ``_acl_filter`` 语义逐条对应的 ACL 谓词(前置裁剪)。

    对 DocChunkRow / DocParentRow 通用(四列同名)。返回 None 表示不过滤
    (admin 或受信内部调用)。``allowed_roles`` 以 ",hr,admin," 存储, LIKE 匹配,
    角色值里的 % / _ / \\ 必须转义, 否则含通配符的角色会放大匹配范围。
    """
    if principal is None or principal.is_admin:
        return None
    clauses: list[ColumnElement[bool]] = [R.visibility == DocVisibility.PUBLIC.value]
    if principal.user_id:
        clauses.append(
            and_(R.visibility == DocVisibility.PRIVATE.value, R.owner_id == principal.user_id)
        )
    if principal.department:
        clauses.append(
            and_(R.visibility == DocVisibility.DEPT.value, R.dept_id == principal.department)
        )
    role = principal.role.value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    clauses.append(
        and_(
            R.visibility == DocVisibility.ROLE.value,
            R.allowed_roles.like(f"%,{role},%", escape="\\"),
        )
    )
    return or_(*clauses)


def build_sql_filter(principal: Principal | None) -> ColumnElement[bool] | None:
    """子块通道的 ACL 谓词(向后兼容旧调用点: 作用于 ``doc_chunks``)。"""
    return _acl_predicate(DocChunkRow, principal)


def _chunks_from_narrow(row: Any, score: float) -> KnowledgeChunk:
    """窄列 Row -> 子块 DTO(content 留空, 由 attach_texts 主键回表补)。"""
    return KnowledgeChunk(
        chunk_id=row.chunk_id,
        doc_id=row.doc_id,
        parent_id=row.parent_id or "",
        chunk_index=int(row.chunk_index or 0),
        ord=int(row.ord or 0),
        title=row.title or "",
        source=row.source or "",
        modality=row.modality or "text",
        section=row.section or "",
        page_no=int(row.page_no),
        content_hash=row.content_hash or "",
        content="",
        is_parent=False,
        visibility=row.visibility or "public",
        owner_id=row.owner_id or "",
        dept_id=row.dept_id or "",
        allowed_roles=row.allowed_roles or "",
        score=score,
    )


def _chunk_row(c: KnowledgeChunk, v: Sequence[float] | None) -> dict[str, Any]:
    """子块 DTO -> doc_chunks 行(embedding 可为 None: 未变化块不重 embed)。"""
    row: dict[str, Any] = {
        "chunk_id": c.chunk_id[:80],
        "doc_id": c.doc_id[:64],
        "parent_id": c.parent_id[:80],
        "chunk_index": c.chunk_index,
        "ord": c.ord,
        "chunk_text": c.content,
        "content_hash": c.content_hash[:32],
        "char_count": len(c.content),
        "embedding_model": get_settings().embedding_model[:64],
        "title": c.title[:512],
        "source": c.source[:512],
        "modality": c.modality,
        "section": c.section[:256],
        "page_no": c.page_no,
        "visibility": c.visibility[:16],
        "owner_id": c.owner_id[:64],
        "dept_id": c.dept_id[:64],
        "allowed_roles": c.allowed_roles[:128],
        "extra": c.extra or {},
    }
    if v is not None:
        row["embedding"] = list(v)
    return row


class ChunkStore:
    """子块持久化 + ANN 检索(doc_chunks)。构造零 I/O。"""

    def __init__(self) -> None:
        self._factory: async_sessionmaker[AsyncSession] | None = None

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            self._factory = get_session_factory()
        return self._factory

    # -------------------------------------------------------------------- 检索
    async def search(
        self,
        query_vector: Sequence[float],
        top_k: int,
        principal: Principal | None = None,
    ) -> list[KnowledgeChunk]:
        """ANN cosine TopK; score = cosine 距离(越小越相关)。

        只取 NARROW_COLUMNS(不含正文), 正文由 ``attach_texts`` 在 rerank 之前主键回表。
        ACL 谓词作用在 ORDER BY ... LIMIT 之前, 与 ES 通道语义一致。
        """
        dist = DocChunkRow.embedding.cosine_distance(list(query_vector))
        stmt = (
            select(*NARROW_COLUMNS, dist.label("score"))
            .order_by(dist)
            .limit(top_k)
        )
        if (acl := _acl_predicate(DocChunkRow, principal)) is not None:
            stmt = stmt.where(acl)
        async with self._sessions()() as session:
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {max(100, top_k * 8)}"))
            hits = (await session.execute(stmt)).all()
            chunks = [_chunks_from_narrow(h, float(h.score)) for h in hits]
            await session.rollback()  # 结束 SET LOCAL 的隐式事务, 不污染连接池
        return chunks

    async def get_texts(self, chunk_ids: Sequence[str]) -> dict[str, str]:
        """按 chunk_id 主键批量点查正文(纯 PK 命中, 无 TOAST 读放大)。"""
        if not chunk_ids:
            return {}
        ids = list(dict.fromkeys(chunk_ids))
        page = get_settings().mongo_batch_page_size
        out: dict[str, str] = {}
        async with self._sessions()() as session:
            for start in range(0, len(ids), page):
                batch = ids[start : start + page]
                rows = (
                    await session.execute(
                        select(DocChunkRow.chunk_id, DocChunkRow.chunk_text).where(
                            DocChunkRow.chunk_id.in_(batch)
                        )
                    )
                ).all()
                out.update({r.chunk_id: r.chunk_text or "" for r in rows})
        return out

    async def attach_texts(self, chunks: Sequence[KnowledgeChunk]) -> list[KnowledgeChunk]:
        """回表入口: 给窄行 DTO 补 chunk_text(保持顺序); 缺文本计数告警。"""
        if not chunks:
            return []
        texts = await self.get_texts([c.chunk_id for c in chunks])
        missing = 0
        out: list[KnowledgeChunk] = []
        for c in chunks:
            text_ = texts.get(c.chunk_id, "")
            if not text_:
                missing += 1
            out.append(c.model_copy(update={"content": text_}))
        if missing:
            logger.warning("chunk_text attach missing for %d/%d chunks", missing, len(chunks))
        return out

    async def existing_hashes(self, doc_id: str) -> dict[str, str]:
        """一次查询拿到该 doc 全部旧块 content_hash(增量 embed 判定用)。"""
        async with self._sessions()() as session:
            rows = (
                await session.execute(
                    select(DocChunkRow.chunk_id, DocChunkRow.content_hash).where(
                        DocChunkRow.doc_id == doc_id
                    )
                )
            ).all()
            return {r.chunk_id: r.content_hash or "" for r in rows}

    async def get_embeddings(self, chunk_ids: Sequence[str]) -> dict[str, list[float]]:
        """按主键批量取回向量(增量入库: 未变化块不重 embed, 原样读回填)。"""
        if not chunk_ids:
            return {}
        ids = list(dict.fromkeys(chunk_ids))
        page = get_settings().mongo_batch_page_size
        out: dict[str, list[float]] = {}
        async with self._sessions()() as session:
            for start in range(0, len(ids), page):
                batch = ids[start : start + page]
                rows = (
                    await session.execute(
                        select(DocChunkRow.chunk_id, DocChunkRow.embedding).where(
                            DocChunkRow.chunk_id.in_(batch)
                        )
                    )
                ).all()
                out.update({r.chunk_id: list(r.embedding) for r in rows if r.embedding is not None})
        return out

    # -------------------------------------------------------------------- 流式
    async def iter_chunk_corpus(
        self, page_size: int | None = None
    ) -> AsyncIterator[list[KnowledgeChunk]]:
        """BM25 重建唯一语料源(含 chunk_text), keyset 分页, 不再一次性加载全表。"""
        page_size = page_size or get_settings().mongo_batch_page_size
        after = ""
        while True:
            async with self._sessions()() as session:
                rows = (
                    await session.execute(
                        select(*NARROW_COLUMNS, DocChunkRow.chunk_text)
                        .where(DocChunkRow.chunk_id > after)
                        .order_by(DocChunkRow.chunk_id)
                        .limit(page_size)
                    )
                ).all()
            if not rows:
                return
            batch = [
                KnowledgeChunk(
                    **{k: getattr(r, k) for k in _NARROW_ATTRS},
                    content=r.chunk_text or "",
                    is_parent=False,
                )
                for r in rows
            ]
            after = rows[-1].chunk_id
            yield batch
            if len(rows) < page_size:
                return

    async def iter_child_chunks(self, limit: int = 100000) -> list[KnowledgeChunk]:
        """[兼容薄封装] 旧整表加载接口, 内部转调流式方法。收尾阶段随旧表删除。"""
        out: list[KnowledgeChunk] = []
        async for batch in self.iter_chunk_corpus():
            out.extend(batch)
            if len(out) >= limit:
                logger.warning("BM25 corpus hit the row limit (%d); raise it", limit)
                break
        return out

    # -------------------------------------------------------------------- 写入
    async def upsert_chunks(
        self, chunks: Sequence[KnowledgeChunk], vectors: Sequence[Sequence[float] | None]
    ) -> int:
        """子块批量 upsert(ON CONFLICT); vectors 允许 None(未变化块保持原向量)。"""
        assert len(chunks) == len(vectors), "chunks/vectors length mismatch"
        rows = [self._row_with_vec(c, v) for c, v in zip(chunks, vectors)]
        if not rows:
            return 0
        batch = max(1, get_settings().upsert_batch_size)
        async with self._sessions()() as session:
            async with session.begin():
                for start in range(0, len(rows), batch):
                    await self._upsert_batch(
                        session, pg_insert(DocChunkRow), rows[start : start + batch],
                        DocChunkRow.chunk_id, CHUNK_UPSERT_COLUMNS,
                    )
        return len(rows)

    @staticmethod
    def _row_with_vec(c: KnowledgeChunk, v: Sequence[float] | None) -> dict[str, Any]:
        return _chunk_row(c, v)

    @staticmethod
    async def _upsert_batch(
        session: AsyncSession, dialect_insert, rows: list[dict], pk_col, upsert_cols
    ) -> None:
        stmt = dialect_insert.values(rows)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[pk_col],
                set_={col: stmt.excluded[col] for col in upsert_cols},
            )
        )

    async def delete_by_doc(self, doc_id: str) -> int:
        """删除某文档全部子块(文档删除路径; 父块由 ParentStore 负责)。"""
        async with self._sessions()() as session:
            res = await session.execute(
                delete(DocChunkRow).where(DocChunkRow.doc_id == doc_id)
            )
            await session.commit()
        return int(res.rowcount or 0)

    async def publish_parent_child(
        self,
        parent_store: "ParentStore",
        doc_id: str,
        parent_rows: Sequence[ParentBlock],
        chunks: Sequence[KnowledgeChunk],
        vectors: Sequence[Sequence[float] | None],
    ) -> int:
        """单事务发布父子块: upsert 新行 -> 陈旧剪除(先子后父)。

        取代旧「delete_by_doc + upsert 两个独立事务」的空窗。同 id 覆盖, 故常态
        重入库无删除窗口; 陈旧行(本次不再引用的)按分批 ``in_`` 删除, 不用超长 NOT IN。
        """
        assert len(chunks) == len(vectors), "chunks/vectors length mismatch"
        batch = max(1, get_settings().upsert_batch_size)
        new_chunk_ids = {c.chunk_id for c in chunks}
        new_parent_ids = {p.parent_id for p in parent_rows}
        chunk_rows = [self._row_with_vec(c, v) for c, v in zip(chunks, vectors)]
        p_rows = [_parent_row(p) for p in parent_rows]
        async with self._sessions()() as session:
            async with session.begin():
                old_chunk_ids = set(
                    (
                        await session.execute(
                            select(DocChunkRow.chunk_id).where(DocChunkRow.doc_id == doc_id)
                        )
                    ).scalars().all()
                )
                old_parent_ids = set(
                    (
                        await session.execute(
                            select(DocParentRow.parent_id).where(DocParentRow.doc_id == doc_id)
                        )
                    ).scalars().all()
                )
                for start in range(0, len(p_rows), batch):
                    await self._upsert_batch(
                        session, pg_insert(DocParentRow), p_rows[start : start + batch],
                        DocParentRow.parent_id, PARENT_UPSERT_COLUMNS,
                    )
                for start in range(0, len(chunk_rows), batch):
                    await self._upsert_batch(
                        session, pg_insert(DocChunkRow), chunk_rows[start : start + batch],
                        DocChunkRow.chunk_id, CHUNK_UPSERT_COLUMNS,
                    )
                # 先删陈旧子块, 再删陈旧父块(避开将来 FK 的顺序问题)
                stale_children = old_chunk_ids - new_chunk_ids
                stale_parents = old_parent_ids - new_parent_ids
                for start in range(0, len(stale_children), batch):
                    page = list(stale_children)[start : start + batch]
                    await session.execute(
                        delete(DocChunkRow).where(DocChunkRow.chunk_id.in_(page))
                    )
                for start in range(0, len(stale_parents), batch):
                    page = list(stale_parents)[start : start + batch]
                    await session.execute(
                        delete(DocParentRow).where(DocParentRow.parent_id.in_(page))
                    )
        return len(chunk_rows)

    async def update_acl_by_doc(
        self, doc_id: str, visibility: str, owner_id: str, dept_id: str, allowed_roles: str
    ) -> tuple[int, int]:
        """同一事务内两条 UPDATE(doc_chunks + doc_parents); 返回 (chunks, parents)。"""
        vals = {
            "visibility": visibility[:16],
            "owner_id": owner_id[:64],
            "dept_id": dept_id[:64],
            "allowed_roles": allowed_roles[:128],
        }
        async with self._sessions()() as session:
            async with session.begin():
                cres = await session.execute(
                    update(DocChunkRow).where(DocChunkRow.doc_id == doc_id).values(**vals)
                )
                pres = await session.execute(
                    update(DocParentRow).where(DocParentRow.doc_id == doc_id).values(**vals)
                )
        return int(cres.rowcount or 0), int(pres.rowcount or 0)

    async def counts(self) -> dict[str, int]:
        async with self._sessions()() as session:
            n = int(
                (await session.execute(select(func.count()).select_from(DocChunkRow))).scalar_one()
            )
            m = int(
                (await session.execute(select(func.count()).select_from(DocParentRow))).scalar_one()
            )
        return {"chunks": n, "parents": m}

    async def count(self) -> int:
        """[兼容] 子块行数(旧脚本 ingest_knowledge 用)。"""
        return (await self.counts())["chunks"]


class ParentStore:
    """父块持久化(doc_parents): 结构定位, 正文在 Mongo。构造零 I/O。"""

    def __init__(self) -> None:
        self._factory: async_sessionmaker[AsyncSession] | None = None

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            self._factory = get_session_factory()
        return self._factory

    async def get_blocks(self, parent_ids: Sequence[str]) -> dict[str, ParentBlock]:
        """按 parent_id 取回父块结构(不取正文); content 由 retriever 从 Mongo 填。"""
        if not parent_ids:
            return {}
        ids = list(dict.fromkeys(parent_ids))
        page = get_settings().mongo_batch_page_size
        out: dict[str, ParentBlock] = {}
        async with self._sessions()() as session:
            for start in range(0, len(ids), page):
                rows = (
                    await session.execute(
                        select(*PARENT_COLUMNS).where(
                            DocParentRow.parent_id.in_(ids[start : start + page])
                        )
                    )
                ).all()
                for r in rows:
                    out[r.parent_id] = ParentBlock(
                        parent_id=r.parent_id,
                        doc_id=r.doc_id,
                        title=r.title or "",
                        section=r.section or "",
                        page_no=int(r.page_no),
                        parent_type=r.parent_type or "section",
                        ord=int(r.ord or 0),
                        start_offset=int(r.start_offset),
                        end_offset=int(r.end_offset),
                        content_hash=r.content_hash or "",
                        visibility=r.visibility or "public",
                        owner_id=r.owner_id or "",
                        dept_id=r.dept_id or "",
                        allowed_roles=r.allowed_roles or "",
                    )
        return out

    async def list_ids(self, doc_id: str | None = None) -> set[str]:
        async with self._sessions()() as session:
            stmt = select(DocParentRow.parent_id)
            if doc_id is not None:
                stmt = stmt.where(DocParentRow.doc_id == doc_id)
            return set((await session.execute(stmt)).scalars().all())

    async def delete_stale(self, doc_id: str, keep_ids: set[str]) -> int:
        batch = max(1, get_settings().upsert_batch_size)
        async with self._sessions()() as session:
            existing = set(
                (
                    await session.execute(
                        select(DocParentRow.parent_id).where(DocParentRow.doc_id == doc_id)
                    )
                ).scalars().all()
            )
            stale = list(existing - keep_ids)
            total = 0
            async with session.begin():
                for start in range(0, len(stale), batch):
                    res = await session.execute(
                        delete(DocParentRow).where(
                            DocParentRow.parent_id.in_(stale[start : start + batch])
                        )
                    )
                    total += int(res.rowcount or 0)
        return total

    async def delete_by_doc(self, doc_id: str) -> int:
        async with self._sessions()() as session:
            res = await session.execute(
                delete(DocParentRow).where(DocParentRow.doc_id == doc_id)
            )
            await session.commit()
        return int(res.rowcount or 0)


def _parent_row(p: ParentBlock) -> dict[str, Any]:
    return {
        "parent_id": p.parent_id[:80],
        "doc_id": p.doc_id[:64],
        "ord": p.ord,
        "parent_type": p.parent_type[:16],
        "title": p.title[:512],
        "section": p.section[:256],
        "page_no": p.page_no,
        "start_offset": p.start_offset,
        "end_offset": p.end_offset,
        "content_hash": p.content_hash[:32],
        "char_count": len(p.content),
        "child_count": 0,
        "normalizer_version": get_settings().normalizer_version[:8],
        "visibility": p.visibility[:16],
        "owner_id": p.owner_id[:64],
        "dept_id": p.dept_id[:64],
        "allowed_roles": p.allowed_roles[:128],
        "extra": {},
    }


# 向后兼容别名: 旧 import(PgVectorStore)与评测脚本仍指向子块 store。
PgVectorStore = ChunkStore
