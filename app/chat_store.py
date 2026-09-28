"""页面会话记录的持久化 DAO (PostgreSQL: chat_sessions / chat_messages)。

与 Session Memory(Redis, 有 TTL, 供 prompt 上下文) 分工不同: 这里保存的是
"页面上可回看的完整聊天记录"(含思考过程/路由/参考来源), 供前端刷新、重进
页面后恢复历史。写路径由 ``persist_memory`` 节点在每轮结束时调用。

降级策略与记忆层一致: DB 不可用只记 warning 不抛出, 聊天链路不能被历史
记录保存失败阻断; 读路径(DB 不可用/表不存在)返回空结果。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChatMessage, ChatSession
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

# 会话标题取首条用户消息的前 N 个字符
_TITLE_LEN = 30


class ChatStore:
    """会话/消息的异步 DAO; 构造期不做任何 I/O。"""

    def __init__(self) -> None:
        self._factory: async_sessionmaker[AsyncSession] | None = None

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            self._factory = get_session_factory()
        return self._factory

    async def save_turn(
        self,
        *,
        session_id: str,
        user_id: str,
        role: str,
        department: str,
        trace_id: str,
        user_message: str,
        answer: str,
        thinking: str = "",
        route: str = "",
        target: str = "",
        intent: str = "",
        docs_meta: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> int | None:
        """upsert 会话 + 追加 user/assistant 两条消息, 返回助手消息 id。

        任何 DB 异常都吞掉并返回 None(降级不阻断对话), 由调用方无需处理。
        """
        if not session_id:
            return None
        try:
            async with self._sessions()() as session:
                async with session.begin():
                    title = user_message.strip()[:_TITLE_LEN] or "新会话"
                    # 标题只在首次插入时写入; 冲突时仅刷新 updated_at 与身份信息
                    await session.execute(
                        pg_insert(ChatSession)
                        .values(
                            id=session_id,
                            user_id=user_id,
                            role=role,
                            department=department,
                            title=title,
                        )
                        .on_conflict_do_update(
                            index_elements=["id"],
                            set_={
                                "user_id": user_id,
                                "role": role,
                                "department": department,
                                "updated_at": func.now(),
                            },
                        )
                    )
                    user_msg = ChatMessage(
                        session_id=session_id,
                        trace_id=trace_id,
                        role="user",
                        content=user_message,
                    )
                    session.add(user_msg)
                    await session.flush()  # 取得自增 id
                    ai_msg = ChatMessage(
                        session_id=session_id,
                        trace_id=trace_id,
                        role="assistant",
                        content=answer,
                        thinking=thinking or None,
                        route=route,
                        target=target or "",
                        intent=intent or "",
                        docs_meta=docs_meta or [],
                        artifacts=artifacts or [],
                    )
                    session.add(ai_msg)
                    await session.flush()
                    return int(ai_msg.id)
        except Exception as exc:  # noqa: BLE001 - 历史记录写失败绝不阻断对话
            logger.warning("会话记录保存失败(降级跳过): session=%s err=%s", session_id, exc)
            return None

    async def list_sessions(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """某用户最近的会话列表(按更新时间倒序)。"""
        try:
            stmt = (
                select(ChatSession)
                .where(ChatSession.user_id == user_id)
                .order_by(ChatSession.updated_at.desc())
                .limit(limit)
            )
            async with self._sessions()() as session:
                rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "session_id": r.id,
                    "title": r.title,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                    "updated_at": r.updated_at.isoformat() if r.updated_at else "",
                }
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话列表读取失败: %s", exc)
            return []

    async def get_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """一个会话的全部消息(时间正序), 供前端刷新后回填历史。"""
        try:
            stmt = (
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.asc())
                .limit(limit)
            )
            async with self._sessions()() as session:
                rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "id": r.id,
                    "session_id": r.session_id,
                    "trace_id": r.trace_id,
                    "role": r.role,
                    "content": r.content,
                    "thinking": r.thinking or "",
                    "route": r.route,
                    "target": r.target,
                    "intent": r.intent,
                    "docs_meta": r.docs_meta or [],
                    "artifacts": r.artifacts or [],
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话消息读取失败: session=%s err=%s", session_id, exc)
            return []

    async def delete_session(self, session_id: str) -> None:
        """删除一个会话及其全部消息。"""
        try:
            async with self._sessions()() as session:
                async with session.begin():
                    await session.execute(
                        delete(ChatMessage).where(ChatMessage.session_id == session_id)
                    )
                    await session.execute(
                        delete(ChatSession).where(ChatSession.id == session_id)
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话删除失败: session=%s err=%s", session_id, exc)


_chat_store: ChatStore | None = None


def get_chat_store() -> ChatStore:
    """进程级单例会话存储。"""
    global _chat_store
    if _chat_store is None:
        _chat_store = ChatStore()
    return _chat_store
