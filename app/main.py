"""FastAPI application entry: single Assistant gateway + Web UI.

Startup initialises the PostgreSQL schema — document metadata tables plus the
pgvector knowledge table (prompting for the password once in the terminal);
if the database is unavailable the gateway still serves chat with degraded
metadata while the document-management APIs report errors.

Run:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.assistant.router import router as assistant_router
from app.docs.router import router as docs_router
from app.memory.router import router as memory_router
from app.kg.router import router as kg_router

logger = logging.getLogger(__name__)

# Vue3 SPA 构建产物目录 (web-ui 执行 pnpm build 输出到 web/dist)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DIST_DIR = WEB_DIR / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Init PostgreSQL schema at startup (getpass password prompt happens here)."""
    from app.tracing import init_tracing

    if init_tracing():
        logger.info("LangSmith tracing active for this gateway process")
    try:
        from app.db.session import init_schema

        await init_schema()
        logger.info(
            "PostgreSQL schema ready (metadata + pgvector + long_term_memories + user_profiles)"
        )
    except Exception as exc:
        logger.error(
            "PostgreSQL 初始化失败, 文档管理/知识库功能不可用, 聊天将降级为无标签模式: %s", exc
        )
    # 记忆层/缓存层初始化: Redis Checkpointer + Neo4j schema。内部已经逐层降级
    # (AsyncRedisSaver 连不上自动退回 InMemorySaver / Neo4j 连不上自动图记忆不可用),
    # 这里再兜底一层, 避免 setup() 本身抛出未预期异常时阻断启动。
    from app.assistant.graph import get_orchestrator
    from app.cache.redis_client import close_redis
    from app.memory.graph_store import close_driver

    orchestrator = get_orchestrator()
    try:
        await orchestrator.setup()
        logger.info("Memory/cache layers ready (checkpoint + graph schema)")
    except Exception as exc:
        logger.error("记忆层/缓存层初始化失败, 将退回无持久化行为: %s", exc)
    # 文档知识图谱 schema 预热(幂等; 内部已逐层降级, store 查询时也会兜底 ensure)。
    from app.config import get_settings

    if get_settings().doc_kg_enabled:
        try:
            from app.kg import store as kg_store

            await kg_store.ensure_schema()
            logger.info("Document knowledge graph schema ready (Neo4j :KgDoc/:KgEntity)")
        except Exception as exc:  # noqa: BLE001
            logger.error("文档知识图谱 schema 初始化失败, 图谱功能降级: %s", exc)
    yield
    await orchestrator.shutdown()
    await close_redis()
    await close_driver()


def create_app() -> FastAPI:
    """Build the gateway application."""
    app = FastAPI(title="MXI Enterprise Multi-Agent Assistant", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(assistant_router)
    app.include_router(docs_router)
    app.include_router(memory_router)
    app.include_router(kg_router)

    # --- Vue3 SPA (web/dist) 托管: 静态资源 + history 模式回退 ---
    index_file = DIST_DIR / "index.html"
    if (DIST_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        # 已注册的 /api 路由优先级更高, 不会走到这里; 其余一律回退到 index.html
        return FileResponse(index_file)

    return app


app = create_app()
