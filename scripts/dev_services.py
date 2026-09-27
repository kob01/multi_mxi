"""开发/验证期的 docker 依赖服务管理 + 对接自检。

用法::

    uv run python -m scripts.dev_services up [--build] [--with-assistant]
    uv run python -m scripts.dev_services check [--strict]
    uv run python -m scripts.dev_services down

为什么需要这个脚本: 本项目除了 Ollama 以外的依赖在 compose 里都有对应服务
(postgres / elasticsearch / redis / neo4j / mongo / tei-rerank / mineru / hr-mcp /
finance-mcp / hr-agent / finance-agent), 宿主只跑网关与 vite dev。麻烦之处在于这些层
连不上时**全是静默降级**: Redis 退回内存 dict、checkpoint 退回 InMemorySaver、TEI 超时
退回 RRF 融合序、Neo4j 关图记忆、Mongo 父块退回子块文本。看功能表现分不清"代码坏了"和
"配置指到了容器内服务名"。本脚本把每个降级点变成显式的一行结论, 并顺带拦住两类配置事故:

1. 宿主轨混入容器地址(旧版 app/config.py 同时加载 docker/.env 造成的串味, 见 CONFIG_RULES);
2. 真实密钥被写进 .env / docker/.env(应当只放 docker/secrets/<name>.txt)。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from app.config import get_settings
from app.db.session import async_database_url, get_engine

BASE_DIR = Path(__file__).resolve().parent.parent
COMPOSE_FILE = "docker/docker-compose.yml"

# dev 期由 docker 提供的服务; assistant 默认不含 —— 它是宿主热重载改造的对象,
# 一起起会抢宿主发布端口(18000)并让"改代码不生效"这种错觉长期存在。
DEV_SERVICES = [
    "postgres",
    "elasticsearch",
    "redis",
    "neo4j",
    "mongo",
    "tei-rerank",
    "mineru",
    "hr-mcp",
    "finance-mcp",
    "hr-agent",
    "finance-agent",
]

# 允许出现在 .env / docker/.env 里的密钥字段: 出现非空值即视为写错了位置。
SECRET_KEYS = (
    "DEEPSEEK_API_KEY",
    "LANGSMITH_API_KEY",
    "PG_PASSWORD",
    "MONGO_PASSWORD",
    "NEO4J_PASSWORD",
)

# compose 网络内的服务名与容器专用主机名: 宿主轨配置里出现即说明串味了。
CONTAINER_HOSTS = (
    "postgres",
    "elasticsearch",
    "redis",
    "neo4j",
    "mongo",
    "tei-rerank",
    "mineru",
    "hr-mcp",
    "finance-mcp",
    "hr-agent",
    "finance-agent",
    "host.docker.internal",
)

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

# 连不上不报错、只静默降级的层: 自检把它们单独提示出来, 避免"看起来一切正常"。
SILENT_DEGRADE_SERVICES = ("redis", "neo4j", "tei-rerank", "mongo", "elasticsearch", "mineru")


class Check:
    """一条检查: 目标地址 + 探测协程 + 失败时的修复提示。"""

    def __init__(
        self,
        name: str,
        target: str,
        probe: Callable[[], Awaitable[tuple[str, str]]] | None = None,
        *,
        required: bool = True,
        fix: str = "",
    ) -> None:
        self.name = name
        self.target = target
        self.probe = probe
        self.required = required
        self.fix = fix
        self.status = SKIP
        self.detail = ""

    async def run(self) -> "Check":
        if self.probe is None:
            return self
        try:
            self.status, self.detail = await self.probe()
        except Exception as exc:  # noqa: BLE001 - 自检永不因单条异常中断
            self.status = FAIL
            self.detail = f"{type(exc).__name__}: {exc}"[:160]
        return self


def _compose(*args: str, build: bool = False, profile: bool = True) -> list[str]:
    cmd = ["docker", "compose", "-f", COMPOSE_FILE]
    if profile:
        cmd += ["--profile", "mineru"]
    cmd += list(args)
    if build:
        cmd += ["--build"]
    return cmd


def _run_compose(*args: str, build: bool = False, profile: bool = True) -> int:
    cmd = _compose(*args, build=build, profile=profile)
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=BASE_DIR)


def _read_env_pairs(rel: str) -> dict[str, str]:
    """解析 dotenv 文件(仅取 key=value, 忽略注释与行内注释)。"""
    path = BASE_DIR / rel
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------- 各服务探测实现


async def _probe_http(url: str, *, expect_json: bool = False) -> tuple[str, str]:
    """GET 一个 HTTP 端点: 2xx=OK, 其他状态=WARN, 连不上=FAIL。

    超时给得比应用路径宽: 十几条探测并发跑在同一事件循环上, 重探测(如 PG 建连接池)
    会短暂阻塞循环, 按 2s 连超时会出现"服务其实活着但自检报红"的假阴性。
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        resp = await client.get(url)
    note = f"HTTP {resp.status_code}"
    if resp.is_success and expect_json:
        payload = resp.json()
        if isinstance(payload, dict):
            keys = ",".join(sorted(payload)[:4])
            if keys:
                note = f"HTTP {resp.status_code} ({keys})"
    return (OK if resp.is_success else WARN), note


async def _probe_postgres() -> tuple[str, str]:
    from sqlalchemy import text

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT version()"))).scalar_one()
            database = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            ext = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
            ).scalar()
    finally:
        await engine.dispose()
    # pgvector 扩展缺失时整条向量检索通道不可用, 必须和"能连上"一起验
    if not ext:
        return FAIL, f"{database} 可连, 但缺 vector 扩展 -> 跑 scripts.init_db 或 compose 的 init/01_vector.sql"
    return OK, f"{database} 可连, pgvector {ext} ({version.split(',')[0]})"


async def _probe_elasticsearch() -> tuple[str, str]:
    s = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        health = (await client.get(f"{s.es_url}/_cluster/health")).json()
        count = await client.get(f"{s.es_url}/{s.es_index}/_count")
    status = health.get("status", "?")
    if status not in ("green", "yellow"):
        return FAIL, f"集群 status={status}"
    if count.is_success:
        return OK, f"status={status}, 索引 {s.es_index} 共 {count.json().get('count')} 条"
    return WARN, f"status={status}, 索引 {s.es_index} 缺失(首次入库/重建后自动创建)"


async def _probe_redis() -> tuple[str, str]:
    from redis.asyncio import Redis

    s = get_settings()
    # decode_responses 必须开: 不开时 module_list() 返回的 dict 键是 bytes,
    # 按 'name' 取会拿到 None, 看起来像"一个模块都没加载"。
    r = Redis.from_url(s.redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=8)
    try:
        await r.ping()
        modules = {str(m.get("name", "")) for m in await r.module_list()}
        indexes = await r.execute_command("FT._LIST")
    finally:
        await r.aclose()
    # langgraph-checkpoint-redis 依赖 RedisJSON + RediSearch; 裸 redis 镜像会在
    # checkpointer setup() 时报 "unknown command JSON.SET", 会话记忆静默退回内存。
    lowered = {m.lower() for m in modules}
    missing = [label for label, pat in (("RedisJSON", "rejson"), ("RediSearch", "search")) if not any(pat in m for m in lowered)]
    if missing:
        return FAIL, f"PING 通但缺模块 {','.join(missing)} -> compose 必须用 redis-stack-server 镜像"
    note = f"PING 通, RedisJSON/RediSearch 就位({len(modules)} 个模块)"
    if indexes:
        note += f", 已有索引 {','.join(str(i) for i in indexes)}"
    return OK, note


async def _probe_neo4j() -> tuple[str, str]:
    from app.memory.graph_store import get_driver

    driver = get_driver()
    if driver is None:
        return WARN, "graph_memory_enabled 与 doc_kg_enabled 均关闭, 未建驱动"
    try:
        async with driver.session() as session:
            res = await session.run("RETURN 1 AS ok")
            record = await res.single()
        return (OK if record and record["ok"] == 1 else FAIL), "Cypher RETURN 1 通过"
    finally:
        await driver.close()


async def _probe_mongo() -> tuple[str, str]:
    from app.bodies.client import mongo_database_url, ping

    if not get_settings().mongo_enabled:
        return WARN, "mongo_enabled=false, 父块上下文将降级为子块文本"
    ok = await ping()
    url = mongo_database_url()
    return (OK if ok else FAIL), "ping 通过" if ok else "连不上 -> 入库接口会直接报错"


async def _probe_tei() -> tuple[str, str]:
    s = get_settings()
    # TEI /health 只有权重加载完才 2xx; 容器刚起来的几十秒内是 503 -> WARN 而非 FAIL
    code, note = await _probe_http(f"{s.tei_rerank_url}/health")
    if code == OK:
        return OK, "模型已就绪, 重排可用"
    if code == WARN:
        return WARN, f"{note} -> 权重仍在加载或模型不对, 检索会降级 RRF 融合序"
    return FAIL, note


async def _probe_ollama() -> tuple[str, str]:
    s = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        resp = await client.get(f"{s.ollama_base_url}/api/tags")
    if not resp.is_success:
        return FAIL, f"HTTP {resp.status_code}"
    models = {m.get("name", "") for m in resp.json().get("models", [])}
    want = s.embedding_model
    if not any(want in m for m in models):
        return FAIL, f"服务在, 但缺模型 {want} -> ollama pull {want}"
    return OK, f"{want} 已就绪"


async def _probe_mcp(name: str, url: str) -> tuple[str, str]:
    """FastMCP 的 streamable-http 端点: GET 通常 405/406, 能应答即说明服务活着。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return FAIL, f"{type(exc).__name__} -> 服务未起或宿主端口不对"
    return OK, f"HTTP {resp.status_code}(MCP 端点已应答)"


async def _probe_agent(name: str, base_url: str) -> tuple[str, str]:
    """A2A: 拉 Agent Card, 顺带暴露卡片里的通告地址(与配置地址不一致是被覆盖的那一个)。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/.well-known/agent-card.json")
    if not resp.is_success:
        return FAIL, f"HTTP {resp.status_code}"
    card = resp.json()
    advertised = card.get("url", "?")
    note = f"卡片 {card.get('name', name)} 通告 {advertised}"
    if advertised.rstrip("/") != base_url.rstrip("/"):
        note += " (与配置地址不同, 客户端按配置覆盖 —— 属预期)"
    return OK, note


async def _probe_gateway() -> tuple[str, str]:
    s = get_settings()
    # 健康端点在 assistant_router 的 /api 前缀下: GET /health 会落到 SPA catch-all
    # 并返 200 + index.html, 看起来"服务正常"其实探的是静态页。
    return await _probe_http(f"http://127.0.0.1:{s.assistant_port}/api/health", expect_json=True)


# ---------------------------------------------------------------- 配置轨与密钥检查


def _host_of(value: str) -> str:
    """取出地址里的主机部分: 去掉 scheme、认证段与端口/路径。

    必须按主机名比较而不是子串: ``mongodb://localhost:27017`` 里含 "mongo",
    ``redis://localhost:6379/0`` 里含 "redis", 子串匹配会把健康的宿主配置误判成串味。
    """
    rest = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", (value or "").strip())
    rest = rest.rsplit("@", 1)[-1]  # 去掉 user:pw@ 认证段
    return re.split(r"[/:]", rest, maxsplit=1)[0].lower()


def _config_track_checks() -> list[Check]:
    """宿主轨自检: 地址不该是容器服务名, 密钥不该写在 dotenv 文件里。"""
    s = get_settings()
    checks: list[Check] = []

    urls = {
        "ES_URL": s.es_url,
        "MONGO_URL": s.mongo_url,
        "REDIS_URL": s.redis_url,
        "NEO4J_URI": s.neo4j_uri,
        "TEI_RERANK_URL": s.tei_rerank_url,
        "HR_MCP_URL": s.hr_mcp_url,
        "FINANCE_MCP_URL": s.finance_mcp_url,
        "HR_AGENT_URL": s.hr_agent_url,
        "FINANCE_AGENT_URL": s.finance_agent_url,
        "OLLAMA_BASE_URL": s.ollama_base_url,
        "MINERU_BASE_URL": s.mineru_base_url,
        "PG_HOST": s.pg_host,
    }
    leaked = {k: v for k, v in urls.items() if _host_of(v) in CONTAINER_HOSTS}
    checks.append(
        Check(
            "配置轨: 宿主未混入容器地址",
            "config.py env_file=(.env, .env.local)",
            probe=None,
        )
    )
    track = checks[0]
    if leaked:
        track.status = FAIL
        track.detail = (
            f"读到容器内主机名 {','.join(f'{k}={_host_of(v)}' for k, v in sorted(leaked.items()))}"
            f" -> 检查 app/config.py 是否又加载了 docker/.env, 或宿主 .env 里被填了服务名"
            f" (见 CONFIG_RULES 第 5 条)"
        )
    else:
        track.status = OK
        track.detail = f"全部为宿主可达地址 (pg={s.pg_host}, es={s.es_url})"

    # 密钥泄漏: dotenv 文件里出现非空密钥值即写错了位置
    bad: list[str] = []
    embedded_dsn: list[str] = []
    for rel in (".env", "docker/.env", ".env.local"):
        pairs = _read_env_pairs(rel)
        for key in SECRET_KEYS:
            if pairs.get(key):
                bad.append(f"{rel}:{key}")
    dsn = s.database_url
    if dsn and re.search(r"://[^:/@]+:[^@]+@", dsn):
        embedded_dsn.append("DATABASE_URL 内嵌口令")
    scan = Check(
        "密钥: 未写进 dotenv 文件",
        " / ".join(p for p in (".env", "docker/.env") if (BASE_DIR / p).is_file()) or "(无 dotenv)",
        probe=None,
        fix="把值移到 docker/secrets/<name>.txt (见该目录 README.md)",
    )
    if bad or embedded_dsn:
        scan.status = FAIL
        scan.detail = "发现明文密钥 " + ", ".join(bad + embedded_dsn) + f" -> {scan.fix}"
    else:
        sources = [
            f"{name}"
            for name in ("pg_password", "deepseek_api_key")
            if (BASE_DIR / "docker" / "secrets" / f"{name}.txt").is_file()
        ]
        scan.status = OK
        scan.detail = "dotenv 干净" + (f"; secret 文件就位: {', '.join(sources)}" if sources else "; 无 docker/secrets/*.txt")

    # 口令是否真的拿得到(来源只报有无, 绝不打印值)
    pw = Check("凭据可用性: PG 口令", async_database_url().split("@")[-1], probe=None)
    pw.status = OK if s.pg_password else FAIL
    pw.detail = "已取到(来源: 环境变量或 docker/secrets/pg_password.txt)" if s.pg_password else "取不到 -> PG 连接与各 mcp/agent 服务都会失败"
    pw.fix = "创建 docker/secrets/pg_password.txt, 或 export PG_PASSWORD"
    checks += [scan, pw]

    tracing = Check(
        "合规: LangSmith tracing",
        f"LANGSMITH_TRACING={s.langsmith_tracing}",
        probe=None,
    )
    if str(s.langsmith_tracing).lower() in ("true", "1", "yes", "on"):
        tracing.status = WARN
        tracing.detail = "开启中: 对话内容会上传到 " + s.langsmith_endpoint + " (容器侧必须 false)"
    else:
        tracing.status = OK
        tracing.detail = "关闭: 对话 trace 不出本机"
    checks.append(tracing)
    return checks


async def _probe_mineru(base_url: str) -> tuple[str, str]:
    """mineru-api 是 FastAPI 服务: 根路径无路由(404), 用 openapi 描述文件判活。"""
    return await _probe_http(f"{base_url.rstrip('/')}/openapi.json")


async def _build_checks(with_gateway: bool) -> list[Check]:
    s = get_settings()
    checks = _config_track_checks()
    checks += [
        Check("postgres", f"{s.pg_host}:{s.pg_port}/{s.pg_database}", _probe_postgres, fix="docker compose -f docker/docker-compose.yml up -d postgres"),
        Check("elasticsearch", s.es_url, _probe_elasticsearch, fix="docker compose -f docker/docker-compose.yml up -d elasticsearch"),
        Check("redis", s.redis_url, _probe_redis, fix="docker compose -f docker/docker-compose.yml up -d redis"),
        Check("neo4j", s.neo4j_uri, _probe_neo4j, fix="docker compose -f docker/docker-compose.yml up -d neo4j"),
        Check("mongo", s.mongo_url, _probe_mongo, fix="docker compose -f docker/docker-compose.yml up -d mongo"),
        Check("tei-rerank", s.tei_rerank_url, _probe_tei, fix="docker compose -f docker/docker-compose.yml up -d tei-rerank (权重需预下载到 data/tei_models)"),
        Check("ollama(宿主)", s.ollama_base_url, _probe_ollama, fix="启动宿主机 Ollama 并 ollama pull bge-m3"),
        Check("mineru", s.mineru_base_url, lambda: _probe_mineru(s.mineru_base_url), required=False, fix="docker compose -f docker/docker-compose.yml --profile mineru up -d mineru"),
        Check("hr-mcp", s.hr_mcp_url, lambda: _probe_mcp("hr-mcp", s.hr_mcp_url), fix="docker compose -f docker/docker-compose.yml up -d hr-mcp"),
        Check("finance-mcp", s.finance_mcp_url, lambda: _probe_mcp("finance-mcp", s.finance_mcp_url), fix="docker compose -f docker/docker-compose.yml up -d finance-mcp"),
        Check("hr-agent", s.hr_agent_url, lambda: _probe_agent("hr-agent", s.hr_agent_url), fix="docker compose -f docker/docker-compose.yml up -d hr-agent"),
        Check("finance-agent", s.finance_agent_url, lambda: _probe_agent("finance-agent", s.finance_agent_url), fix="docker compose -f docker/docker-compose.yml up -d finance-agent"),
    ]
    if with_gateway:
        checks.append(
            Check(
                "assistant(宿主)",
                f"http://127.0.0.1:{s.assistant_port}/api/health",
                _probe_gateway,
                required=False,
                fix="uv run uvicorn app.main:app --host 0.0.0.0 --port " + str(s.assistant_port) + " --reload (或 ./scripts/dev.ps1)",
            )
        )
    # 单条探测失败不影响其他条(每条自带异常掉到 FAIL), 并发跑省掉十几秒
    await asyncio.gather(*(c.run() for c in checks))
    return checks


def _print_report(checks: list[Check]) -> tuple[int, int, int]:
    width = max(len(c.name) for c in checks) + 2
    tw = 40
    print()
    print(f"{'服务'.ljust(width)}{'目标'.ljust(tw)}结论  说明")
    print("-" * 110)
    ok = warn = fail = 0
    for c in checks:
        if c.status == OK:
            ok += 1
        elif c.status == WARN:
            warn += 1
        elif c.status == FAIL:
            fail += 1
        target = c.target if len(c.target) <= tw else c.target[: tw - 1] + "…"
        detail = c.detail or (c.fix if c.status == FAIL else "")
        if c.status == FAIL and c.fix and c.fix not in detail:
            detail = f"{detail} | 修复: {c.fix}"
        print(f"{c.name.ljust(width)}{target.ljust(tw)}{c.status:<5} {detail}")
    print("-" * 110)
    return ok, warn, fail


async def _action_check(strict: bool, gateway: bool) -> int:
    checks = await _build_checks(gateway)
    ok, warn, fail = _print_report(checks)
    required_fail = [c for c in checks if c.status == FAIL and c.required]
    print(
        f"合计: OK={ok} WARN={warn} FAIL={fail} (其中必需项 FAIL={len(required_fail)})"
    )
    silent = [c.name for c in checks if c.status != OK and c.name in SILENT_DEGRADE_SERVICES]
    if silent:
        print("\n提示: " + ", ".join(silent) + " 不可用时不会报错, 只会静默降级为内存态/RRF 融合序,"
              " 表现为“回答质量下降但链路正常”。")
    if required_fail:
        return 1
    if strict and (warn or fail):
        return 1
    return 0


def _action_up(build: bool, with_assistant: bool) -> int:
    missing = [
        n for n in ("pg_password", "deepseek_api_key")
        if not (BASE_DIR / "docker" / "secrets" / f"{n}.txt").is_file()
    ]
    if missing:
        print(
            f"缺少密钥文件: {', '.join(missing)} -> 见 docker/secrets/README.md 的创建命令。\n"
            "注意: pg_password.txt 必须与 pg_data 卷里已有的口令一致, 不要随意重生成。",
            file=sys.stderr,
        )
        return 2
    services = list(DEV_SERVICES) + (["assistant"] if with_assistant else [])
    rc = _run_compose("up", "-d", *services, build=build)
    if rc != 0:
        return rc
    print("\n服务拉起中(TEI 加载权重、ES 建索引需要几十秒), 就绪与否以自检为准:")
    print("  uv run python -m scripts.dev_services check")
    return 0


def _action_down() -> int:
    return _run_compose("stop", *DEV_SERVICES, "assistant")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="docker 依赖服务管理与对接自检")
    sub = parser.add_subparsers(dest="action", required=True)

    p_up = sub.add_parser("up", help="起 dev 期需要的 compose 服务(默认不含 assistant)")
    p_up.add_argument("--build", action="store_true", help="先重建镜像(改过 app/mcp/agent 代码时需要)")
    p_up.add_argument("--with-assistant", action="store_true", help="连 assistant 一起起(整栈验证用, 会与宿主网关抢 18000 端口)")

    p_check = sub.add_parser("check", help="逐服务对接自检")
    p_check.add_argument("--strict", action="store_true", help="WARN 也视为失败(退出码 1)")
    p_check.add_argument("--gateway", action="store_true", help="顺带探宿主网关 /health")

    sub.add_parser("down", help="停掉 dev 期服务(只停不删卷)")

    args = parser.parse_args(argv)
    if args.action == "up":
        return _action_up(build=args.build, with_assistant=args.with_assistant)
    if args.action == "down":
        return _action_down()
    return asyncio.run(_action_check(strict=args.strict, gateway=args.gateway))


if __name__ == "__main__":
    sys.exit(main())
