"""PostgreSQL + pgvector knowledge store (替代原 Milvus Lite 向量库).

一张 ``knowledge_chunks`` 表同时承担: 稠密向量 ANN 检索、父子块组装取回、
文档级 ACL 标量前置裁剪 —— 三者在一个 SQL 里完成。因此原 Milvus 方案里两处
补丁逻辑一并消失: 没有「标量无局部更新 -> 读回整行含向量再 upsert」, 也没有
gRPC keepalive 被服务端判定 too_many_pings 后必须自愈的连接管理。

检索永远过滤 ``is_parent = false``(只有子块参与 TopK), 命中的父块随后按 id
批量取回用于上下文组装 —— 与旧 Milvus 行为保持一致。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.config import get_settings
from app.db.models import KnowledgeChunkRow
from app.db.session import get_session_factory
from app.schemas import DocVisibility, KnowledgeChunk
from app.security.acl import Principal

logger = logging.getLogger(__name__)

# ACL 标量列: 权限变更只需 UPDATE 这些列(向量与 HNSW 索引不受影响)。
ACL_COLUMNS = ("visibility", "owner_id", "dept_id", "allowed_roles")
# 主键冲突时需要整体覆盖的列(embedding 也在内 -> 重新入库即刷新向量)。
UPSERT_COLUMNS = (
    "doc_id",
    "title",
    "content",
    "source",
    "modality",
    "parent_id",
    "is_parent",
    "page_no",
    "section",
    "embedding",
    *ACL_COLUMNS,
)


def build_sql_filter(principal: Principal | None) -> ColumnElement[bool] | None:
    """SQLAlchemy predicate mirroring the ES bool filter in app.rag.bm25.

    两条检索通道的 ACL 语义必须逐条对应(前置裁剪: 无权文档根本不进候选集,
    不占 TopK): public 全员 / private 看 owner_id / dept 看 dept_id /
    role 看逗号包裹的角色串。返回 None 表示不过滤(admin 或受信内部调用)。

    ``allowed_roles`` 以 ",hr,admin," 形式存储, 用 LIKE 匹配; 角色值里的
    ``%`` / ``_`` / ``\\`` 必须转义, 否则一个含通配符的角色会放大匹配范围。
    """
    if principal is None or principal.is_admin:
        return None
    R = KnowledgeChunkRow
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


class PgVectorStore:
    """Vector persistence + ANN search over enterprise knowledge chunks."""

    def __init__(self) -> None:
        # 惰性取工厂: 本对象会在 engine 就绪之前被构造(密码可能还没读到),
        # 因此构造必须零 I/O。
        self._factory: async_sessionmaker[AsyncSession] | None = None

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            self._factory = get_session_factory()
        return self._factory

    # ------------------------------------------------------------------ 序列化
    @staticmethod
    def _row(c: KnowledgeChunk, v: Sequence[float]) -> dict[str, Any]:
        return {
            "chunk_id": c.chunk_id[:80],
            "doc_id": c.doc_id[:64],
            "title": c.title[:512],
            "content": c.content,
            "source": c.source[:512],
            "modality": c.modality,
            "parent_id": c.parent_id[:80],
            "is_parent": c.is_parent,
            "page_no": c.page_no,
            "section": c.section[:256],
            "visibility": c.visibility[:16],
            "owner_id": c.owner_id[:64],
            "dept_id": c.dept_id[:64],
            "allowed_roles": c.allowed_roles[:128],
            "embedding": list(v),
        }

    @staticmethod
    def _to_chunk(row: KnowledgeChunkRow, score: float = 0.0) -> KnowledgeChunk:
        return KnowledgeChunk(
            chunk_id=row.chunk_id,
            doc_id=row.doc_id,
            title=row.title,
            content=row.content,
            source=row.source,
            modality=row.modality or "text",
            parent_id=row.parent_id or "",
            is_parent=bool(row.is_parent),
            page_no=int(row.page_no),
            section=row.section or "",
            visibility=row.visibility or "public",
            owner_id=row.owner_id or "",
            dept_id=row.dept_id or "",
            allowed_roles=row.allowed_roles or "",
            score=score,
        )

    # -------------------------------------------------------------------- 写入
    async def upsert(
        self, chunks: Sequence[KnowledgeChunk], vectors: Sequence[Sequence[float]]
    ) -> int:
        """Insert or update chunks with their dense vectors (batched ON CONFLICT)."""
        assert len(chunks) == len(vectors), "chunks/vectors length mismatch"
        rows = [self._row(c, v) for c, v in zip(chunks, vectors)]
        if not rows:
            return 0
        batch = max(1, get_settings().upsert_batch_size)
        async with self._sessions()() as session:
            async with session.begin():
                for start in range(0, len(rows), batch):
                    stmt = pg_insert(KnowledgeChunkRow).values(rows[start : start + batch])
                    await session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[KnowledgeChunkRow.chunk_id],
                            set_={col: stmt.excluded[col] for col in UPSERT_COLUMNS},
                        )
                    )
        return len(rows)

    async def delete_by_doc(self, doc_id: str) -> int:
        """Remove all chunks of a document (used for overwrite re-ingest)."""
        async with self._sessions()() as session:
            res = await session.execute(
                delete(KnowledgeChunkRow).where(KnowledgeChunkRow.doc_id == doc_id)
            )
            await session.commit()
        return int(res.rowcount or 0)

    async def update_acl_by_doc(
        self, doc_id: str, visibility: str, owner_id: str, dept_id: str, allowed_roles: str
    ) -> int:
        """Rewrite the ACL metadata on every chunk row of a document.

        pgvector 支持标量列原地 UPDATE: 向量不变、HNSW 索引不受影响, 这正是旧
        Milvus 方案必须「读回整行含 embedding 再 upsert」的地方。
        """
        async with self._sessions()() as session:
            res = await session.execute(
                update(KnowledgeChunkRow)
                .where(KnowledgeChunkRow.doc_id == doc_id)
                .values(
                    visibility=visibility[:16],
                    owner_id=owner_id[:64],
                    dept_id=dept_id[:64],
                    allowed_roles=allowed_roles[:128],
                )
            )
            await session.commit()
        return int(res.rowcount or 0)

    # -------------------------------------------------------------------- 检索
    async def search(
        self,
        query_vector: Sequence[float],
        top_k: int,
        principal: Principal | None = None,
    ) -> list[KnowledgeChunk]:
        """ANN cosine TopK over child chunks; score = cosine distance (越小越相关).

        ``is_parent = false`` 与 ACL 谓词都作用在 ORDER BY ... LIMIT 之前, 与 ES
        通道的 bool filter 语义一致。score 是「距离」而非旧 Milvus 的「相似度」,
        只用于排序与 RRF 融合(下游会被 RRF/rerank 分数覆盖)—— 全链路唯一相关性
        阈值本来就只作用在 rerank 阶段, 故无需在此设阈值。
        """
        dist = KnowledgeChunkRow.embedding.cosine_distance(list(query_vector))
        stmt = (
            select(KnowledgeChunkRow, dist.label("score"))
            .where(KnowledgeChunkRow.is_parent.is_(False))
            .order_by(dist)
            .limit(top_k)
        )
        if (acl := build_sql_filter(principal)) is not None:
            stmt = stmt.where(acl)
        async with self._sessions()() as session:
            # HNSW 默认 ef_search=100: TopK 很小(默认 8)时够用, 但带标量过滤时
            # 候选会被筛掉一部分, 抬高 ef 以保证召回与旧 Milvus 持平。
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {max(100, top_k * 8)}"))
            hits = (await session.execute(stmt)).all()
            # 实体 -> DTO 必须在会话内完成: 下面的 rollback 会 expire 所有实体,
            # 出块后再取属性就是 DetachedInstanceError。
            chunks = [self._to_chunk(row, float(score)) for row, score in hits]
            await session.rollback()  # 结束 SET LOCAL 所在的隐式事务, 不污染连接池
        return chunks

    async def query_parents(self, parent_ids: Sequence[str]) -> dict[str, KnowledgeChunk]:
        """Fetch parent blocks by chunk_id (for context assembly)."""
        if not parent_ids:
            return {}
        async with self._sessions()() as session:
            rows = (
                await session.execute(
                    select(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.chunk_id.in_(list(parent_ids))
                    )
                )
            ).scalars().all()
            return {r.chunk_id: self._to_chunk(r) for r in rows}

    async def iter_child_chunks(self, limit: int = 100000) -> list[KnowledgeChunk]:
        """Return all child chunks (BM25 corpus rebuild)."""
        async with self._sessions()() as session:
            rows = (
                await session.execute(
                    select(KnowledgeChunkRow)
                    .where(KnowledgeChunkRow.is_parent.is_(False))
                    .order_by(KnowledgeChunkRow.chunk_id)
                    .limit(limit)
                )
            ).scalars().all()
            chunks = [self._to_chunk(r) for r in rows]
        if len(chunks) >= limit:
            logger.warning(
                "BM25 corpus hit the row limit (%d); sparse channel is partial, raise the limit", limit
            )
        return chunks

    async def count(self) -> int:
        """Return number of stored chunks (parents + children)."""
        async with self._sessions()() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(KnowledgeChunkRow)
                    )
                ).scalar_one()
            )
