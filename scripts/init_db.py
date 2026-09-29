"""PostgreSQL bootstrap / connectivity self-check.

Reads the database password from the PG_PASSWORD environment variable (or the
.env file loaded by pydantic-settings), ensures the pgvector extension exists,
then creates every metadata table in ``Base.metadata`` if missing (业务表 +
父子双表 doc_parents/doc_chunks + 记忆表 + 会话记录表; 旧单表 knowledge_chunks
仍在 metadata 里供迁移脚本读写)。

Usage:
    python -m scripts.init_db
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.session import get_engine, init_schema


async def _vector_version() -> str:
    """Installed pgvector version, or a hint when the extension is missing."""
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        return (
            await session.execute(
                text("SELECT COALESCE((SELECT extversion FROM pg_extension WHERE extname = 'vector'), '(not installed)')")
            )
        ).scalar_one()


async def main() -> None:
    engine = get_engine()  # getpass prompt happens here
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version()"))).scalar()
    print(f"[init_db] connected, {version}")
    await init_schema()
    print(f"[init_db] pgvector extension: {await _vector_version()}")
    print(
        "[init_db] tables ready (documents / tags / doc_parents / doc_chunks / "
        "long_term_memories / user_profiles / chat_* / hr_* / fin_*; 旧 knowledge_chunks 仅供迁移)"
    )


if __name__ == "__main__":
    asyncio.run(main())
