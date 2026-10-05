"""FastAPI application entry: single Assistant gateway + Web UI.

Startup initialises the PostgreSQL schema — document metadata tables plus the
pgvector knowledge table (prompting for the password once in the terminal);
if the database is unavailable the gateway still serves chat with degraded
metadata while the document-management APIs report errors.

Run:
    容器内(compose 已映射到宿主 18000): uvicorn app.main:app --host 0.0.0.0 --port 8000
    宿主机直跑(开发拓扑见 README; 用宿主发布端口, 8000 常落 winnat 排除段): uvicorn app.main:app --port 18000
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.assistant.router import router as assistant_router
from app.docs.router import router as docs_router
from app.files.router import router as files_router
from app.memory.router import router as memory_router
from app.kg.router import router as kg_router
from app.config import get_settings

logger = logging.getLogger(__name__)


def _ensure_app_logging() -> None:
    """给应用日志补一个 root handler, 否则启动阶段的关键 INFO/WARNING 会丢。

    uvicorn 的默认 logging config 只配 `uvicorn.*` 三个 logger, 不动 root; `app.main` 的
    记录一路冒泡到无 handler 的 root, 由 logging.lastResort 按 WARNING 以上才输出 ——
    于是"PostgreSQL 初始化失败"之外的所有提示(包括依赖对接地址、rerank 就绪)在日志里
    一条看不到。降级是静默的, 日志再静默, 排查就只能靠猜。
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# Vue3 SPA 构建产物目录 (web-ui 执行 pnpm build 输出到 web/dist; 仅生产/打包需要)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DIST_DIR = WEB_DIR / "dist"
# 开发期前端由 vite dev server 提供(代理 /api 到本网关), 不依赖 web/dist。
VITE_DEV_URL = "http://localhost:5173"


def _log_dependency_endpoints() -> None:
    """启动时把各依赖的真实对接地址打出来。

    记忆层/检索层连不上时都是静默降级(内存 dict / RRF 融合序), 只看结果无法区分
    "功能坏了"与"配置指到了容器内服务名"。这一行日志就是对照现场配置的快照。
    只打地址, 不打密码/API Key(settings 中密码类字段已 repr=False)。
    """
    s = get_settings()
    logger.info(
        "依赖对接地址[宿主轨看 .env, 容器轨看 compose]: pg=%s:%s/%s es=%s redis=%s neo4j=%s "
        "mongo=%s tei=%s ollama=%s mineru=%s hr_mcp=%s finance_mcp=%s analytics_mcp=%s "
        "procurement_mcp=%s hr_agent=%s finance_agent=%s analyst_agent=%s "
        "contract_agent=%s public_base=%s report_dir=%s audit=%s",
        s.pg_host,
        s.pg_port,
        s.pg_database,
        s.es_url,
        s.redis_url,
        s.neo4j_uri,
        s.mongo_url,
        s.tei_rerank_url,
        s.ollama_base_url,
        s.mineru_base_url,
        s.hr_mcp_url,
        s.finance_mcp_url,
        s.analytics_mcp_url,
        s.procurement_mcp_url,
        s.hr_agent_url,
        s.finance_agent_url,
        s.analyst_agent_url,
        s.contract_agent_url,
        s.public_base_url or "(相对路径)",
        s.report_dir,
        s.audit_log_path,
    )


# 能力开关清单(启动摘要与告警的唯一来源): 名字取 settings 字段, 第二个元素是人在
# 日志里该看到的中文能力名。这些开关关掉后都只表现为"功能不存在"而不是报错
# (降级优先), 没拷 .env 时整站能力会静默少一大片, 仅看页面无法区分"坏了"与"没开"。
_CAPABILITY_FLAGS: tuple[tuple[str, str], ...] = (
    ("long_term_memory_enabled", "长期记忆(Vector 通道)"),
    ("graph_memory_enabled", "长期记忆(Graph 通道)"),
    ("personal_memory_enabled", "个人级六桶记忆"),
    ("doc_kg_enabled", "文档知识图谱"),
    ("cache_enabled", "三类缓存"),
    ("mongo_enabled", "正文外置存储(父块上下文)"),
    ("multi_agent_enabled", "多智能体并发委派"),
    ("checkpoint_enabled", "LangGraph Checkpointer"),
)


def _log_capability_summary() -> None:
    """启动时打一行能力开关摘要, 并对每个关闭的能力记 WARNING。

    能力开关在代码里取保守默认值(与 .env.example 的建议值不同), 因此
    "忘拷 .env" 的后果是静默降级: 父块上下文、长期记忆、文档图谱全部不存在,
    但对话仍然正常返回 —— 只看结果与日志都分不出"坏了"还是"没开"。这一行把现场
    展开成可对照的快照(与依赖地址快照同位同时打)。
    """
    s = get_settings()
    on: list[str] = []
    off: list[str] = []
    for field_name, label in _CAPABILITY_FLAGS:
        (on if getattr(s, field_name, False) else off).append(label)
    logger.info("能力开关摘要 on=[%s] off=[%s]", ", ".join(on) or "-", ", ".join(off) or "-")
    if off:
        logger.warning(
            "以下能力未开启(表现为功能缺失而不报错): %s —— 若这不是预期, 检查是否漏了 .env "
            "或 compose 侧对应键(容器轨看 docker/.env)",
            ", ".join(off),
        )


def _configure_thread_pools() -> None:
    """抬两个默认线程池的上限(不抬就是百人并发下最先生效的全局串行点)。

    为什么必要: 本项目把大量同步活卸载到线程 —— ``asyncio.to_thread`` 走事件循环的
    默认 executor(上限 ``min(32, cpu+4)``, 四核机器就是 8 个), Starlette 的同步接口与
    ``FileResponse`` 走 anyio limiter(默认 40)。docgen builder/文档解析/PIL/同步 DB 工具
    共抢前者, 下载路由共抢后者; 任一池满了, 后来者只能排队而事件循环依旧看似健康。

    上限来自 ``THREAD_POOL_TOKENS``, 与 PG 连接池/下游服务上限同量级取一个保守值;
    两个池都只在启动设一次(运行中改会影响已在跑的任务), 所以放在 lifespan 开头。
    """
    tokens = max(16, int(get_settings().thread_pool_tokens))
    loop = asyncio.get_running_loop()
    # asyncio.to_thread: 默认 executor 只能在第一次使用前替换(本函数在 lifespan 最开头
    # 跑, 此时默认池尚未被创建; 已创建的池不去 shutdown, 免得丢掉已在跑的卸载任务)
    if getattr(loop, "_default_executor", None) is None:
        loop.set_default_executor(
            ThreadPoolExecutor(max_workers=tokens, thread_name_prefix="mxi-blocking")
        )
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = tokens
    except Exception as exc:  # noqa: BLE001 - 抬不动也只是回到默认值, 不阻断启动
        logger.warning("anyio 线程池上限设置失败(保持默认 40): %s", exc)
    logger.info("线程池上限: asyncio/anyio 各 %d", tokens)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Init PostgreSQL schema at startup (getpass password prompt happens here)."""
    from app.tracing import init_langfuse, init_tracing

    _configure_thread_pools()
    _log_dependency_endpoints()
    _log_capability_summary()
    if init_tracing():
        logger.info("LangSmith tracing active for this gateway process")
    if init_langfuse():
        logger.info("Langfuse tracing active for this gateway process")
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
    # 正文外置存储(MongoDB)初始化: 集合与索引幂等创建。内部逐层降级,
    # 连不上时入库接口会报错, 已有检索的父块上下文降级为子块文本(不阻断对话)。
    try:
        from app.bodies.client import init_body_schema

        await init_body_schema()
        logger.info("MongoDB body store ready (doc_bodies / doc_body_parts / parent_texts)")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "MongoDB 初始化失败, 入库接口将报错, 已有检索的父块上下文将降级为子块文本: %s", exc
        )
    # 记忆层/缓存层初始化: Redis Checkpointer + Neo4j schema。内部已经逐层降级
    # (AsyncRedisSaver 连不上自动退回 InMemorySaver / Neo4j 连不上自动图记忆不可用),
    # 这里再兜底一层, 避免 setup() 本身抛出未预期异常时阻断启动。
    from app.assistant.graph import get_orchestrator
    from app.cache.redis_client import close_redis
    from app.memory.graph_store import close_driver
    from app.security.audit import get_audit_logger

    # 写线程在启动时就拉起来(而不是第一条审计到达时): 否则启动初期那些关键
    # 留痕会落在一个刚创建的线程上, 并且难区分"没日志"与"日志在内存里"。
    get_audit_logger()

    orchestrator = get_orchestrator()
    try:
        await orchestrator.setup()
        logger.info("Memory/cache layers ready (checkpoint + graph schema)")
    except Exception as exc:
        logger.error("记忆层/缓存层初始化失败, 将退回无持久化行为: %s", exc)
    # Rerank 后端(TEI 真 cross-encoder)就绪探测: 只打日志不改开关。
    # 不可用时检索仍按查询降级 RRF 融合序(秒级超时), 不能因此阻断启动。
    from app.rag.reranker import close_reranker_client, get_reranker

    try:
        if await get_reranker().probe():
            logger.info("Rerank 后端就绪 (TEI %s)", get_settings().tei_rerank_url)
        else:
            logger.warning(
                "TEI rerank 不可用(%s; 容器可能仍在加载权重), 检索会按查询降级 RRF 融合序",
                get_settings().tei_rerank_url,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rerank 探测异常(不阻断启动): %s", exc)
    # 文档知识图谱 schema 预热(幂等; 内部已逐层降级, store 查询时也会兜底 ensure)。
    if get_settings().doc_kg_enabled:
        try:
            from app.kg import store as kg_store

            await kg_store.ensure_schema()
            logger.info("Document knowledge graph schema ready (Neo4j :KgDoc/:KgEntity)")
        except Exception as exc:  # noqa: BLE001
            logger.error("文档知识图谱 schema 初始化失败, 图谱功能降级: %s", exc)
    yield
    from app.tools._http import close_web_client
    from app.tracing import shutdown_langfuse

    await orchestrator.shutdown()
    await close_reranker_client()
    await close_web_client()
    # 新增的几个进程级共享连接池必须在关停路径上放掉, 否则它们和 checkpointer
    # 同命运: 进程退出时留一堆未关闭连接(容器重入时表现为满端口 TIME_WAIT)。
    from app.assistant.a2a_client import close_a2a_pool
    from app.docs.parsers import close_mineru_client
    from app.rag.bm25 import close_es_client
    from app.rag.embeddings import close_embedder_client
    from app.security.audit import shutdown_audit

    await close_a2a_pool()
    await close_es_client()
    await close_embedder_client()
    await close_mineru_client()
    await close_redis()
    await close_driver()
    from app.bodies.client import close_mongo

    await close_mongo()
    # 审计落盘改成后台批量写了, 关停不刷就会把最后一批留痕丢在内存里(合规问题)。
    shutdown_audit()
    # Langfuse 上报是批量异步队列: 关停前 flush, 否则最后几轮对话的 trace 丢在内存里。
    shutdown_langfuse()


def create_app() -> FastAPI:
    """Build the gateway application."""
    _ensure_app_logging()
    app = FastAPI(title="MXI Enterprise Multi-Agent Assistant", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 文档生成物下载路由(计划 E3): 必须注册在 assistant_router 之后 —— 那里面有
    # /api/files/reports/{name}(图表/报告产物), 先到先得; 下载路由自身的 32 位 hex 令牌
    # 校验是双保险, 即使顺序被调整 "reports" 也永远匹配不进令牌位。
    app.include_router(assistant_router)
    app.include_router(files_router)
    app.include_router(docs_router)
    app.include_router(memory_router)
    app.include_router(kg_router)

    # --- Vue3 SPA 托管: 仅当存在构建产物(生产/打包)时才启用 ---
    # 开发期不跑 pnpm build, web/dist 可能不存在: 旧的写法无条件回退 index.html,
    # 会让 GET / 直接 500, 掩盖不住"该用 vite dev server"这个真实原因。
    index_file = DIST_DIR / "index.html"
    if index_file.is_file():
        assets_dir = DIST_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            # 已注册的 /api 路由优先级更高, 不会走到这里; 其余一律回退到 index.html
            return FileResponse(index_file)
    else:

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_dev(full_path: str) -> JSONResponse:
            # 无构建产物 = dev 模式: 页面由 vite 提供, 本进程只做 API 与代理目标
            return JSONResponse(
                {
                    "detail": "web/dist 无前端构建产物, 开发期请用 vite dev 模式",
                    "dev_server": VITE_DEV_URL,
                    "how_to": "cd web-ui && pnpm dev (代理目标读 VITE_API_TARGET, 默认本网关)",
                    "api_docs": "/docs",
                    "health": "/api/health",
                },
                status_code=200,
            )

    return app


app = create_app()
