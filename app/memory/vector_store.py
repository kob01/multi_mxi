"""个人记忆的 Vector 通道: PostgreSQL + pgvector 的 ``long_term_memories`` 表。

按 ``user_id`` 严格隔离 —— 与知识库文档的 public/dept/role 可见性模型不同,
这里没有"共享给同事"的语义, 任何查询都必须带 ``user_id`` 谓词, 否则会跨用户
泄露对话内容。

一个 ``kind`` 列承载全部个人记忆桶(偏好/习惯/情节/知识, 见
``app/memory/taxonomy.py``): 各桶形状都是"一段文本 + 向量", 分表只会把一次召回
变成多次 UNION。因此**语义查重也限定在同 kind 内** —— 否则一条"喜欢 Markdown"
的偏好会被语义相近的知识条目吞掉, 变成另一条记录。

两条读路径按桶的注入方式分开: ``list_recent`` 是纯标量直读(偏好/习惯, 每轮都
该带, 与当轮问法无关), ``search_by_buckets`` 是语义召回(情节/知识, 相关才带);
``search_by_buckets`` 一次查询覆盖多桶, 只算一次 embedding。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import text as sa_text

from app.config import get_settings
from app.db.models import LongTermMemoryRow
from app.db.session import get_session_factory
from app.rag.embeddings import OllamaEmbedder

logger = logging.getLogger(__name__)

# 召回时多取一些再按桶裁剪(每桶 Top-K 不同, 一次 SQL 拿够, 避免每桶查一次)。
_OVERFETCH = 3


def _aware(value: datetime | None) -> datetime | None:
    """naive 时间按 UTC 补齐(容器默认 UTC, 老行可能是 naive)。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_same_observation(existing: datetime | None, incoming: datetime | None, window_days: int) -> bool:
    """两条时间是不是"同一件事的再一次说法": 任一没时间或相差在窗口内即算同一观测。

    相差超出窗口就是同一事实的**不同时刻观测**(如"现在 70kg"与"2015 年秋 64kg"),
    这种必须各存一条 —— 语义查重救的是"同一件事反复说", 不是"不同时间同一指标"。
    """
    if existing is None or incoming is None:
        return True
    return abs((_aware(incoming) - _aware(existing)).days) <= window_days


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    """取两者中较新的时间(空值忽略): 合并时 ``occurred_at`` 只往前走, 不回退。"""
    stamps = [s for s in (_aware(a), _aware(b)) if s is not None]
    return max(stamps) if stamps else None


@dataclass
class MemoryHit:
    """一条记忆 + 召回信息(分数为 cosine 相似度, 越大越相关)。"""

    id: int
    content: str
    kind: str
    title: str = ""
    score: float = 0.0
    source: str = "turn"
    occurred_at: datetime | None = None
    created_at: datetime | None = None


def _hit(
    row: LongTermMemoryRow,
    *,
    score: float = 0.0,
    title: str | None = None,
    kind: str | None = None,
) -> MemoryHit:
    return MemoryHit(
        id=int(row.id),
        content=row.content or "",
        kind=kind if kind is not None else row.kind,
        title=row.title if title is None else title,
        score=score,
        source=row.source or "turn",
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


class LongTermMemoryStore:
    """跨会话个人记忆的向量存储与召回 (按 user_id 隔离)。"""

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
        title: str = "",
        source: str = "turn",
        occurred_at: datetime | None = None,
    ) -> int:
        """查重后写入一条记忆, 返回记忆 id(新建或复用既有 id 均返回)。

        查重范围是"同用户 + 同桶": 跨桶查重会把语义相近但用途不同的记录合并,
        例如把偏好"回复用中文"和知识"项目语言是中文"当成一条。

        命中查重也不等于直接覆盖: 两条 ``occurred_at`` 相差超出
        ``memory_observation_window_days`` 时当作"同一事实的不同时刻观测"各存一条,
        否则"2015 年我 64kg"会把"现在 70kg"整条抹掉(而这两条都是有用的);
        留在窗口内才真的合并, 且 ``occurred_at`` 只往前走不后退。
        """
        if not user_id or not content.strip():
            return 0
        settings = get_settings()
        vec = await self.embedder.embed_query(content)
        async with self._sessions()() as session:
            async with session.begin():
                dist = LongTermMemoryRow.embedding.cosine_distance(list(vec))
                stmt = (
                    select(
                        LongTermMemoryRow.id,
                        LongTermMemoryRow.content,
                        LongTermMemoryRow.occurred_at,
                        dist.label("dist"),
                    )
                    .where(
                        LongTermMemoryRow.user_id == user_id,
                        LongTermMemoryRow.kind == kind,
                    )
                    .order_by(dist)
                    .limit(1)
                )
                await session.execute(sa_text(f"SET LOCAL hnsw.ef_search = {max(100, 10)}"))
                row = (await session.execute(stmt)).first()
                similarity = 1.0 - float(row.dist) if row else 0.0
                # 命中查重不等于"同一条记忆": 时间超出观测窗口就是同一事实的不同时刻
                # 观测(体重 70kg vs 2015 年 64kg), 那种各存一条, 读路径按较新的锚点排序。
                same_observation = bool(row) and _is_same_observation(
                    row.occurred_at, occurred_at, settings.memory_observation_window_days
                )
                if row and similarity >= settings.memory_dedup_threshold and same_observation:
                    # 命中查重 = 同一条记忆的又一次表述: 保留信息更完整的那一句,
                    # 不能让本轮可能更短的说法把已有富表述冲掉(如"喜欢单板滑雪"
                    # 覆盖掉"最喜欢的运动是单板滑雪和爬山")。
                    old_content = (row.content or "").strip()
                    merged_content = content if len(content) >= len(old_content) else old_content
                    # embedding 严格对齐最终 content: 采用本轮文本时直接复用函数开头
                    # 已算好的 vec(零额外 embedding); 保留既有更长文本时重算其向量,
                    # 顺带修复历史上"改过 content 却没同步 embedding"的漂移行。
                    merged_vec = (
                        vec
                        if merged_content == content
                        else await self.embedder.embed_query(merged_content)
                    )
                    values: dict[str, object] = {
                        "content": merged_content,
                        "embedding": list(merged_vec),
                        "kind": kind,
                        "source": source,
                        "occurred_at": _later(row.occurred_at, occurred_at),
                        "last_accessed_at": datetime.now(timezone.utc),
                    }
                    # 命中查重时不抹掉已有标题(本轮提取常常不带 title)。
                    if title.strip():
                        values["title"] = title.strip()
                    await session.execute(
                        update(LongTermMemoryRow)
                        .where(LongTermMemoryRow.id == row.id)
                        .values(**values)
                    )
                    return int(row.id)
                memory = LongTermMemoryRow(
                    user_id=user_id,
                    kind=kind,
                    title=title.strip()[:128],
                    content=content,
                    source=source,
                    occurred_at=occurred_at,
                    source_session_id=source_session_id,
                    embedding=list(vec),
                )
                session.add(memory)
                await session.flush()
                return int(memory.id)

    async def search_memories(
        self, user_id: str, query: str, top_k: int | None = None
    ) -> list[tuple[str, str, float]]:
        """按语义召回某用户的全部记忆, 返回 ``(content, kind, score)`` 列表。

        这是个人级分桶之前的老接口, 现在只服务 ``personal_memory_enabled=false``
        的降级路径; 新代码请用 ``search_by_buckets``。

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

    async def search_by_buckets(
        self,
        user_id: str,
        query: str,
        kinds: Sequence[str],
        limit: int | None = None,
    ) -> list[MemoryHit]:
        """一次 embedding + 一次查询, 按语义召回多个桶的记忆(按分数倒序)。

        每桶最终留几条交由调用方裁剪: 桶的 Top-K 不一样, 且情节还要叠加时间衰减
        重排, 在 SQL 里表达反而更绕; 这里只多取一点(``_OVERFETCH`` 倍)保证够裁。
        """
        if not user_id or not kinds or not query.strip():
            return []
        kinds = [k for k in kinds if k]
        limit = limit or get_settings().long_term_memory_top_k * len(kinds)
        query_vec = await self.embedder.embed_query(query)
        dist = LongTermMemoryRow.embedding.cosine_distance(list(query_vec))
        stmt = (
            select(LongTermMemoryRow, dist.label("dist"))
            .where(
                LongTermMemoryRow.user_id == user_id,
                LongTermMemoryRow.kind.in_(list(kinds)),
            )
            .order_by(dist)
            .limit(limit * _OVERFETCH)
        )
        async with self._sessions()() as session:
            await session.execute(sa_text(f"SET LOCAL hnsw.ef_search = {max(100, limit * 8)}"))
            rows = (await session.execute(stmt)).all()
            # 必须在 rollback 之前把 ORM 行转成普通 dataclass: rollback 会 expire 所有
            # 实例, 出了会话再取属性就是"detached instance"报错(整桶召回静默变空)。
            hits = [_hit(row, score=1.0 - float(d)) for row, d in rows]
            await session.rollback()  # 结束 SET LOCAL 事务, 不污染连接池
        return hits

    async def list_recent(
        self,
        user_id: str,
        kinds: Sequence[str],
        limit: int = 5,
        *,
        order_by: str = "last_accessed_at",
    ) -> list[MemoryHit]:
        """纯标量直读某用户若干桶的最近记忆(不做向量检索, 零 embedding 成本)。"""
        if not user_id or not kinds:
            return []
        column = (
            LongTermMemoryRow.created_at
            if order_by == "created_at"
            else LongTermMemoryRow.last_accessed_at
        )
        stmt = (
            select(LongTermMemoryRow)
            .where(
                LongTermMemoryRow.user_id == user_id,
                LongTermMemoryRow.kind.in_([k for k in kinds if k]),
            )
            .order_by(column.desc())
            .limit(limit)
        )
        async with self._sessions()() as session:
            rows = (await session.execute(stmt)).scalars().all()
            # 同上: 离开会话前完成 ORM -> dataclass 的转换。
            return [_hit(r) for r in rows]

    async def count_since(self, user_id: str, kind: str, since: datetime | None) -> int:
        """统计某桶自 ``since`` 以来的新增条数(情节蒸馏门槛判定用)。"""
        if not user_id:
            return 0
        stmt = (
            select(func.count())
            .select_from(LongTermMemoryRow)
            .where(LongTermMemoryRow.user_id == user_id, LongTermMemoryRow.kind == kind)
        )
        if since is not None:
            stmt = stmt.where(LongTermMemoryRow.created_at > since)
        async with self._sessions()() as session:
            return int((await session.execute(stmt)).scalar() or 0)

    async def count_by_kind(self, user_id: str) -> dict[str, int]:
        """一次 GROUP BY 拿到各桶的总条数(前端记忆页的统计概览)。"""
        if not user_id:
            return {}
        stmt = (
            select(LongTermMemoryRow.kind, func.count())
            .where(LongTermMemoryRow.user_id == user_id)
            .group_by(LongTermMemoryRow.kind)
        )
        async with self._sessions()() as session:
            rows = (await session.execute(stmt)).all()
        return {str(kind): int(total) for kind, total in rows}

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

    async def delete_items(self, user_id: str, memory_ids: Sequence[int]) -> int:
        """删除若干条记忆(限定 user_id, 不允许越权删别人的记录), 返回实际删除数。"""
        if not user_id or not memory_ids:
            return 0
        async with self._sessions()() as session:
            result = await session.execute(
                delete(LongTermMemoryRow).where(
                    LongTermMemoryRow.user_id == user_id,
                    LongTermMemoryRow.id.in_(list(memory_ids)),
                )
            )
            await session.commit()
        return int(result.rowcount or 0)

    async def merge_group(
        self, user_id: str, keep_id: int, content: str, drop_ids: Sequence[int]
    ) -> int:
        """把一个语义重复组归并成一条: 保留 ``keep_id`` 并刷新其文本, 删除组内其余。

        所有操作都限定 ``user_id`` 且排除 ``keep_id`` 本身, 返回实际删除行数。
        重算保留行的 embedding(文本变了, 不重算会让后续查重/召回对新文本失准)。
        """
        keep_id = int(keep_id)
        drop = [int(i) for i in drop_ids if int(i) != keep_id]
        if not user_id or not drop or not content.strip():
            return 0
        vec = await self.embedder.embed_query(content)
        async with self._sessions()() as session:
            await session.execute(
                update(LongTermMemoryRow)
                .where(LongTermMemoryRow.user_id == user_id, LongTermMemoryRow.id == keep_id)
                .values(content=content.strip(), embedding=list(vec),
                        last_accessed_at=datetime.now(timezone.utc))
            )
            result = await session.execute(
                delete(LongTermMemoryRow).where(
                    LongTermMemoryRow.user_id == user_id,
                    LongTermMemoryRow.id.in_(drop),
                )
            )
            await session.commit()
        return int(result.rowcount or 0)

    async def update_memory_content(
        self, user_id: str, memory_id: int, content: str, title: str = ""
    ) -> bool:
        """原地刷新一条记忆: 换文本、重算 embedding, 返回是否命中该行。

        显式"记一下"的同话题更新走这里 —— 文本变了不重算向量, 后续查重/召回
        会对新文本失准(与 ``merge_group`` 保留行同一口径)。限定 ``user_id``
        防越权改别人的行; ``title`` 非空才覆盖(本轮没给主题就留原主题)。
        """
        memory_id = int(memory_id)
        if not user_id or not memory_id or not content.strip():
            return False
        vec = await self.embedder.embed_query(content)
        values: dict[str, object] = {
            "content": content.strip(),
            "embedding": list(vec),
            "last_accessed_at": datetime.now(timezone.utc),
        }
        if title.strip():
            values["title"] = title.strip()[:60]
        async with self._sessions()() as session:
            result = await session.execute(
                update(LongTermMemoryRow)
                .where(LongTermMemoryRow.user_id == user_id, LongTermMemoryRow.id == memory_id)
                .values(**values)
            )
            await session.commit()
        return int(result.rowcount or 0) > 0

    async def clear_bucket(self, user_id: str, kind: str) -> int:
        """清空某用户某个桶, 返回删除条数。"""
        if not user_id or not kind:
            return 0
        async with self._sessions()() as session:
            result = await session.execute(
                delete(LongTermMemoryRow).where(
                    LongTermMemoryRow.user_id == user_id,
                    LongTermMemoryRow.kind == kind,
                )
            )
            await session.commit()
        return int(result.rowcount or 0)


_memory_store: LongTermMemoryStore | None = None


def get_long_term_store() -> LongTermMemoryStore:
    """进程级单例长期记忆存储。"""
    global _memory_store
    if _memory_store is None:
        _memory_store = LongTermMemoryStore()
    return _memory_store
