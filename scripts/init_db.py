"""PostgreSQL bootstrap / connectivity self-check.

Reads the database password from the PG_PASSWORD environment variable (or the
.env file loaded by pydantic-settings), ensures the pgvector extension exists,
then creates every metadata table in ``Base.metadata`` if missing (业务表 +
父子双表 doc_parents/doc_chunks + 记忆表 + 会话记录表; 旧单表 knowledge_chunks
仍在 metadata 里供迁移脚本读写)。

``init_schema()`` 尾部还会做层 1 的数据库隔离(作用域回填 + 最小权限角色 + RLS
策略 + 审计表不可变性), 本脚本的 ``--rls-status`` 把当前结论看一眼而已, 不改变任何东西。

Usage:
    python -m scripts.init_db
    python -m scripts.init_db --rls-status
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from app.config import get_settings
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
    args = _parser().parse_args()
    engine = get_engine()  # getpass prompt happens here
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version()"))).scalar()
    print(f"[init_db] connected, {version}")
    if args.rls_status:
        from app.db.rls import rls_status

        status = await rls_status()
        print(f"[rls] 策略覆盖表: {[p['table'] for p in status['policies']] or '(无)'}")
        print(f"[rls] FORCE 生效表: {status['forced_tables'] or '(无)'}")
        print(f"[rls] analytics 角色: {status['roles'] or '(未创建)'}")
        return
    await init_schema()
    print(f"[init_db] pgvector extension: {await _vector_version()}")
    print(
        "[init_db] tables ready (documents / tags / doc_parents / doc_chunks / "
        "long_term_memories / user_profiles / chat_* / hr_* / fin_* / proc_* / "
        "sys_departments / dataops_* / sql_audit_records; 旧 knowledge_chunks 仅供迁移)"
    )
    if get_settings().rls_enabled:
        from app.db.rls import rls_status

        status = await rls_status()
        print(
            f"[rls] 已对 {len(status['forced_tables'])} 张表 FORCE ROW LEVEL SECURITY; "
            f"角色: {[r['name'] for r in status['roles']] or '(未创建)'}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap PostgreSQL schema")
    parser.add_argument(
        "--rls-status", action="store_true", help="只看当前 RLS/角色结论, 不执行任何 DDL"
    )
    return parser


if __name__ == "__main__":
    asyncio.run(main())
