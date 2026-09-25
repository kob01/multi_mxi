"""长期记忆的 Vector 通道: PostgreSQL + pgvector 的 ``long_term_memories`` 表。

按 ``user_id`` 严格隔离 —— 与知识库文档的 public/dept/role 可见性模型不同,
这里没有"共享给同事"的语义, 任何查询都必须带 ``user_id`` 谓词, 否则会跨用户
泄露对话内容。

``upsert_memory`` 先做一次同用户内的 cosine 查重: 相似度 >=
``settings.memory_dedup_threshold`` 视为同一条事实(如用户两次提到"我负责研发
部"), 只刷新 ``last_accessed_at`` / ``content``, 不重复插入 —— 长期记忆是
"越写越浓缩"而非"越写越多", 不做查重会很快淹没在一次次的重复表述里。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import text as sa_text

from app.config import get_settings
from app.db.models import LongTermMemoryRow
from app.db.session import get_session_factory
from app.rag.embeddings import OllamaEmbedder

logger = logging.getLogger(__name__)


class LongTermMemoryStore:
    """跨会话长期记忆的向量存储与召回 (按 user_id 隔离)。"""

    def __init__(self) -> None:
        # 同 PgVectorStore: 构造期不做任何 I/O, engine 可能还没就绪。
        self._factory: async_sessionmaker[AsyncSession] | None = None
        self.embedder = OllamaEmbedder()

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            self._factory = get_session_factory()
        return self._factory

    async def upsert_memory(
        self,
        user_id: str,
        content: str,
        *,
        kind: str = "fact",
        source_session_id: str = "",
    ) -> int:
        """查重后写入一条长期记忆, 返回记忆 id(新建或复用既有 id 均返回)。"""
        if not user_id or not content.strip():
            return 0
        settings = get_settings()
        vec = await self.embedder.embed_query(content)
        async with self._sessions()() as session:
            async with session.begin():
                dist = LongTermMemoryRow.embedding.cosine_distance(list(vec))
                stmt = (
                    select(LongTermMemoryRow.id, dist.label("dist"))
                    .where(LongTermMemoryRow.user_id == user_id)
                    .order_by(dist)
                    .limit(1)
                )
                await session.execute(sa_text(f"SET LOCAL hnsw.ef_search = {max(100, 10)}"))
                row = (await session.execute(stmt)).first()
                similarity = 1.0 - float(row.dist) if row else 0.0
                if row and similarity >= settings.memory_dedup_threshold:
                    await session.execute(
                        update(LongTermMemoryRow)
                        .where(LongTermMemoryRow.id == row.id)
                        .values(
                            content=content,
                            kind=kind,
                            last_accessed_at=datetime.now(timezone.utc),
                        )
                    )
                    return int(row.id)
                memory = LongTermMemoryRow(
                    user_id=user_id,
                    kind=kind,
                    content=content,
                    source_session_id=source_session_id,
                    embedding=list(vec),
                )
                session.add(memory)
                await session.flush()
                return int(memory.id)

    async def search_memories(
        self, user_id: str, query: str, top_k: int | None = None
    ) -> list[tuple[str, str, float]]:
        """按语义召回某用户的长期记忆, 返回 ``(content, kind, score)`` 列表。

        ``score`` 是 cosine 相似度(越大越相关, 与 knowledge_chunks 的"距离"口径
        相反, 这里已经换算过, 便于拼接进 prompt 时直接判断相关性)。
        """
        if not user_id:
            return []
        settings = get_settings()
        top_k = top_k or settings.long_term_memory_top_k
        query_vec = await self.embedder.embed_query(query)
        dist = LongTermMemoryRow.embedding.cosine_distance(list(query_vec))
        stmt = (
            select(LongTermMemoryRow.content, LongTermMemoryRow.kind, dist.label("dist"))
            .where(LongTermMemoryRow.user_id == user_id)
            .order_by(dist)
            .limit(top_k)
        )
        async with self._sessions()() as session:
            await session.execute(sa_text(f"SET LOCAL hnsw.ef_search = {max(100, top_k * 8)}"))
            hits = (await session.execute(stmt)).all()
            await session.rollback()  # 结束 SET LOCAL 事务, 不污染连接池
        return [(content, kind, 1.0 - float(d)) for content, kind, d in hits]

    async def touch(self, memory_ids: Sequence[int]) -> None:
        """批量刷新 last_accessed_at(召回命中即"又用了一次", 供后续淘汰策略用)。"""
        if not memory_ids:
            return
        async with self._sessions()() as session:
            await session.execute(
                update(LongTermMemoryRow)
                .where(LongTermMemoryRow.id.in_(list(memory_ids)))
                .values(last_accessed_at=datetime.now(timezone.utc))
            )
            await session.commit()


_memory_store: LongTermMemoryStore | None = None


def get_long_term_store() -> LongTermMemoryStore:
    """进程级单例长期记忆存储。"""
    global _memory_store
    if _memory_store is None:
        _memory_store = LongTermMemoryStore()
    return _memory_store
