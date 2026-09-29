
app/agents/analyst_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/analyst_agent/executor.py:
⋮
│def _build_role_prompt(role: Role) -> str:
⋮
│class AnalystAgent:
│    """LangGraph ReAct agent over analytics MCP tools, role-aware."""
│
⋮
│    async def _ensure_agent(self, role: Role = Role.MANAGER) -> Any:
⋮
│    async def invoke(self, user_text: str, user_id: str, role: Role) -> str:
⋮
│class AnalystAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge: A2A task -> AnalystAgent invocation."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/analyst_agent/server.py:
⋮
│def create_app():
⋮

app/agents/contract_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/contract_agent/executor.py:
⋮
│def _build_role_prompt(role: Role) -> str:
⋮
│class ContractAgent:
│    """LangGraph ReAct agent over procurement MCP tools, role-aware."""
│
⋮
│    async def _ensure_agent(self, role: Role = Role.EMPLOYEE) -> Any:
⋮
│    async def invoke(self, user_text: str, user_id: str, role: Role) -> str:
⋮
│class ContractAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge: A2A task -> ContractAgent invocation."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/contract_agent/server.py:
⋮
│def create_app():
⋮

app/agents/finance_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/finance_agent/executor.py:
⋮
│def _build_role_prompt(role: Role) -> str:
⋮
│class FinanceAgent:
│    """LangGraph ReAct agent over finance MCP tools, role-aware."""
│
⋮
│    async def _ensure_agent(self, role: Role = Role.EMPLOYEE) -> Any:
⋮
│    async def invoke(self, user_text: str, user_id: str, role: Role) -> str:
⋮
│class FinanceAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge: A2A task -> FinanceAgent invocation."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/finance_agent/server.py:
⋮
│def create_app():
⋮

app/agents/hr_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/hr_agent/executor.py:
⋮
│def _build_role_prompt(role: Role) -> str:
⋮
│class HRAgent:
│    """LangGraph ReAct agent over HR MCP tools, role-aware."""
│
⋮
│    async def _ensure_agent(self, role: Role = Role.EMPLOYEE) -> Any:
⋮
│    async def invoke(self, user_text: str, user_id: str = "", role: Role = Role.EMPLOYEE) -> str:
⋮
│class HRAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge for HR_Agent."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/hr_agent/server.py:
⋮
│def create_app():
⋮

app/analytics/charts.py:
⋮
│def _text_width(label: str) -> float:
⋮
│def _to_number(value: Any) -> float:
⋮
│def fmt(value: float, digits: int = 2) -> str:
⋮
│def _nice_ticks(hi: float, count: int = 5) -> tuple[float, float, float]:
⋮
│def normalize(
│    categories: Sequence[Any],
│    series: Sequence[Any] | None,
│    values: Sequence[Any] | None,
⋮
│def _category_labels(chart: dict[str, Any]) -> list[str]:
⋮
│def bar_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
⋮
│def line_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
⋮
│def pie_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
⋮
│def _load_font(size: int):
⋮
│def _truncate(draw: Any, text: str, font: Any, max_px: int) -> str:
⋮
│def render_png(
│    chart_type: str,
│    title: str,
│    categories: Sequence[Any],
│    series: Sequence[Any] | None = None,
│    values: Sequence[Any] | None = None,
│    background: str = "#FFFFFF",
│) -> dict[str, Any]:
│    """与 :func:`render` 同数据同图型, 但产出 PNG 字节(供 office 内嵌)。
│
│    Returns:
│        ``{png: bytes, chart_type, categories_count}``; 无数据/图型不支持返回 ``{error}``。
│        Pillow 不可用时也返回 ``{error}`` 而不是抛出(调用方据此只出 SVG)。
⋮
│    def y_of(v: float) -> float:
⋮
│def _png_cat_labels(draw: Any, cats: list[str], slot: float, x0: float, y1: float, font: Any) -> No
⋮
│def _png_pie(draw: Any, cats: list[str], data: list[float], font: Any, small: Any) -> None:
⋮

app/analytics/reports.py:
⋮
│def current_cst_date() -> date:
⋮
│def resolve_range(period: str, end: date | None = None) -> tuple[date, date, str]:
⋮
│def collect_metrics(start: date, end: date) -> dict[str, Any]:
⋮
│def build_markdown(metrics: dict[str, Any], label: str, charts: list[tuple[str, str]]) -> str:
⋮

app/analytics/store.py:
⋮
│def reports_dir() -> Path:
⋮
│def artifact_url(name: str) -> str:
⋮
│def slugify(text: str, max_len: int = 28) -> str:
⋮
│def stamp(prefix: str, ext: str, title: str = "") -> str:
⋮
│def is_safe_name(name: str) -> bool:
⋮
│def write_text(name: str, content: str, *, created_by: str = "", title: str = "", params: dict | No
⋮
│def write_bytes(
│    name: str, data: bytes, *, created_by: str = "", title: str = "", params: dict | None = None
⋮
│def _register_ledger(
│    *, name: str, kind: str, title: str, created_by: str, params: dict | None, size: int
⋮
│def recent_artifacts(created_by: str = "", limit: int = 10) -> list[dict[str, Any]]:
⋮

app/assistant/a2a_client.py:
⋮
│def _pin_card_url(card, base_url: str, domain: str):
⋮
│class A2AClientPool:
│    """Lazily-resolved A2A clients keyed by agent domain.
│
│    并发下三件事(缺一不可):
│    - 共享一个带上限的 ``httpx.AsyncClient``: 委派是多步办理, 一次可能跑几十秒,
│      不卡上限就是"默认 100 条连接 + 无 keepalive 上限"直接把智能体压垮。
│    - 卡片发现加单飞锁: 旧写法没有锁, 同时到来的 N 个委派会做 N 次 agent-card 发现
│      (每个都一次 HTTP + 一整套卡片解析), 而且同一域名可能被写入两次不同的 client。
│    - 有明确的关停入点: 不关就是常驻连接池在进程退出时留一堆未释放资源。
⋮
│    def _lock_obj(self) -> asyncio.Lock:
⋮
│    def _http_client(self) -> httpx.AsyncClient:
⋮
│    async def _get_client(self, domain: str) -> A2AClient:
⋮
│    async def aclose(self) -> None:
⋮
│    @staticmethod
│    def _extract_text(result: Any) -> str:
⋮
│    async def send(
│        self,
│        domain: str,
│        text: str,
│        context_id: str | None = None,
│        metadata: dict[str, Any] | None = None,
⋮
│async def close_a2a_pool() -> None:
⋮

app/assistant/graph.py:
⋮
│class AssistantOrchestrator:
│    """Single-entry Assistant that routes across KB / MCP / A2A layers."""
│
⋮
│    async def setup(self) -> None:
⋮
│    async def shutdown(self) -> None:
⋮
│    async def refresh_knowledge(self) -> None:
⋮
│    async def handle_stream(self, req: ChatRequest) -> str:
⋮
│def get_orchestrator() -> AssistantOrchestrator:
⋮
│def get_graph():
⋮

app/assistant/mcp_client.py:
⋮
│class MCPClientPool:
│    """Pool of MCP tool connections keyed by server name."""
│
⋮
│    async def _lock_for(self, server: str | None) -> asyncio.Lock:
⋮
│    async def get_tools(self, server: str | None = None) -> list[BaseTool]:
⋮
│    async def refresh(self, server: str | None = None) -> None:
⋮
│def get_mcp_pool() -> MCPClientPool:
⋮
│def _flatten(result: Any) -> str:
⋮

app/assistant/memory.py:
⋮
│@dataclass
│class SessionMemory:
⋮
│def _render_turn(user: str, assistant: str) -> str:
⋮
│class MemoryStore:
│    """Session memory manager with summarization on overflow."""
│
⋮
│    def _redis_or_none(self):
⋮
│    def _turns_key(self, session_id: str) -> str:
⋮
│    def _summary_key(self, session_id: str) -> str:
⋮
│    def _local(self, session_id: str) -> SessionMemory:
⋮
│    def _local_history_text(self, session_id: str) -> str:
⋮
│    async def _local_append(self, session_id: str, user: str, assistant: str) -> str:
⋮
│    async def append(self, session_id: str, user: str, assistant: str) -> str:
⋮

app/assistant/planner.py:
⋮
│def clean_tasks(raw: str, max_tasks: int) -> tuple[list[str], int]:
⋮
│class TaskPlanner:
│    """复合问法 -> 2~N 条独立子问题(一次 LLM 调用, 走 Prompt Cache)。"""
│
⋮
│    def looks_multi(self, message: str) -> bool:
⋮
│    async def split(self, message: str, history: str = "") -> tuple[list[str], int]:
⋮

app/assistant/router.py:
⋮
│@router.post("/chat", response_model=ChatResponse)
│async def chat(req: ChatRequest) -> ChatResponse:
⋮
│@router.get("/sessions")
│async def list_sessions(user_id: str, limit: int = 50) -> list[dict]:
⋮
│@router.delete("/sessions/{session_id}")
│async def delete_session(session_id: str) -> dict[str, str]:
⋮

app/assistant/stream.py:
⋮
│class RunOverloaded(RuntimeError):
⋮
│class RunBuffer:
│    """单个 run 的事件缓冲: 自增 id + Event 唤醒, 支持多读者断点重放。
│
│    并发规模下两个硬约束(1000 人同时在流时缺一不可):
│    - 读侧用 Event 唤醒而不是 50ms 轮询: 轮询时每 50ms 都重扫整条事件列表,
│      开销是 O(事件数 × 读者数 × 20 次/秒), 几千条 token 事件会把事件循环烧穿;
│      改为二分定位切片 + 新事件 set 唤醒, 空闲读者只挂在一个 Event 上。
│    - 事件条数有上限(``stream_max_events``): 超限后丢弃最老事件, 内存有顶;
│      续流凭"尾部 + id 单调"仍然成立, 最终 result 事件不会被丢。
│    id 仍然单调递增(只是可能有空洞), "id > cursor" 的过滤式重放天然幂等。
⋮
│    async def append(self, event: dict[str, Any]) -> int:
⋮
│    def _fold_discarded(self, discarded: list[tuple[int, dict[str, Any]]]) -> None:
⋮
│    async def mark_done(self) -> None:
⋮
│    def pending(self, cursor: int) -> list[tuple[int, dict[str, Any]]]:
⋮
│    async def wait_new(self, timeout: float = 1.0) -> None:
⋮
│    def mark_seen(self) -> None:
⋮
│    async def iterate(self, from_id: int = 0) -> AsyncIterator[tuple[int, dict[str, Any]]]:
⋮
│def bisect_key(events: list[tuple[int, Any]], cursor: int) -> int:
⋮
│class StreamHub:
│    """run_id -> RunBuffer 的进程内注册表 + 同时在跑的 run 并发闸门。
│
│    三道治理(都是 1000 人共用一个网关进程时的硬需求):
│    1. TTL 惰性回收(原行为) + 限频扫描: 原实现每条命令全表扫一遍,
│       百个活跃 run x 每秒几百条事件时扫描本身就成了热路径开销。
│    2. 缓冲区总数硬顶(``stream_max_buffers``): 触顶时按结束时间最老提前回收,
│       否则"刷新后再也不回来"的会话会把 finished 缓冲区堆到内存耗尽。
│    3. 并发闸门(``stream_max_concurrent_runs``): 新建 run 前先过闸, 超上限立刻
│       拒绝(路由层转 503), 而不是让所有下游(LLM 配额/PG/事件循环)被拖到集体超时。
⋮
│    def try_acquire_run(self) -> bool:
⋮
│    def release_run(self) -> None:
⋮
│    @property
│    def inflight(self) -> int:
⋮
│    def create(self, run_id: str) -> RunBuffer:
⋮
│    def get(self, run_id: str) -> RunBuffer | None:
⋮
│    async def append(self, run_id: str, event: dict[str, Any]) -> int | None:
⋮
│    async def finish(self, run_id: str) -> None:
⋮
│    def _sweep(self) -> None:
⋮
│def get_stream_hub() -> StreamHub:
⋮
│def sse_frame(event_id: int, event: dict[str, Any]) -> str:
⋮

app/bodies/client.py:
⋮
│def mongo_database_url() -> str:
⋮
│def get_mongo_client() -> AsyncIOMotorClient:
⋮
│def get_db():
⋮
│async def init_body_schema() -> None:
⋮
│async def close_mongo() -> None:
⋮

app/bodies/store.py:
⋮
│def _split_text(text: str, cap_bytes: int) -> list[str]:
⋮
│@dataclass
│class ParentTextItem:
⋮
│class BodyStore:
│    """正文外置存储门面: 进程级单例, 构造零 I/O(同 PgVectorStore 约定)。"""
│
⋮
│    async def save_doc_body(
│        self,
│        doc_key: str,
│        *,
│        raw: str,
│        normalized: str,
│        structure: list[dict],
│        meta: dict | None = None,
⋮
│    async def get_doc_body(
│        self, doc_key: str, *, field: str = "normalized", head_only: bool = False
⋮
│    async def delete_doc_body(self, doc_key: str) -> None:
⋮
│    async def save_parent_texts(self, items: Sequence[ParentTextItem]) -> int:
⋮
│    async def get_parent_texts(self, parent_ids: Sequence[str]) -> dict[str, str]:
⋮
│    async def delete_parents_by_doc(self, doc_id: str) -> int:
⋮
│    async def delete_stale_parents(self, doc_id: str, keep_ids: set[str]) -> int:
⋮
│    async def count(self) -> dict[str, int]:
⋮
│    async def prune_orphans(
│        self, live_doc_keys: set[str], live_parent_ids: set[str]
⋮
│def get_body_store() -> BodyStore:
⋮

app/cache/prompt_cache.py:
⋮
│async def cached_llm_call(
│    model: str,
│    temperature: float,
│    prompt: str,
│    invoke: Callable[[], Awaitable[str]],
⋮

app/cache/redis_client.py:
⋮
│def get_redis() -> Redis | None:
⋮
│async def try_redis(
│    op: Callable[[], Awaitable[T]],
│    *,
│    default: T | None = None,
│    what: str = "redis op",
⋮
│async def close_redis() -> None:
⋮

app/cache/retrieval_cache.py:
⋮
│def acl_signature(principal: Principal | None) -> str:
⋮
│async def invalidate_all() -> None:
⋮

app/cache/tool_cache.py:
⋮
│def is_cacheable_tool_name(name: str) -> bool:
⋮
│async def cached_tool_call(
│    server: str,
│    tool_name: str,
│    args: dict,
│    role: str,
│    invoke: Callable[[], Awaitable[str]],
⋮

app/chat_store.py:
⋮
│class ChatStore:
│    """会话/消息的异步 DAO; 构造期不做任何 I/O。"""
│
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮
│    async def list_sessions(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
⋮
│    async def get_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
⋮
│    async def delete_session(self, session_id: str) -> None:
⋮
│def get_chat_store() -> ChatStore:
⋮

app/config.py:
⋮
│class Settings(BaseSettings):
│    """Central configuration for the whole platform.
│
│    取值优先级(高->低): 真实环境变量 -> .env.local -> .env -> 字段默认值。
│
│    配置只有一条宿主轨: ``.env`` 是**宿主机视角**(全部指向 docker 已发布端口),
│    ``.env.local`` 是同一视角的机器私有覆盖(均已 gitignore)。**不要**把
│    ``docker/.env`` 加进 env_file: pydantic-settings 是后者覆盖前者, 那会把容器
│    服务名(tei-rerank/elasticsearch/mongo/...)灌进宿主机进程(本轨现在只给
│    dev_services/评测等脚本用, 网关已只跑在容器内), 解析失败后静默降级
│    (见 CONFIG_RULES.md 第 5 条 → config-env-tracks §1)。
⋮
│    @field_validator(
│        "pg_password", "deepseek_api_key", "zhipu_api_key", "langsmith_api_key",
│        "langfuse_api_key", "mongo_password", "tavily_api_key", "serper_api_key",
│        mode="after",
⋮
│    def _read_from_secret_file(cls, value: str, info) -> str:
⋮
│    @property
│    def llm_providers(self) -> dict[str, dict]:
⋮
│    def resolve_llm_provider(self, model: str) -> tuple[str, dict] | None:
⋮
│    def model_post_init(self, __context) -> None:
⋮
│    @property
│    def base_dir(self) -> Path:
⋮
│@lru_cache
│def get_settings() -> Settings:
⋮

app/db/models.py:
⋮
│class Document(Base):
⋮
│class DocumentTag(Base):
⋮
│class Employee(Base):
⋮
│class HRTicket(Base):
⋮
│class LeaveRecord(Base):
⋮
│class Reimbursement(Base):
⋮
│class DepartmentBudget(Base):
⋮
│class Supplier(Base):
⋮
│class PurchaseRequest(Base):
⋮
│class ContractReview(Base):
⋮
│class LongTermMemoryRow(Base):
⋮
│class ChatMessage(Base):
⋮
│class ReportArtifact(Base):
⋮

app/db/session.py:
⋮
│def _password() -> str:
⋮
│def async_database_url() -> str:
⋮
│def _connect_args() -> dict:
⋮
│def get_engine() -> AsyncEngine:
⋮
│def get_session_factory() -> async_sessionmaker[AsyncSession]:
⋮
│async def init_schema() -> None:
⋮
│def db_available() -> bool:
⋮

app/db/sql_guard.py:
⋮
│class SQLGuardError(ValueError):
⋮
│def validate_readonly_select(sql: str, allowed_tables: set[str]) -> str:
⋮

app/db/sync.py:
⋮
│def _jsonify(value):
⋮
│def _ensure_password() -> str:
⋮
│def get_sync_engine() -> Engine:
⋮
│def execute_readonly_sql(
│    sql: str, allowed_tables: set[str], params: dict | None = None
⋮

app/docgen/docx_builder.py:
⋮
│def _add_images(doc: Any, images: list[dict[str, Any]]) -> None:
⋮

app/docgen/genstore.py:
⋮
│def new_token() -> str:
⋮
│def _upload_dir() -> Path:
⋮
│def gen_root() -> Path:
⋮
│def new_file_name(ext: str, title: str = "") -> str:
⋮
│def build_path(token: str, file_name: str) -> Path | None:
⋮
│def mime_of(ext: str) -> str:
⋮
│def cleanup_expired() -> int:
⋮
│def parse_spec(raw: Any) -> tuple[dict[str, Any] | None, str]:
⋮

app/docgen/images.py:
⋮
│async def resolve_images(spec_images: Any, dest_dir: Path) -> tuple[list[dict[str, Any]], list[str]
⋮

app/docgen/md_builder.py:
⋮
│def _table_md(table: dict[str, Any]) -> list[str]:
⋮

app/docgen/pdf_builder.py:
⋮
│def _ensure_font() -> None:
⋮
│def _table_flowable(table: dict[str, Any]) -> Table | None:
⋮
│def build_pdf(path: Path, spec: dict[str, Any]) -> None:
⋮
│def _append_images(story: list[Any], images: list[dict[str, Any]]) -> None:
⋮

app/docgen/pptx_builder.py:
⋮
│def _bullet_text(item: Any) -> tuple[str, int]:
⋮
│def _add_images(prs: Any, images: list[dict[str, Any]]) -> None:
⋮

app/docgen/xlsx_builder.py:
⋮
│def _normalize_sheets(spec: dict[str, Any]) -> list[dict[str, Any]]:
⋮
│def _add_images(wb: Any, images: list[dict[str, Any]]) -> None:
⋮

app/docs/normalize.py:
⋮
│def normalize_text(text: str) -> str:
⋮
│def content_hash(text: str) -> str:
⋮

app/docs/parsers.py:
⋮
│def _get_mineru_client() -> httpx.AsyncClient:
⋮
│async def close_mineru_client() -> None:
⋮
│@dataclass
│class ParsedBlock:
⋮
│def supported_extensions() -> set[str]:
⋮
│def modality_of(path: Path) -> str:
⋮
│def _parse_md(path: Path) -> list[ParsedBlock]:
│    """Split markdown by headings; section = current heading path."""
⋮
│    def flush() -> None:
⋮
│def _page_image_pngs(page) -> list[bytes]:
⋮
│async def _parse_pdf(path: Path) -> list[ParsedBlock]:
⋮
│def _parse_docx(path: Path) -> list[ParsedBlock]:
│    """Split docx by Heading styles (supports EN/CN style names)."""
⋮
│    def flush() -> None:
⋮
│async def _mineru_parse(filename: str, data: bytes) -> str:
⋮
│async def _parse_image(path: Path) -> list[ParsedBlock]:
⋮
│async def parse_blocks(path: Path) -> tuple[str, list[ParsedBlock]]:
⋮

app/docs/service.py:
⋮
│class UploadError(ValueError):
⋮
│def _upload_dir() -> Path:
⋮
│async def stage_upload(filename: str, upload) -> tuple[str, Path, str]:
⋮
│async def check_existing(doc_key: str) -> Document | None:
⋮
│async def suggest_tags(text: str) -> list[str]:
⋮
│def normalize_acl(
│    visibility: str, owner_id: str, dept_id: str, allowed_roles: list[str] | str
⋮
│async def ingest_confirmed(
│    doc_key: str,
│    filename: str,
│    tags: list[str],
│    uploader: str,
│    visibility: str = "public",
│    dept_id: str = "",
│    allowed_roles: list[str] | str = "",
⋮
│async def update_document_acl(
│    doc_key: str,
│    visibility: str,
│    dept_id: str = "",
│    allowed_roles: list[str] | str = "",
│    operator: str = "",
⋮
│async def delete_document(doc_key: str) -> dict[str, Any]:
⋮
│async def get_meta_map(doc_keys: list[str]) -> dict[str, dict[str, Any]]:
⋮
│async def list_documents() -> list[dict[str, Any]]:
⋮
│async def list_tags() -> list[dict[str, Any]]:
⋮

app/kg/extract.py:
⋮
│@dataclass
│class DocKG:
⋮
│async def extract_doc_graph(title: str, tags: list[str], text: str) -> DocKG:
⋮

app/kg/router.py:
⋮
│@router.get("/graph")
│async def get_graph(
│    user_id: str = Query("", description="当前用户工号(ACL 判定)"),
│    role: str = Query("employee", description="当前角色(ACL 判定)"),
│    department: str = Query("", description="当前部门(ACL 判定)"),
│    focus: str | None = Query(None, description="聚焦展开的文档 doc_key"),
│    hops: int | None = Query(None, ge=1, le=4, description="邻域跳数"),
│    limit: int | None = Query(None, ge=1, le=1000, description="节点上限"),
⋮

app/kg/service.py:
⋮
│async def build_for_doc(doc_key: str) -> dict[str, Any]:
⋮
│async def rebuild_all(limit_docs: int | None = None) -> dict[str, Any]:
⋮
│async def get_graph(
│    principal: Principal,
│    focus: str | None = None,
│    hops: int | None = None,
│    limit: int | None = None,
⋮

app/kg/store.py:
⋮
│async def ensure_schema() -> None:
⋮
│async def upsert_document_graph(
│    doc_key: str,
│    name: str,
│    ext: str,
│    modality: str,
│    entities: Sequence[dict],
│    relations: Sequence[dict],
⋮
│async def delete_document_graph(doc_key: str) -> None:
⋮
│async def query_subgraph(
│    allowed_doc_keys: Sequence[str],
│    focus: str | None = None,
│    hops: int | None = None,
│    limit: int | None = None,
⋮

app/llm.py:
⋮
│def _load_model_class(dotted: str):
⋮
│def _render_extra_body(thinking: bool, template: str, effort: str) -> dict[str, Any]:
⋮
│def get_chat_model(
│    model: str | None = None,
│    *,
│    temperature: float = 0.1,
│    json_mode: bool = False,
│    thinking: bool | None = None,
⋮
│def get_streaming_chat_model(
│    model: str | None = None,
│    *,
│    temperature: float = 0.1,
│    thinking: bool = True,
⋮
│def extract_reasoning(msg: AIMessage | AIMessageChunk) -> str:
⋮

app/main.py:
⋮
│def create_app() -> FastAPI:
⋮

app/mcp_servers/analytics_server.py:
⋮
│def _parse_date(value: str, fallback: date) -> date:
⋮
│@mcp.tool()
│def render_chart(
│    title: str,
│    chart_type: str = "bar",
│    categories: list[Any] | None = None,
│    series: list[dict[str, Any]] | None = None,
│    values: list[Any] | None = None,
⋮

app/mcp_servers/finance_server.py:
⋮
│def _order_dict(o: Reimbursement) -> dict[str, Any]:
⋮

app/mcp_servers/hr_server.py:
⋮
│def _ticket_dict(t: HRTicket) -> dict[str, Any]:
⋮

app/mcp_servers/procurement_server.py:
⋮
│def _order_dict(o: PurchaseRequest) -> dict[str, Any]:
⋮
│@mcp.tool()
│def precheck_purchase_order(order_no: str) -> dict[str, Any]:
⋮

app/memory/extraction.py:
⋮
│def _parse_date(raw: object) -> datetime | None:
⋮
│def _clean_str(raw: object, max_len: int = 500) -> str:
⋮
│def _str_list(raw: object, max_len: int = 500) -> list[str]:
⋮
│@dataclass
│class EpisodeRecord:
⋮
│@dataclass
│class KnowledgeRecord:
⋮
│@dataclass
│class MemoryExtraction:
⋮
│async def extract_memories(
│    message: str,
│    answer: str,
│    *,
│    existing_preferences: Sequence[str] = (),
│    existing_habits: Sequence[str] = (),
⋮

app/memory/graph_store.py:
⋮
│def _iso_utc(value: datetime | None) -> str | None:
⋮
│def _relation_row(raw: dict, *, now_dt: datetime, grace_days: int) -> dict:
⋮
│def get_driver() -> AsyncDriver | None:
⋮
│async def ensure_schema() -> None:
⋮
│def _entity_type(raw: object) -> str:
⋮
│async def upsert_entities(
│    user_id: str,
│    entities: Sequence[dict],
│    relations: Sequence[dict],
│    *,
│    source: str = "turn",
⋮
│async def related_facts(
│    user_id: str,
│    entity_names: Sequence[str],
│    hops: int | None = None,
│    *,
│    include_expired: bool = False,
⋮
│async def entity_names(user_id: str, limit: int = 200) -> list[str]:
⋮
│async def user_subgraph(user_id: str, limit: int = 60) -> dict[str, list[dict]]:
⋮
│async def close_driver() -> None:
⋮

app/memory/personal.py:
⋮
│@dataclass
│class PersonalContext:
│    """一轮对话召回的个人记忆; 空桶不产生 prompt 小节。"""
│
⋮
│    def as_prompt(self) -> str:
⋮
│class PersonalMemoryAgent:
│    """个人级记忆的读写编排; 无状态, 进程级单例。"""
│
⋮
│    async def build(self, user_id: str, query: str) -> PersonalContext:
⋮
│    async def existing_stable_texts(self, user_id: str) -> tuple[list[str], list[str]]:
⋮
│    async def _touch(self, memory_ids: list[int]) -> None:
⋮
│    async def write(
│        self,
│        user_id: str,
│        session_id: str,
│        extraction: MemoryExtraction,
│        *,
│        source: str = SOURCE_TURN,
⋮
│    async def add_session_episode(self, user_id: str, session_id: str, summary: str) -> bool:
⋮
│    async def consolidate_stable_buckets(self, user_id: str) -> int:
⋮
│    async def delete(self, user_id: str, item_id: int) -> bool:
⋮
│    async def clear(self, user_id: str, bucket: MemoryBucket | str) -> int:
⋮
│def get_personal_agent() -> PersonalMemoryAgent:
⋮

app/memory/profile_store.py:
⋮
│def _empty_profile() -> dict[str, Any]:
⋮
│def _normalize_key(key: str) -> str:
⋮
│def _in_pool(key: str, pool: Iterable[str]) -> bool:
⋮
│def is_measurement(key: str) -> bool:
⋮
│def is_correction(key: str) -> bool:
⋮
│def _clean(value: Any, max_len: int = 120) -> str:
⋮
│def _as_list(value: Any) -> list[Any]:
⋮
│def _observation(raw: Any, *, fallback_time: datetime | None) -> TimedValue | None:
⋮
│def _ordered(
│    observations: Iterable[TimedValue], *, now: datetime, grace_days: int
⋮
│def merge_attributes(
│    old: dict[str, Any] | None,
│    items: list[dict],
│    *,
│    history: dict[str, Any] | None = None,
│    now: datetime | None = None,
│    stored_at: datetime | None = None,
│    max_entries: int | None = None,
│    grace_days: int | None = None,
│) -> tuple[dict[str, list[str]], dict[str, list[dict]], int, int, int]:
│    """把提取到的 ``[{key, value, valid_at, explicit}]`` 合并进既有画像(纯函数, 便于单测)。
│
│    返回 ``(当前值画像, 观测序列, 新增槽位数, 更新次数, 被拦下的历史陈述数)``。规则:
│
│    - 英文同义键先归一(如 department -> 部门), 避免同一事实占两个槽;
│    - 波动类键(体重/身高/部门/职位/职级/汇报对象/所在地/年龄/入职时间): 每个值是
│      一条观测, 当前值按**生效时间**派生 —— 在讲过去的陈述(明说了时间且已在
│      ``grace_days`` 之前)只进历史、不改当前值(用户先说"现在 70kg", 后又说"2015 年秋
│      64kg", 画像仍得是 70kg); 没标时间的按本轮日期当下生效, 同一槽位上后听到的赢;
│      "从上个月起…"这类近期起始日期归入"当下生效"一类(它描述的是持续到现在的变更),
⋮
│    def _touch(key: str) -> None:
⋮
│    def _absorb(key: str, raw: Any) -> None:
⋮
│def _same_observation(a: TimedValue, b: TimedValue) -> bool:
⋮
│def _current_observation(entries: Any) -> TimedValue | None:
⋮
│def render_summary(
│    attrs: dict[str, Any], max_chars: int | None = None, *, history: dict[str, Any] | None = None
⋮
│class UserProfileStore:
│    """画像的异步 DAO; 构造期不做任何 I/O, DB 不可用一律静默降级。"""
│
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮
│    async def get(self, user_id: str) -> dict[str, Any]:
⋮
│    async def merge(self, user_id: str, items: list[dict]) -> dict[str, int]:
⋮
│    async def clear(self, user_id: str) -> bool:
⋮
│    async def get_last_reflected_at(self, user_id: str) -> datetime | None:
⋮
│    async def mark_reflected(self, user_id: str, *, at: datetime | None = None) -> None:
⋮
│def get_profile_store() -> UserProfileStore:
⋮

app/memory/taxonomy.py:
⋮
│class MemoryBucket(str, Enum):
⋮
│@dataclass(frozen=True)
│class BucketSpec:
⋮
│def spec_of(bucket: MemoryBucket | str) -> BucketSpec:
⋮
│def label_of(kind: str) -> str:
⋮

app/memory/vector_store.py:
⋮
│def _is_same_observation(existing: datetime | None, incoming: datetime | None, window_days: int) ->
⋮
│@dataclass
│class MemoryHit:
⋮
│class LongTermMemoryStore:
│    """跨会话个人记忆的向量存储与召回 (按 user_id 隔离)。"""
│
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮
│    async def upsert_memory(
│        self,
│        user_id: str,
│        content: str,
│        *,
│        kind: str = "fact",
│        source_session_id: str = "",
│        title: str = "",
│        source: str = "turn",
│        occurred_at: datetime | None = None,
⋮
│    async def search_memories(
│        self, user_id: str, query: str, top_k: int | None = None
⋮
│    async def search_by_buckets(
│        self,
│        user_id: str,
│        query: str,
│        kinds: Sequence[str],
│        limit: int | None = None,
⋮
│    async def list_recent(
│        self,
│        user_id: str,
│        kinds: Sequence[str],
│        limit: int = 5,
│        *,
│        order_by: str = "last_accessed_at",
⋮
│    async def count_since(self, user_id: str, kind: str, since: datetime | None) -> int:
⋮
│    async def count_by_kind(self, user_id: str) -> dict[str, int]:
⋮
│    async def delete_items(self, user_id: str, memory_ids: Sequence[int]) -> int:
⋮
│    async def merge_group(
│        self, user_id: str, keep_id: int, content: str, drop_ids: Sequence[int]
⋮
│    async def clear_bucket(self, user_id: str, kind: str) -> int:
⋮
│def get_long_term_store() -> LongTermMemoryStore:
⋮

app/procurement/rules.py:
⋮
│@dataclass
│class Finding:
│    """一条初审结论: 项名 + 严重度 + 建议。"""
│
⋮
│    def to_dict(self) -> dict[str, Any]:
⋮
│@dataclass
│class PrecheckOutcome:
│    """一份合同/一张采购单的初审结果汇总。"""
│
⋮
│    def to_dict(self) -> dict[str, Any]:
⋮
│def _parse_date(value: Any) -> date | None:
⋮
│def _to_decimal(value: Any) -> Decimal:
⋮
│def _risk_of(findings: list[Finding]) -> str:
⋮
│def _conclusion(risk_level: str, findings: list[Finding]) -> str:
⋮
│def precheck_contract(
│    *,
│    content: str = "",
│    title: str = "",
│    party_a: str = "",
│    party_b: str = "",
│    amount: Any = 0,
│    currency: str = "CNY",
│    sign_date: Any = None,
│    effective_date: Any = None,
⋮
│def precheck_purchase_order(
│    *,
│    amount: Any,
│    department: str = "",
│    supplier_name: str = "",
│    quotes_count: int = 1,
│    category: str = "",
│    budget: dict[str, Any] | None = None,
│    supplier: dict[str, Any] | None = None,
⋮

app/rag/bm25.py:
⋮
│def tokenize(text: str) -> list[str]:
⋮
│def _token_field(text: str) -> str:
⋮
│def _acl_filter(principal: Principal | None) -> list[dict]:
⋮
│def _doc_source(chunk: KnowledgeChunk) -> dict:
⋮
│class ElasticBM25Retriever:
│    """BM25 lexical channel backed by Elasticsearch.
│
│    高并发下的三个要点(都不是可选的):
│    - 索引存在性只探一次: 旧实现每次 ``search`` 都跑一次 ``indices.exists``,
│      等于每轮对话白送一个 ES 往返(几百 QPS 时它比查询本身还贵)。
│    - 检索超时是秒级而非 30s: ES 半死时 30s 挂钟会把连接和内存拖爆,
│      这里宁可快速失败走"稀疏通道为空"的降级。
│    - 稀疏通道有并发上限: 超出部分在本地排队而不是把 ES 压到雪崩。
⋮
│    async def aclose(self) -> None:
⋮
│    async def ensure_index(self) -> None:
⋮
│    async def _create_index(self) -> None:
⋮
│    async def rebuild(self, chunks_or_iter) -> None:
⋮
│    async def index_chunks(self, chunks: Sequence[KnowledgeChunk], refresh: bool = True) -> None:
⋮
│    async def search(
│        self, query: str, top_k: int, principal: Principal | None = None
⋮
│def get_es_bm25() -> ElasticBM25Retriever:
⋮
│async def close_es_client() -> None:
⋮

app/rag/embeddings.py:
⋮
│def _get_query_gate() -> asyncio.Semaphore:
⋮
│def get_embedder_client() -> httpx.AsyncClient:
⋮
│async def close_embedder_client() -> None:
⋮
│class OllamaEmbedder:
│    """Thin async client for Ollama's /api/embed endpoint.
│
│    bge-m3 produces 1024-dim dense vectors and natively supports
│    multilingual + long-context (8k) inputs, which fits enterprise
│    Chinese/English mixed documents.
⋮
│    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
⋮
│    async def embed_query(self, text: str) -> list[float]:
⋮
│    async def _embed_one(self, text: str, timeout: float) -> list[list[float]]:
⋮

app/rag/ingest.py:
⋮
│def compute_doc_id(name: str, ext: str) -> str:
⋮
│def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
⋮
│def build_structure(blocks: Sequence[ParsedBlock], normalized_text: str) -> list[dict]:
⋮
│def build_parent_child(
│    doc_id: str,
│    title: str,
│    source: str,
│    modality: str,
│    blocks: Sequence[ParsedBlock],
│    normalized_text: str,
│    parent_max: int | None = None,
│    acl: dict[str, str] | None = None,
⋮
│async def ingest_blocks(
│    doc_id: str,
│    filename: str,
│    title: str,
│    source: str,
│    modality: str,
│    blocks: Sequence[ParsedBlock],
│    store: ChunkStore,
│    embedder: OllamaEmbedder,
│    acl: dict[str, str] | None = None,
⋮
│async def ingest_file(
│    path: Path, store: ChunkStore, embedder: OllamaEmbedder, bodies=None
⋮
│async def ingest_directory(
│    dir_path: Path, store: ChunkStore, embedder: OllamaEmbedder
⋮
│async def collect_corpus(store: ChunkStore, limit: int = 100000) -> list[KnowledgeChunk]:
⋮

app/rag/reranker.py:
⋮
│def _get_client() -> httpx.AsyncClient:
⋮
│async def close_reranker_client() -> None:
⋮
│def _parse_scores(payload: object, n: int) -> list[float]:
⋮
│class TeiReranker:
│    """Score (query, chunk) relevance with a real cross-encoder served by TEI."""
│
⋮
│    @staticmethod
│    def _doc_text(chunk: KnowledgeChunk, max_chars: int) -> str:
⋮
│    async def rerank(
│        self, query: str, chunks: Sequence[KnowledgeChunk], top_n: int
⋮
│    async def probe(self) -> bool:
⋮
│def get_reranker() -> TeiReranker:
⋮

app/rag/retriever.py:
⋮
│class HybridRetriever:
│    """Enterprise knowledge retriever used by the Assistant's KB path."""
│
⋮
│    async def rebuild_bm25(self) -> None:
⋮
│    async def retrieve(
│        self,
│        query: str,
│        top_k: int | None = None,
│        top_n: int | None = None,
│        principal: Principal | None = None,
⋮
│    async def assemble_parents(self, chunks: Sequence[KnowledgeChunk]) -> list[KnowledgeChunk]:
⋮
│    def format_context(
│        self,
│        chunks: Sequence[KnowledgeChunk],
│        meta_map: dict[str, dict[str, Any]] | None = None,
⋮

app/rag/vectorstore.py:
⋮
│def _acl_predicate(R: Any, principal: Principal | None) -> ColumnElement[bool] | None:
⋮
│def _chunks_from_narrow(row: Any, score: float) -> KnowledgeChunk:
⋮
│def _chunk_row(c: KnowledgeChunk, v: Sequence[float] | None) -> dict[str, Any]:
⋮
│class ChunkStore:
│    """子块持久化 + ANN 检索(doc_chunks)。构造零 I/O。"""
│
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮
│    async def search(
│        self,
│        query_vector: Sequence[float],
│        top_k: int,
│        principal: Principal | None = None,
⋮
│    async def get_texts(self, chunk_ids: Sequence[str]) -> dict[str, str]:
⋮
│    async def attach_texts(self, chunks: Sequence[KnowledgeChunk]) -> list[KnowledgeChunk]:
⋮
│    async def existing_hashes(self, doc_id: str) -> dict[str, str]:
⋮
│    async def get_embeddings(self, chunk_ids: Sequence[str]) -> dict[str, list[float]]:
⋮
│    async def iter_chunk_corpus(
│        self, page_size: int | None = None
⋮
│    async def iter_child_chunks(self, limit: int = 100000) -> list[KnowledgeChunk]:
⋮
│    @staticmethod
│    def _row_with_vec(c: KnowledgeChunk, v: Sequence[float] | None) -> dict[str, Any]:
⋮
│    @staticmethod
│    async def _upsert_batch(
│        session: AsyncSession, dialect_insert, rows: list[dict], pk_col, upsert_cols
⋮
│    async def publish_parent_child(
│        self,
│        parent_store: "ParentStore",
│        doc_id: str,
│        parent_rows: Sequence[ParentBlock],
│        chunks: Sequence[KnowledgeChunk],
│        vectors: Sequence[Sequence[float] | None],
⋮
│    async def update_acl_by_doc(
│        self, doc_id: str, visibility: str, owner_id: str, dept_id: str, allowed_roles: str
⋮
│    async def counts(self) -> dict[str, int]:
⋮
│    async def count(self) -> int:
⋮
│class ParentStore:
│    """父块持久化(doc_parents): 结构定位, 正文在 Mongo。构造零 I/O。"""
│
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮
│    async def get_blocks(self, parent_ids: Sequence[str]) -> dict[str, ParentBlock]:
⋮
│def _parent_row(p: ParentBlock) -> dict[str, Any]:
⋮

app/schemas.py:
⋮
│class Role(str, Enum):
⋮
│class DocVisibility(str, Enum):
⋮
│class IntentType(str, Enum):
⋮
│class IntentResult(BaseModel):
⋮
│class ChatRequest(BaseModel):
⋮
│class ChatResponse(BaseModel):
⋮
│class KnowledgeChunk(BaseModel):
⋮
│class ParentBlock(BaseModel):
⋮

app/security/acl.py:
⋮
│@dataclass(frozen=True)
│class Principal:
│    """Caller identity resolved from the unified identity system."""
│
⋮
│    @property
│    def is_admin(self) -> bool:
⋮
│def format_allowed_roles(roles: list[str] | tuple[str, ...] | str) -> str:
⋮
│def parse_allowed_roles(stored: str) -> list[str]:
⋮
│def is_allowed(chunk: KnowledgeChunk, principal: Principal) -> bool:
⋮

app/security/audit.py:
⋮
│class _Barrier:
⋮
│def new_trace_id() -> str:
⋮
│class AuditLogger:
│    """Append-only JSONL audit sink, 异步落盘(调用点非阻塞)。"""
│
⋮
│    def log(
│        self,
│        trace_id: str,
│        actor: str,
│        action: str,
│        detail: dict[str, Any] | None = None,
│        session_id: str | None = None,
⋮
│    def _enqueue(self, line: str) -> None:
⋮
│    def _ensure_thread(self) -> None:
⋮
│    def _consume(self, item: Any, buf: list[str]) -> bool:
⋮
│    def _flush_buf(self, buf: list[str]) -> None:
⋮
│    def flush(self, timeout: float = 2.0) -> bool:
⋮
│    def close(self, timeout: float = 2.0) -> None:
⋮
│def get_audit_logger() -> AuditLogger:
⋮
│def shutdown_audit(timeout: float = 2.0) -> None:
⋮

app/security/auth.py:
⋮
│def filter_tools_for_role(role: Role, server_name: str, tools: list[Any]) -> list[Any]:
⋮
│class PermissionDenied(Exception):
⋮
│def check_agent_permission(role: Role, agent_name: str) -> None:
⋮
│def check_mcp_permission(role: Role, server_name: str, tool_name: str) -> None:
⋮

app/security/masking.py:
⋮
│def _mask_plain(text: str) -> str:
⋮
│def mask_text(text: str) -> str:
⋮
│def mask_sensitive(data: Any) -> Any:
⋮

app/security/url_guard.py:
⋮
│class UrlBlocked(ValueError):
⋮
│def _host_allowed_by_allowlist(host: str, allowlist: list[str]) -> bool:
⋮
│def _parse_allowlist() -> list[str]:
⋮
│def _is_denied_literal(host: str) -> bool:
⋮
│def _all_global(ips: list[str]) -> bool:
⋮
│async def resolve_and_validate(url: str) -> list[str]:
⋮

app/tools/_http.py:
⋮
│def get_web_client() -> httpx.AsyncClient:
⋮
│def get_search_client() -> httpx.AsyncClient:
⋮
│async def close_web_client() -> None:
⋮

app/tools/docgen.py:
⋮
│async def _generate(
│    kind: str, spec_raw: Any, builder, title_fallback: str, *, resolve_imgs: bool = True
⋮

app/tools/web.py:
⋮
│def _extract_text(html: str) -> tuple[str, str]:
⋮

app/tracing.py:
⋮
│def init_tracing() -> bool:
⋮
│def init_langfuse() -> bool:
⋮
│def langfuse_callback(
│    session_id: str = "", user_id: str = "", trace_id: str = ""
⋮
│def shutdown_langfuse() -> None:
⋮

main.py:
│def main():
⋮

scripts/bench_doc_stores.py:
⋮
│async def main() -> None:
⋮

scripts/build_doc_kg.py:
⋮
│async def main() -> None:
⋮

scripts/demo_reimburse.py:
⋮
│async def turn(client: httpx.AsyncClient, session: str, message: str) -> dict:
⋮
│async def main() -> None:
⋮

scripts/dev_services.py:
⋮
│class Check:
│    """一条检查: 目标地址 + 探测协程 + 失败时的修复提示。"""
│
⋮
│    async def run(self) -> "Check":
⋮
│def _read_env_pairs(rel: str) -> dict[str, str]:
⋮
│def main(argv: list[str] | None = None) -> int:
⋮

scripts/gen_repo_map.py:
⋮
│def collect_fnames(root: Path, io: InputOutput, all_files: bool) -> list[str]:
⋮
│def main() -> int:
⋮

scripts/ingest_knowledge.py:
⋮
│async def main() -> None:
⋮

scripts/init_db.py:
⋮
│async def main() -> None:
⋮

scripts/migrate_doc_stores.py:
⋮
│def seq_hint(chunk_id: str) -> int | None:
⋮
│async def verify_only() -> int:
⋮
│async def drop_legacy() -> None:
⋮
│async def main() -> None:
⋮

scripts/msmarco_eval/__main__.py:
⋮
│def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
⋮
│async def run(args: argparse.Namespace) -> int:
⋮
│def main(argv: list[str] | None = None) -> int:
⋮

scripts/msmarco_eval/chunking_ab.py:
⋮
│def assemble_documents(
│    corpus: dict[str, dict], *, seed: int, sections_per_doc: int
⋮
│def render_markdown(sections: list[tuple[str, str, str]], doc_no: int) -> tuple[str, dict[str, tupl
⋮
│def fixed_windows(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[in
⋮
│def build_arms(
│    sample,
│    *,
│    seed: int,
│    sections_per_doc: int,
⋮
│def context_efficiency(
│    rankings: dict[str, list[str]],
│    qrels: dict[str, list[str]],
│    corpus: dict[str, dict],
│    meta: dict[str, dict],
│    sample,
│    top_n: int,
⋮
│def build_markdown(arms: dict[str, Any], ab: dict[str, dict], args) -> str:
⋮
│def write_ab(ab: dict, arms: dict, args) -> tuple[Path, Path]:
⋮
│async def run(args) -> int:
⋮
│def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
⋮
│def main(argv: list[str] | None = None) -> int:
⋮

scripts/msmarco_eval/dataset.py:
⋮
│@dataclass
│class MSMarcoSample:
⋮
│def cache_dir() -> Path:
⋮
│def download_head(dest: Path, max_bytes: int, force: bool = False) -> Path:
⋮
│def iter_records(path: Path) -> Iterator[dict]:
⋮
│def load_sample(
│    num_queries: int = 200,
│    seed: int = 42,
│    head_mb: int = DEFAULT_HEAD_MB,
│    head_path: Path | None = None,
│    force_download: bool = False,
│    persist: bool = True,
⋮

scripts/msmarco_eval/harness.py:
⋮
│def corpus_to_chunks(
│    corpus: dict[str, dict],
⋮
│def _database_url_with(database: str) -> str:
⋮
│def _connect_args() -> dict:
⋮
│async def provision_eval_database(database: str = DEFAULT_EVAL_DB) -> AsyncEngine:
⋮
│def install_eval_mongo(database: str = DEFAULT_EVAL_MONGO_DB) -> None:
⋮
│def install_eval_engine(engine: AsyncEngine) -> None:
⋮
│def make_retriever(es_index: str = DEFAULT_EVAL_ES_INDEX) -> HybridRetriever:
⋮
│async def rerank_available() -> bool:
⋮
│async def ingest_corpus(
│    retriever: HybridRetriever,
│    corpus: dict[str, dict],
⋮
│async def run_queries(
│    retriever: HybridRetriever,
│    queries: Sequence[dict],
│    *,
│    top_k: int,
│    top_n: int,
│    threshold: float = 0.0,
│    rerank: bool = True,
│    concurrency: int = 4,
⋮
│async def drop_eval_stores(
│    engine: AsyncEngine,
│    *,
│    database: str = DEFAULT_EVAL_DB,
│    es_index: str = DEFAULT_EVAL_ES_INDEX,
│    drop_database: bool = True,
│    drop_eval_mongo: bool = True,
│    mongo_database: str = DEFAULT_EVAL_MONGO_DB,
⋮

scripts/msmarco_eval/metrics.py:
⋮
│def hit_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
⋮
│def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
⋮
│def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
⋮
│def mrr_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
⋮
│def dcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
⋮
│def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
⋮
│def ap_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
⋮
│def evaluate_run(
│    rankings: dict[str, Sequence[str]],
│    qrels: dict[str, Sequence[str]],
│    ks: Sequence[int] = (1, 3, 5, 10),
⋮

scripts/msmarco_eval/report.py:
⋮
│def build_report(
│    *,
│    sample_summary: dict,
│    config: dict,
│    eval_result: dict,
│    index_stats: dict,
│    run_stats: dict,
⋮
│def render_markdown(report: dict) -> str:
⋮
│def write_reports(report: dict, out_dir: Path | None = None) -> tuple[Path, Path]:
⋮

scripts/package.py:
⋮
│def _copy_tree(src: Path, dst: Path, rel_root: Path) -> int:
⋮
│def build_frontend(skip_install: bool) -> None:
⋮
│def write_deploy_doc(staging: Path, name: str) -> None:
⋮
│def scan_staged(staging: Path) -> list[str]:
⋮
│def make_archive(staging: Path, out_root: Path, use_tar: bool) -> Path:
⋮
│def write_manifest(staging: Path, meta: dict) -> dict:
⋮
│def validate_compose(staging: Path) -> None:
⋮
│def main(argv: list[str] | None = None) -> int:
⋮

scripts/smoke_langfuse.py:
⋮
│async def main() -> int:
⋮

scripts/smoke_rerank.py:
⋮
│async def run(args: argparse.Namespace) -> int:
⋮
│def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
⋮

scripts/smoke_tools.py:
⋮
│def check(name: str, ok: bool, detail: str = "") -> None:
⋮
│async def check_url_guard() -> None:
⋮
│async def check_tools_surface() -> None:
⋮
│async def check_fetch_guard() -> None:
⋮
│def check_builders(keep: bool) -> None:
⋮
│def check_capability_routing() -> None:
⋮
│def check_masking_and_routes() -> None:
⋮
│def main(argv: list[str] | None = None) -> int:
⋮

scripts/stress_concurrency.py:
⋮
│async def one_turn(
│    client: httpx.AsyncClient, base: str, message: str, user_id: str, session_id: str
⋮
│async def probe_readiness(client: httpx.AsyncClient, base: str) -> dict[str, Any]:
⋮
│async def main() -> int:
⋮

scripts/test_concurrency_offline.py:
⋮
│def check(name: str, ok: bool, detail: str = "") -> None:
⋮
│async def read_all(buf: RunBuffer, from_id: int = 0, limit: int = 100000) -> list[int]:
⋮
│async def test_run_buffer_bounded() -> None:
⋮
│async def test_run_buffer_no_polling_scan() -> None:
│    print("\n2) RunBuffer: 空闲读者不再轮询扫描(靠 Event 唤醒)")
⋮
│    async def reader() -> None:
⋮
│async def test_run_buffer_many_readers() -> None:
│    print("\n3) RunBuffer: 多读者独立续流(断线重放)")
⋮
│    async def reader(name: str, from_id: int) -> None:
⋮
│async def test_hub_gate() -> None:
⋮
│async def test_overload_raises() -> None:
⋮
│async def test_audit_batched() -> None:
⋮
│async def test_audit_sync_fallback_after_close() -> None:
⋮
│async def test_masking_still_applied() -> None:
⋮
│async def main() -> int:
⋮
│async def test_fetch_url_streaming() -> None:
⋮

scripts/test_multi_task.py:
⋮
│def check(name: str, ok: bool, detail: str = "") -> None:
⋮
│def parse_frame(frame: str) -> dict | None:
⋮
│async def stream_chat(
│    client: httpx.AsyncClient,
│    message: str,
│    session_id: str,
│    *,
│    stop_after: int = 0,
⋮
│def stages(events: list[dict]) -> list[str]:
⋮
│def status_texts(events: list[dict], stage: str) -> list[str]:
⋮
│def sections(answer: str) -> list[str]:
⋮
│def section_bodies(answer: str) -> list[str]:
⋮
│def subtask_intervals(trace_id: str) -> list[dict]:
⋮
│def is_parallel_item(item: dict) -> bool:
⋮
│def overlaps(a: dict, b: dict) -> bool:
⋮
│async def main() -> int:
⋮

scripts/test_multi_task_offline.py:
⋮
│def check(name: str, ok: bool, detail: str = "") -> None:
⋮
│def test_clean_tasks() -> None:
⋮
│def test_looks_multi() -> None:
⋮
│def test_parallel_partition() -> None:
⋮
│def test_merge_answers() -> None:
⋮
│def test_build_response() -> None:
⋮
│def test_graph_wiring() -> None:
⋮
│def test_settings_defaults() -> None:
⋮
│def main() -> int:
⋮

scripts/test_sse_resume.py:
⋮
│def parse_frame(frame: str) -> tuple[int | None, dict | None]:
⋮
│async def main() -> None:
⋮

scripts/test_stream_hub.py:
⋮
│async def replay_test() -> None:
│    hub = get_stream_hub()
⋮
│    async def finisher() -> None:
⋮
│async def live_test() -> None:
│    hub = get_stream_hub()
⋮
│    async def reader() -> None:
⋮
│async def main_all() -> None:
⋮

scripts/test_tools_flow.py:
⋮
│def check(name: str, ok: bool, detail: str = "") -> None:
⋮
│def parse_frame(frame: str) -> dict | None:
⋮
│async def chat(client: httpx.AsyncClient, message: str, session_id: str, role: str = ROLE) -> dict:
⋮
│async def main() -> int:
⋮

web-ui/src/composables/useEmployee.js:
⋮
│function loadInitial() {
│  let saved = null
│  try {
│    saved = localStorage.getItem(STORAGE_KEY)
│  } catch {
│    /* localStorage 不可用时降级为默认值 */
│  }
│  return EMPLOYEES.find((e) => e.empId === saved) || EMPLOYEES.find((e) => e.empId === DEFAULT_EMP_
⋮
│export function useEmployee() {
│  function switchEmployee(empId) {
│    const emp = EMPLOYEES.find((e) => e.empId === empId)
│    if (!emp) return
│    Object.assign(state.current, emp)
│    try {
│      localStorage.setItem(STORAGE_KEY, emp.empId)
│    } catch {
│      /* 忽略存储失败 */
│    }
⋮

web-ui/src/router.js:
⋮
│const routes = [
│  { path: '/', name: 'chat', component: () => import('./views/ChatView.vue') },
│  { path: '/upload', name: 'upload', component: () => import('./views/UploadView.vue') },
│  { path: '/memory', name: 'memory', component: () => import('./views/MemoryView.vue') },
│  { path: '/graph', name: 'graph', component: () => import('./views/GraphView.vue') },
⋮

web-ui/vite.config.js:
⋮
│function resolveApiTarget() {
│  if (process.env.VITE_API_TARGET) return process.env.VITE_API_TARGET;
│  let port = "18000";
│  try {
│    const envFile = fileURLToPath(new URL("../.env", import.meta.url));
│    const hit = readFileSync(envFile, "utf8").match(/^ASSISTANT_PORT=\s*(\d+)\s*$/m);
│    if (hit) port = hit[1];
│  } catch {
│    /* .env 缺失(如容器内构建)则用默认端口, 不阻断前端启动 */
│  }
⋮
