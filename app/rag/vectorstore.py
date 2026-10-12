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
from datetime import datetime, timezone
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
# ``deprecated_at`` 必须列在内并置 NULL: 同 chunk_id 重入库时上一轮被标成陈旧的行
# 现在又是当前版本了, 不重置就会被 GC 当成旧行真删(表现为"入库成功但检索不到")。
CHUNK_UPSERT_COLUMNS = (
    "doc_id", "parent_id", "chunk_index", "ord", "chunk_text", "content_hash",
    "char_count", "embedding_model", "title", "source", "modality", "section",
    "page_no", "embedding", "extra", "deprecated_at", *ACL_COLUMNS,
)
PARENT_UPSERT_COLUMNS = (
    "doc_id", "ord", "parent_type", "title", "section", "page_no", "start_offset",
    "end_offset", "content_hash", "char_count", "child_count", "normalizer_version",
    "extra", "deprecated_at", *ACL_COLUMNS,
)

# 检索侧的统一门禁: 已标陈旧的行(本轮发布不再引用 / 文档已删除)在任何读路径都不可见。
# 与 documents.status 的发布态门禁不同, 这一道是行级的、与新行落地同事务生效。
_LIVE = DocChunkRow.deprecated_at.is_(None)
_LIVE_PARENT = DocParentRow.deprecated_at.is_(None)


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


def _chunk_from_corpus_row(row: Any, content: str) -> KnowledgeChunk:
    """语料行(含 chunk_text 与 ACL 列) -> 子块 DTO: BM25 重建/单文档投影的唯一构造点。

    **ACL 四列必须跟着走**: ES 的 ``_acl_filter`` 读的就是索引里的 visibility/owner_id/
    dept_id/allowed_roles。历史上这里只按 ``_NARROW_ATTRS`` 解包(不含 ACL), 于是私有文档
    写进 ES 的永远是默认 public —— 稠密通道拦住的那篇文档, 稀疏通道会把它送进候选集。
    那个错只在"权限文档 + 关键词命中"同时出现时暴露, 所以必须留这段说明。
    """
    return KnowledgeChunk(
        **{k: getattr(row, k) for k in _NARROW_ATTRS},
        content=content or "",
        is_parent=False,
        visibility=getattr(row, "visibility", None) or "public",
        owner_id=getattr(row, "owner_id", None) or "",
        dept_id=getattr(row, "dept_id", None) or "",
        allowed_roles=getattr(row, "allowed_roles", None) or "",
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
        "deprecated_at": None,
    }
    if v is not None:
        row["embedding"] = list(v)
    return row


async def upsert_rows(
    session: AsyncSession, dialect_insert, rows: list[dict], pk_col, upsert_cols
) -> None:
    """一批 ``INSERT ... ON CONFLICT (pk) DO UPDATE``(语句本身不提交, 跟调用方事务)。"""
    stmt = dialect_insert.values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[pk_col],
            set_={col: stmt.excluded[col] for col in upsert_cols},
        )
    )


async def publish_in_session(
    session: AsyncSession,
    doc_id: str,
    parent_rows: Sequence[ParentBlock],
    chunks: Sequence[KnowledgeChunk],
    vectors: Sequence[Sequence[float] | None],
) -> tuple[int, list[str]]:
    """在调用方事务内发布父子块: upsert 新行 -> 把不再引用的行标陈旧。

    差集只与**现行行**比(两边都带 ``deprecated_at IS NULL``): 上一轮已标陈旧、这轮
    又不再引用的行不需要再刷一次时间戳, 把它们拉进差集只会把 GC 的宽限期反复推开。
    """
    assert len(chunks) == len(vectors), "chunks/vectors length mismatch"
    batch = max(1, get_settings().upsert_batch_size)
    new_chunk_ids = {c.chunk_id for c in chunks}
    new_parent_ids = {p.parent_id for p in parent_rows}
    chunk_rows = [_chunk_row(c, v) for c, v in zip(chunks, vectors)]
    p_rows = [_parent_row(p) for p in parent_rows]
    old_chunk_ids = set(
        (
            await session.execute(
                select(DocChunkRow.chunk_id).where(DocChunkRow.doc_id == doc_id, _LIVE)
            )
        ).scalars().all()
    )
    old_parent_ids = set(
        (
            await session.execute(
                select(DocParentRow.parent_id).where(DocParentRow.doc_id == doc_id, _LIVE_PARENT)
            )
        ).scalars().all()
    )
    for start in range(0, len(p_rows), batch):
        await upsert_rows(
            session, pg_insert(DocParentRow), p_rows[start : start + batch],
            DocParentRow.parent_id, PARENT_UPSERT_COLUMNS,
        )
    for start in range(0, len(chunk_rows), batch):
        await upsert_rows(
            session, pg_insert(DocChunkRow), chunk_rows[start : start + batch],
            DocChunkRow.chunk_id, CHUNK_UPSERT_COLUMNS,
        )
    now = datetime.now(timezone.utc)
    stale_children = sorted(old_chunk_ids - new_chunk_ids)
    stale_parents = sorted(old_parent_ids - new_parent_ids)
    for start in range(0, len(stale_children), batch):
        await session.execute(
            update(DocChunkRow)
            .where(DocChunkRow.chunk_id.in_(stale_children[start : start + batch]))
            .values(deprecated_at=now)
        )
    for start in range(0, len(stale_parents), batch):
        await session.execute(
            update(DocParentRow)
            .where(DocParentRow.parent_id.in_(stale_parents[start : start + batch]))
            .values(deprecated_at=now)
        )
    return len(chunk_rows), stale_children


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
            .where(_LIVE)
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
                            DocChunkRow.chunk_id.in_(batch), _LIVE
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
        """一次查询拿到该 doc 全部**现行**旧块 content_hash(增量 embed 判定用)。

        必须排除陈旧行: 否则刚被标陈旧的旧块会让增量判定误以为"正文没变、可以
        不重 embed", 而 GC 马上就要真删这行 —— 新行会带着 NULL embedding 发布出去。
        """
        async with self._sessions()() as session:
            rows = (
                await session.execute(
                    select(DocChunkRow.chunk_id, DocChunkRow.content_hash).where(
                        DocChunkRow.doc_id == doc_id, _LIVE
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
                            DocChunkRow.chunk_id.in_(batch), _LIVE
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
                        .where(DocChunkRow.chunk_id > after, _LIVE)
                        .order_by(DocChunkRow.chunk_id)
                        .limit(page_size)
                    )
                ).all()
            if not rows:
                return
            batch = [_chunk_from_corpus_row(r, r.chunk_text) for r in rows]
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
        await upsert_rows(session, dialect_insert, rows, pk_col, upsert_cols)

    async def delete_by_doc(self, doc_id: str) -> int:
        """真删某文档全部子块(仅供脚本/迁移使用; 主链路走标陈旧 + GC)。"""
        async with self._sessions()() as session:
            res = await session.execute(
                delete(DocChunkRow).where(DocChunkRow.doc_id == doc_id)
            )
            await session.commit()
        return int(res.rowcount or 0)

    async def list_doc_chunks(self, doc_id: str) -> list[KnowledgeChunk]:
        """取某文档现行全部子块(含正文): ES 单文档增量重建的唯一语料源。

        不要求调用方传分页: 单篇文档的子块量在几千以内, 而 ES 侧本来就要逐条算
        jieba 分词; 真正的海量场景走 :meth:`iter_chunk_corpus`(全库流式)。
        """
        out: list[KnowledgeChunk] = []
        after = ""
        page = get_settings().mongo_batch_page_size
        while True:
            async with self._sessions()() as session:
                rows = (
                    await session.execute(
                        select(*NARROW_COLUMNS, DocChunkRow.chunk_text)
                        .where(DocChunkRow.doc_id == doc_id, _LIVE,
                               DocChunkRow.chunk_id > after)
                        .order_by(DocChunkRow.chunk_id)
                        .limit(page)
                    )
                ).all()
            if not rows:
                break
            out.extend(_chunk_from_corpus_row(r, r.chunk_text) for r in rows)
            after = rows[-1].chunk_id
            if len(rows) < page:
                break
        return out

    async def publish_and_mark(
        self,
        doc_id: str,
        parent_rows: Sequence[ParentBlock],
        chunks: Sequence[KnowledgeChunk],
        vectors: Sequence[Sequence[float] | None],
        session: AsyncSession | None = None,
    ) -> tuple[int, list[str]]:
        """发布一篇文档的父子块: upsert 新行 + 把本次不再引用的行**标陈旧**。

        返回 ``(写入子块数, 被标陈旧的子块 id)``。旧版本在一个事务里"先删子块
        再删父块": 单事务里 DELETE 几万行会长时间持锁, 而且并发改同一篇文档时
        会消失一瞬间的空窗。改成同事务 UPDATE 标陈旧: 旧行与新行在同一瞬间完成
        交接(检索侧 ``deprecated_at IS NULL`` 立刻只看到新行), 物理删除由后台 GC
        分批做, 主链路不再做大事务 DELETE。

        ``session`` 传入时复用调用方事务(入库流水线需要与 documents/tags/outbox
        同事务提交), 不传时自己开一个事务(脚本/评测旧路径)。
        """
        if session is not None:
            return await publish_in_session(session, doc_id, parent_rows, chunks, vectors)
        async with self._sessions()() as new_session:
            async with new_session.begin():
                return await publish_in_session(
                    new_session, doc_id, parent_rows, chunks, vectors
                )

    async def publish_parent_child(
        self,
        parent_store: "ParentStore",
        doc_id: str,
        parent_rows: Sequence[ParentBlock],
        chunks: Sequence[KnowledgeChunk],
        vectors: Sequence[Sequence[float] | None],
    ) -> int:
        """[兼容薄封装] 旧脚本/评测入口: 自己开事务发布, 只回写入行数。

        ``parent_store`` 参数已无用(父子发布本来就在同一个 PG 事务里), 保留只为
        不破坏已有调用点; 新链路请直用 :meth:`publish_and_mark`。
        """
        written, _marked = await self.publish_and_mark(doc_id, parent_rows, chunks, vectors)
        return written

    async def mark_deprecated_by_doc(
        self, session: AsyncSession, doc_id: str
    ) -> tuple[int, int]:
        """在调用方事务里把一篇文档的父子块全部标陈旧(文档删除主链路用)。

        删除与"重入库后旧块不再引用"是同一种状态: 都是"行还在、但对检索不可见、
        等 GC 真删"。把它归一到 GC 一条路径上, 就不需要在大文档删除时先跑一次
        万行 DELETE(同一个锁与空窗问题)。
        """
        now = datetime.now(timezone.utc)
        cres = await session.execute(
            update(DocChunkRow)
            .where(DocChunkRow.doc_id == doc_id, _LIVE)
            .values(deprecated_at=now)
        )
        pres = await session.execute(
            update(DocParentRow)
            .where(DocParentRow.doc_id == doc_id, _LIVE_PARENT)
            .values(deprecated_at=now)
        )
        return int(cres.rowcount or 0), int(pres.rowcount or 0)

    async def delete_deprecated(self, before: datetime, limit: int) -> tuple[int, int]:
        """GC: 分批真删宽限期已过的陈旧行; 返回 ``(删子块数, 删父块数)``。

        先子后父(避开将来父子外键的反序依赖), 每轮最多 ``limit`` 行: 一次删完全部
        历史陈旧行会把单个事务做大、锁住整张块表。
        """
        async with self._sessions()() as session:
            async with session.begin():
                child_ids = list(
                    (
                        await session.execute(
                            select(DocChunkRow.chunk_id)
                            .where(DocChunkRow.deprecated_at.isnot(None),
                                   DocChunkRow.deprecated_at < before)
                            .limit(limit)
                        )
                    ).scalars().all()
                )
                cdel = 0
                batch = max(1, get_settings().upsert_batch_size)
                for start in range(0, len(child_ids), batch):
                    res = await session.execute(
                        delete(DocChunkRow).where(
                            DocChunkRow.chunk_id.in_(child_ids[start : start + batch])
                        )
                    )
                    cdel += int(res.rowcount or 0)
                parent_ids = list(
                    (
                        await session.execute(
                            select(DocParentRow.parent_id)
                            .where(DocParentRow.deprecated_at.isnot(None),
                                   DocParentRow.deprecated_at < before)
                            .order_by(DocParentRow.parent_id)
                            .limit(limit)
                        )
                    ).scalars().all()
                )
                pdel = 0
                for start in range(0, len(parent_ids), batch):
                    res = await session.execute(
                        delete(DocParentRow).where(
                            DocParentRow.parent_id.in_(parent_ids[start : start + batch])
                        )
                    )
                    pdel += int(res.rowcount or 0)
        return cdel, pdel

    async def has_deprecated(self, before: datetime) -> bool:
        """是否还有可回收的陈旧行(GC 循环的终止条件, 不用拉全量 id)。"""
        async with self._sessions()() as session:
            chunk = (
                await session.execute(
                    select(DocChunkRow.chunk_id)
                    .where(DocChunkRow.deprecated_at.isnot(None),
                           DocChunkRow.deprecated_at < before)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if chunk is not None:
                return True
            parent = (
                await session.execute(
                    select(DocParentRow.parent_id)
                    .where(DocParentRow.deprecated_at.isnot(None),
                           DocParentRow.deprecated_at < before)
                    .limit(1)
                )
            ).scalar_one_or_none()
        return parent is not None

    async def update_acl_by_doc(
        self, doc_id: str, visibility: str, owner_id: str, dept_id: str, allowed_roles: str,
        session: AsyncSession | None = None,
    ) -> tuple[int, int]:
        """两条 UPDATE(doc_chunks + doc_parents); 返回 (chunks, parents)。

        ``session`` 传入时不自己提交 —— 权限变更必须与 ``documents`` 元数据行
        **同一事务**(旧写法分两个事务: 第一个提交了、第二个挂了, 就是"元数据说私有、
        块表还是 public"的越权窗口)。
        """
        vals = {
            "visibility": visibility[:16],
            "owner_id": owner_id[:64],
            "dept_id": dept_id[:64],
            "allowed_roles": allowed_roles[:128],
        }
        own = session is None
        if own:
            async with self._sessions()() as new_session:
                async with new_session.begin():
                    return await self._acl_update(new_session, doc_id, vals)
        assert session is not None
        return await self._acl_update(session, doc_id, vals)

    @staticmethod
    async def _acl_update(
        session: AsyncSession, doc_id: str, vals: dict[str, str]
    ) -> tuple[int, int]:
        cres = await session.execute(
            update(DocChunkRow).where(DocChunkRow.doc_id == doc_id).values(**vals)
        )
        pres = await session.execute(
            update(DocParentRow).where(DocParentRow.doc_id == doc_id).values(**vals)
        )
        return int(cres.rowcount or 0), int(pres.rowcount or 0)

    async def counts(self) -> dict[str, int]:
        """现行行数(不含待 GC 的陈旧行): 运维口径要和"能被检索到的量"一致。"""
        async with self._sessions()() as session:
            n = int(
                (
                    await session.execute(
                        select(func.count()).select_from(DocChunkRow).where(_LIVE)
                    )
                ).scalar_one()
            )
            m = int(
                (
                    await session.execute(
                        select(func.count()).select_from(DocParentRow).where(_LIVE_PARENT)
                    )
                ).scalar_one()
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
                            DocParentRow.parent_id.in_(ids[start : start + page]),
                            _LIVE_PARENT,
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

    async def list_ids(self, doc_id: str | None = None, *, include_deprecated: bool = False) -> set[str]:
        """父块 id 集。

        默认只返现行行(供运维看"应可检索的父块"); ``include_deprecated=True`` 时连
        已标陈旧的行一起返 —— Mongo 正文回收必须用这个口径: 陈旧行还在宽限期内时,
        PG 里该行随时可能被下一轮重入库把 ``deprecated_at`` 重置回现行(同 parent_id
        的 upsert), 此时它的正文假设已被删, 父块上下文就永久取不回来了。正文只在
        PG 行被物理删除后的那一轮 GC 里才应被清掉。
        """
        async with self._sessions()() as session:
            stmt = select(DocParentRow.parent_id)
            if not include_deprecated:
                stmt = stmt.where(_LIVE_PARENT)
            if doc_id is not None:
                stmt = stmt.where(DocParentRow.doc_id == doc_id)
            return set((await session.execute(stmt)).scalars().all())

    async def delete_stale(self, doc_id: str, keep_ids: set[str]) -> int:
        """真删该 doc 下不在 keep_ids 的父块(仅供脚本/迁移; 主链路走标陈旧 + GC)。"""
        batch = max(1, get_settings().upsert_batch_size)
        async with self._sessions()() as session:
            existing = set(
                (
                    await session.execute(
                        select(DocParentRow.parent_id).where(
                            DocParentRow.doc_id == doc_id, _LIVE_PARENT
                        )
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
        "deprecated_at": None,
    }


# 向后兼容别名: 旧 import(PgVectorStore)与评测脚本仍指向子块 store。
PgVectorStore = ChunkStore
