"""PostgreSQL async engine / session factory.

单一存储层: 业务/文档元数据与 pgvector 知识块共用这一套连接, MCP server 的
同步访问在 app/db/sync.py (psycopg3), 二者读同一份 Settings。

Password policy: the database password is injected via the PG_PASSWORD
environment variable (or the .env file picked up by pydantic-settings).
It is never hard-coded and .env files are git-ignored.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _password() -> str:
    """Read the database password from the environment (PG_PASSWORD)."""
    password = get_settings().pg_password
    if not password:
        raise RuntimeError(
            "缺少 PostgreSQL 密码，请设置环境变量 PG_PASSWORD"
        )
    return password


def async_database_url() -> str:
    """Resolve the asyncpg DSN from DATABASE_URL or the discrete PG_* fields."""
    settings = get_settings()
    if settings.database_url:
        return settings.database_url
    # 密码可能含 @ / : 等 DSN 保留字符, 必须转义后再拼接。
    return (
        f"postgresql+asyncpg://{settings.pg_user}:{quote_plus(_password())}"
        f"@{settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
    )


def _connect_args() -> dict:
    """Connect args for asyncpg, mapping ``PG_SSLMODE`` straight through.

    asyncpg 的 ``ssl`` 参数接受 libpq 风格的模式名(disable/allow/prefer/require/
    verify-ca/verify-full): disable 会归一为明文, require 及以上由 asyncpg 自己
    建 SSLContext(verify-full 才做主机名校验)。
    必须显式传: 不传时 asyncpg 默认 ``prefer``, 那会让 PG_SSLMODE=disable 形同失效
    (先试 SSL 再回退明文, 报出来的是 SSL 协商栈而不是真正的连接问题)。
    """
    settings = get_settings()
    return {
        "timeout": settings.pg_connect_timeout,
        "server_settings": {"application_name": "mxi-assistant"},
        "ssl": settings.pg_sslmode,
    }


def get_engine() -> AsyncEngine:
    """Lazily create the async engine (reads PG_PASSWORD on first use).

    连接池容量必须显式给: SQLAlchemy 默认 ``pool_size=5 / max_overflow=10``,
    在一个共用进程的网关上撑不住几十轮并发对话(每轮的检索回表/ACL 门禁/元数据/
    会话落库都抢这条池), 抢不到连接时默认等 30s 再抛 ``TimeoutError``, 而
    代码里它会被当作"DB 抖动"吞掉 -> 表现为知识库答不对/会话不记录的静默降级。
    容量与 PG ``max_connections`` 的对账口径见 ``Settings.pg_pool_size`` 注释。
    """
    global _engine, _session_factory
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            async_database_url(),
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=max(1, s.pg_pool_size),
            max_overflow=max(0, s.pg_max_overflow),
            pool_timeout=max(1, s.pg_pool_timeout),
            connect_args=_connect_args(),
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory (creates the engine on first call)."""
    get_engine()
    assert _session_factory is not None
    return _session_factory


# 已存在表的新增列轻量迁移 (create_all 不会给已存在的表加列):
# 表名 -> {列名: DDL}。新表由 create_all 直接建出, 只有"老库升级"才需要补列。
_BACKFILL_COLUMNS: dict[str, dict[str, str]] = {
    "documents": {
        "visibility": "VARCHAR(16) NOT NULL DEFAULT 'public'",
        "owner_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        "dept_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        "allowed_roles": "VARCHAR(128) NOT NULL DEFAULT ''",
        # 正文外置: 发布态门禁 + 正文已落 Mongo 标记(老库升级补列)。
        "status": "VARCHAR(16) NOT NULL DEFAULT 'ready'",
        "body_stored": "BOOLEAN NOT NULL DEFAULT false",
    },
    # 发布事务不再物理删旧块, 改成标记 + 后台 GC: create_all 不会给已存在的表加列,
    # 存量父子块表必须在这里补, 否则所有检索 SQL 直接报 column does not exist。
    "doc_chunks": {"deprecated_at": "TIMESTAMPTZ"},
    "doc_parents": {"deprecated_at": "TIMESTAMPTZ"},
    "long_term_memories": {
        "title": "VARCHAR(128) NOT NULL DEFAULT ''",
        "source": "VARCHAR(32) NOT NULL DEFAULT 'turn'",
        "occurred_at": "TIMESTAMPTZ",
    },
    "user_profiles": {
        # 波动类属性的观测序列(当前值按生效时间派生): 可空 —— 老行没有历史,
        # 首次合并时从 attributes 的旧字符串自愈出序列。
        "attribute_history": "JSON",
    },
    "chat_messages": {
        # docgen 创作产物 [{name,url,title}]: 可空 —— 历史轮次本来就没有产物。
        "artifacts": "JSON",
    },
    # 智能问数 V1→V3 对齐新增列(存量 pg_data 卷上表已存在, create_all 不会补列):
    # 缺 semver 就等于“badcase 无法回溯当时口径”, 缺 slots_json 就算不了 slot-F1。
    "sem_metrics": {"owner": "VARCHAR(64) NOT NULL DEFAULT ''"},
    "sem_dimensions": {
        "hierarchy": "JSON",
        "version": "INTEGER NOT NULL DEFAULT 1",
    },
    "askdata_query_log": {
        "semver": "VARCHAR(32) NOT NULL DEFAULT ''",
        "catalog_origin": "VARCHAR(16) NOT NULL DEFAULT ''",
        "llm_calls": "INTEGER NOT NULL DEFAULT 0",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "cache_hit": "BOOLEAN NOT NULL DEFAULT false",
        "metric_summary": "JSON",
    },
    "askdata_golden": {
        "slots_json": "JSON",
        "difficulty": "VARCHAR(16) NOT NULL DEFAULT 'easy'",
        "intent": "VARCHAR(16) NOT NULL DEFAULT ''",
        "semver": "VARCHAR(32) NOT NULL DEFAULT ''",
        # 默认不可缓存: 语义缓存只服人工批准的高频问法(否则 agent 评测会直接拿到标准答案)。
        "cacheable": "BOOLEAN NOT NULL DEFAULT false",
        # 语义缓存的基底: 存量卷上补向量列(默认 NULL = 这一条不参与语义缓存,
        # 跑 scripts/embed_askdata_golden 回填后才生效; 不回填也不会报错)。
        "embedding": f"vector({get_settings().embedding_dim})",
    },
    "askdata_eval_run": {
        "dsl_exact_match": "NUMERIC(5,2) NOT NULL DEFAULT 0",
        "slot_f1": "NUMERIC(5,2) NOT NULL DEFAULT 0",
        "mis_clarify_rate": "NUMERIC(5,2) NOT NULL DEFAULT 0",
        "fallback_rate": "NUMERIC(5,2) NOT NULL DEFAULT 0",
        "gate_status": "VARCHAR(16) NOT NULL DEFAULT ''",
        "gate_note": "VARCHAR(256) NOT NULL DEFAULT ''",
        "baseline_run_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        "semver": "VARCHAR(32) NOT NULL DEFAULT ''",
    },
}

# 已存在表上要加宽的 varchar 列: ``(表, 列, 目标长度)``。
# 为什么单独开一类: ``create_all`` 不 ALTER 已存在表, 而"模型里的宽度"与"存量库里的宽度"
# 会因历史版本而不一致 —— 实打实碰过: sem_change_log.object_id 早先登记的是 16,
# 而同步脚本往里写的是 `m_expense_total|d_expense_employee_name` 这种复合主键,
# 结果是整个同步事务在写版本日志时报 value too long(前面改的定义行全部回滚,
# 表现为"YAML 改了、库里没变、semver 也没动"。
# 拿 information_schema 先比长度再决定是否 ALTER: 不拿 ALTER 当空转(大表改宽度会重写)。
_BACKFILL_WIDEN = (
    ("sem_change_log", "object_id", 96),
    ("sem_change_log", "object_kind", 32),
)

# 已存在表的新增索引同理: create_all 跳过已存在的表, 表上的新索引也不会建。
_BACKFILL_INDEXES = (
    # 陈旧块的 GC 扫描索引(部分索引: 不进热路径, 只装已标陈旧的行)。
    "CREATE INDEX IF NOT EXISTS ix_doc_chunks_deprecated "
    "ON doc_chunks (deprecated_at) WHERE deprecated_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_doc_parents_deprecated "
    "ON doc_parents (deprecated_at) WHERE deprecated_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_long_term_memories_user_kind_access "
    "ON long_term_memories (user_id, kind, last_accessed_at)",
    # 存量表补列后需要自己补索引(下面两条服务于本次新增列):
    "CREATE INDEX IF NOT EXISTS ix_askdata_query_log_semver "
    "ON askdata_query_log (semver)",
    "CREATE INDEX IF NOT EXISTS ix_askdata_golden_difficulty "
    "ON askdata_golden (difficulty)",
    "CREATE INDEX IF NOT EXISTS ix_askdata_golden_intent "
    "ON askdata_golden (intent)",
    # 语义缓存每轮都要按这个列筛一遍(不建索引就是每轮全表扫)。
    "CREATE INDEX IF NOT EXISTS ix_askdata_golden_cacheable "
    "ON askdata_golden (cacheable) WHERE cacheable",
    # 向量列补上后还要自己补 HNSW 索引(create_all 对已存在的表不会动索引):
    # 没索引时 similar_intent 会退成全表扫, 几百行不致事但语义缓存应当与 sem_terms 同一形状。
    "CREATE INDEX IF NOT EXISTS ix_askdata_golden_embedding_hnsw "
    "ON askdata_golden USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)",
)

# ---------------------------------------------------------------------------
# 层 1 作用域列: 给已存在的业务表补 tenant_id / dept_id / 软删三件套。
# 默认值给 ``''``(未归属)而不是真实租户号: RLS 下任何会话都匹配不上空串, 回填前这些
# 行对分析与写通道都不可见 —— 宁可"暂时看不见", 也不要"暂时谁都能看见"。
# 回填(按部门名算 dept_id)见 app/db/scope.py::ensure_data_scope_backfill。
# ---------------------------------------------------------------------------
_SCOPE_TABLES = (
    "hr_employees",
    "hr_tickets",
    "hr_leave_records",
    "fin_reimbursements",
    "fin_department_budgets",
    "proc_suppliers",
    "proc_orders",
    "proc_contracts",
)
# 只有"单据/台账"类表参与软删; hr_employees/预算/供应商在写通道黑名单里, 不须软删列。
_SOFT_DELETE_TABLES = (
    "hr_tickets",
    "hr_leave_records",
    "fin_reimbursements",
    "proc_orders",
    "proc_contracts",
)
_SCOPE_COLUMNS = {
    "tenant_id": "VARCHAR(32) NOT NULL DEFAULT ''",
    "dept_id": "VARCHAR(32) NOT NULL DEFAULT ''",
}
_SOFT_DELETE_COLUMNS = {
    "is_deleted": "BOOLEAN NOT NULL DEFAULT false",
    "deleted_at": "TIMESTAMPTZ",
    "deleted_by": "VARCHAR(32) NOT NULL DEFAULT ''",
}
for _table in _SCOPE_TABLES:
    _BACKFILL_COLUMNS.setdefault(_table, {}).update(_SCOPE_COLUMNS)
for _table in _SOFT_DELETE_TABLES:
    _BACKFILL_COLUMNS.setdefault(_table, {}).update(_SOFT_DELETE_COLUMNS)
# RLS 谓词与域作域回查都是 (tenant_id, dept_id) 组合条件, 没这个复合索引时
# 每条分析查询都会退化成"全表扫 + 逐行过策略"。
_BACKFILL_INDEXES += tuple(
    f"CREATE INDEX IF NOT EXISTS ix_{_table}_tenant_dept ON {_table} (tenant_id, dept_id)"
    for _table in _SCOPE_TABLES
)


def _table_columns(sync_conn, table: str) -> set[str]:
    """Existing column names of ``table`` via the dialect inspector (no raw SQL).

    表不存在时必须返空集而不是让 inspector 去查: 首次建库时表还不存在,
    直接 get_columns 会让 SQLAlchemy 吐一条 "... does not exist" 的 WARNING,
    紧接着 create_all 就把表建出来了 —— 那条告警纯误导。
    """
    insp = inspect(sync_conn)
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


async def init_schema() -> None:
    """Create all metadata tables if they do not exist yet.

    pgvector 扩展必须先行: 任何 ``Vector`` 列(``doc_chunks.embedding`` 与遗留
    ``knowledge_chunks.embedding``)编译成 DDL 时需要 ``vector`` 类型已在 search_path
    中。容器由 docker/init/01_vector.sql 以超管预建, 这里再兜底一次 —— 失败时抛明确
    错误, 而不是让 create_all 报难懂的 "type vector does not exist"。

    Also backfills columns/indexes on pre-existing tables (``create_all`` never
    ALTERs existing tables, 见 ``_BACKFILL_COLUMNS`` / ``_BACKFILL_INDEXES``), so
    upgrading an old database stays safe and idempotent.

    最后一道是层 1 的数据库隔离: 先按部门名回填作用域列, 再建两个最小权限角色与
    RLS 策略(全部幂等)。为什么不只放在 docker/init/*.sql: 那个目录只在**空卷首次
    初始化**时执行, 已有 pg_data 卷的存量库永远不会跑它 —— 那就等于"只在文档里
    存在的隔离"。幂等地排在 create_all 之后是新库与老库收敛到同一结论的唯一方式。
    """
    from app.db.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:
            raise RuntimeError(
                "知识库需要 pgvector 扩展 (CREATE EXTENSION vector), 当前账号无权限创建: "
                f"{exc}; 请让超级用户执行一次 CREATE EXTENSION IF NOT EXISTS vector"
            ) from exc
        await conn.run_sync(Base.metadata.create_all)
        for table, columns in _BACKFILL_COLUMNS.items():
            have = await conn.run_sync(_table_columns, table)
            for name, ddl in columns.items():
                if name not in have:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        for table, column, target in _BACKFILL_WIDEN:
            # `await conn.execute(...).first()` 会被解析成 `await (coroutine.first())`:
            # 先把结果拿到手再取 first(), 否则就是 'coroutine' object has no attribute 'first'。
            found = await conn.execute(
                text("SELECT character_maximum_length FROM information_schema.columns "
                     "WHERE table_name = :table AND column_name = :column"),
                {"table": table, "column": column},
            )
            current = found.first()
            if current is not None and (current[0] is None or int(current[0]) >= target):
                continue
            await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR({target})"))
        for ddl in _BACKFILL_INDEXES:
            await conn.execute(text(ddl))

    # 作用域回填与策略/角色建立(内部各自幂等; rls_enabled=false 时只回填不碰策略)。
    from app.db.rls import (
        ensure_audit_immutability,
        ensure_db_principals,
        ensure_rls_policies,
    )
    from app.db.scope import ensure_data_scope_backfill

    await ensure_data_scope_backfill()
    if get_settings().rls_enabled:
        await ensure_db_principals()
        await ensure_rls_policies()
    # 审计不可变性不跟着 RLS 一起关: 它是层 6 的地基, 与隔离开关无关。
    await ensure_audit_immutability()

    # 语义层灌入(只在 sem_metrics 为空时写内置 Top 指标集, 已有则完全不动):
    # 为什么同样不能只放 docker/init/*.sql: 那个目录只在空卷首建时跑, 存量库永不重放。
    # 语义层空 = AskData_Agent 一个口径都认不出来(而不是"降级成自由 SQL"),
    # 所以它必须与建表同一条收敛路径; 灌入失败只记日志不阻断启动(问数不可用但对话可用)。
    if get_settings().askdata_enabled:
        from app.semantic.registry import ensure_seeded

        try:
            counts = await ensure_seeded()
            if counts.get("metrics"):
                logger.info("[askdata] 语义层首次灌入: %s", counts)
        except Exception as exc:  # noqa: BLE001 - 灌入失败不能拖死整个网关
            logger.error("[askdata] 语义层灌入失败, 智能问数将退回内置口径: %s", exc)


def db_available() -> bool:
    """Whether the engine has been initialised (password already provided)."""
    return _engine is not None
