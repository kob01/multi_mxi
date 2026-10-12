
app/agents/analyst_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/analyst_agent/executor.py:
⋮
│class AnalystAgent:
│    """LangGraph ReAct agent over analytics MCP tools, role-aware."""
│
⋮
│    async def invoke(
│        self, user_text: str, user_id: str, role: Role, trace_id: str = ""
⋮
│class AnalystAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge: A2A task -> AnalystAgent invocation."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/askdata_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/askdata_agent/executor.py:
⋮
│def format_payload(answer: str, payload: dict[str, Any] | None) -> str:
⋮
│class AskDataAgent:
│    """问数智能体本体(进程级单例; 状态机本身无状态, 每轮一个 state)。"""
│
⋮
│    async def invoke(
│        self,
│        user_text: str,
│        user_id: str,
│        role: Role,
│        trace_id: str = "",
│        session_id: str = "",
│        prior_slots: dict[str, Any] | None = None,
│        prior_dsl: dict[str, Any] | None = None,
│        prior_top_metric: str = "",
⋮
│class AskDataAgentExecutor(AgentExecutor):
│    """A2A ``AgentExecutor`` 桥: A2A task -> 一轮问数状态机。"""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/askdata_agent/graph.py:
⋮
│def extract_json(text: str) -> dict[str, Any] | None:
⋮
│class AskDataPipeline:
│    """一台跑在 askdata MCP server 之上的确定性问数流水线(按角色缓存图)。"""
│
⋮
│    def build(self):
⋮

app/agents/askdata_agent/prompts.py:
⋮
│def chart_hint(
│    column_count: int, has_period: bool, row_count: int,
│    *, has_compare: bool = False, metric_count: int = 1,
⋮

app/agents/common_tools.py:
⋮
│def take_lookup_tool(tools: list[Any]) -> Any | None:
⋮
│def with_lookup_tool(tools: list[Any], lookup: Any | None) -> list[Any]:
⋮

app/agents/contract_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/contract_agent/executor.py:
⋮
│class ContractAgent:
│    """合同初审 DAG 工作流 + 办理型 ReAct 回退, 角色感知。"""
│
⋮
│    async def invoke(self, user_text: str, user_id: str, role: Role, trace_id: str = "unknown") -> 
⋮
│class ContractAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge: A2A task -> ContractAgent invocation."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/contract_agent/prompts.py:
⋮
│SPECIALIST_CAPABILITIES = _MANAGER = MANAGER_CAPABILITIES
│
⋮

app/agents/finance_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/finance_agent/executor.py:
⋮
│class FinanceAgent:
│    """第三代 Agentic: plan-execute-reflect 闭环 + 横向 A2A 协同, 角色感知。"""
│
⋮
│    async def invoke(self, user_text: str, user_id: str, role: Role, trace_id: str = "unknown") -> 
⋮
│class FinanceAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge: A2A task -> FinanceAgent invocation."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/hr_agent/agent_card.py:
⋮
│def build_agent_card() -> AgentCard:
⋮

app/agents/hr_agent/executor.py:
⋮
│class HRAgent:
│    """LangGraph ReAct agent over HR MCP tools, role-aware."""
│
⋮
│    async def invoke(self, user_text: str, user_id: str = "", role: Role = Role.EMPLOYEE) -> str:
⋮
│class HRAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge for HR_Agent."""
│
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/analytics/charts.py:
⋮
│def _text_width(label: str) -> float:
⋮
│def _to_number(value: Any) -> float:
⋮
│def fmt(value: float, digits: int = 2) -> str:
⋮
│def normalize(
│    categories: Sequence[Any],
│    series: Sequence[Any] | None,
│    values: Sequence[Any] | None,
⋮
│def bar_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
⋮
│def line_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
⋮
│def pie_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
⋮
│def heatmap_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
⋮
│def _load_font(size: int):
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

app/analytics/reports.py:
⋮
│def current_cst_date() -> date:
⋮
│def build_markdown(metrics: dict[str, Any], label: str, charts: list[tuple[str, str]]) -> str:
⋮

app/analytics/store.py:
⋮
│def reports_dir() -> Path:
⋮
│def artifact_url(name: str) -> str:
⋮
│def is_safe_name(name: str) -> bool:
⋮
│def write_text(name: str, content: str, *, created_by: str = "", title: str = "", params: dict | No
⋮
│def write_bytes(
│    name: str, data: bytes, *, created_by: str = "", title: str = "", params: dict | None = None
⋮

app/askdata/attribution.py:
⋮
│def dsl_diff(was: Any, now: Any) -> dict[str, tuple[Any, Any]]:
⋮
│def _as_list(value: Any) -> list[Any]:
⋮

app/askdata/router.py:
⋮
│@router.get("/golden")
│async def list_golden(
│    role: str = Query(default="employee", description="调用者角色"),
│    operator: str = Query(default="", description="调用者工号"),
│    include_drafts: bool = Query(default=False, description="是否含未启用的草案"),
│    limit: int = Query(default=200, ge=1, le=2000),
⋮

app/askdata/store.py:
⋮
│def record_event(
│    *, query_id: str = "", trace_id: str = "", session_id: str = "", user_id: str = "",
│    event_type: str, payload: Any = None, stage: str = "", conn=None,
⋮
│def sample_stats() -> dict[str, int]:
⋮
│def record_eval_run(summary: dict[str, Any]) -> str:
⋮
│def latest_eval_run(mode: str = "") -> dict[str, Any]:
⋮
│def list_golden(*, enabled_only: bool = True, limit: int = 500) -> list[dict[str, Any]]:
⋮
│def upsert_golden(
│    *, question: str, dsl: Any = None, expected_sql: str = "", expected_result: Any = None,
│    tags: str = "", note: str = "", created_by: str = "", enabled: bool = True,
│    case_id: str = "", slots: Any = None, difficulty: str = "easy", intent: str = "",
│    semver: str = "", cacheable: bool = False,
⋮
│def golden_cacheable_count() -> int:
⋮

app/assistant/a2a_client.py:
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
│    async def _get_client(self, domain: str) -> A2AClient:
⋮
│    async def aclose(self) -> None:
⋮

app/assistant/askdata_bridge.py:
⋮
│def split_askdata_payload(text: str) -> tuple[str, dict[str, Any] | None]:
⋮
│def slots_of(payload: dict[str, Any]) -> dict[str, Any]:
⋮

app/assistant/graph.py:
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
│def get_mcp_pool() -> MCPClientPool:
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
│    def _lock_for(self, session_id: str) -> asyncio.Lock:
⋮
│    def _redis_or_none(self):
⋮
│    def _turns_key(self, session_id: str) -> str:
⋮
│    def _summary_key(self, session_id: str) -> str:
⋮
│    def _local_history_text(self, session_id: str) -> str:
⋮
│    async def _local_append(self, session_id: str, user: str, assistant: str) -> str:
⋮
│    async def append(self, session_id: str, user: str, assistant: str) -> str:
⋮
│    async def _compress_overflow(
│        self, redis, session_id: str, turns_key: str, summary_key: str
⋮

app/assistant/slot_store.py:
⋮
│class SlotStore:
│    """per-session 的"上一轮问数槽位"存储(Redis 优先, 进程内 LRU 降级)。"""
│
⋮
│    def _redis_or_none(self):
⋮
│    async def get(self, session_id: str) -> dict[str, Any]:
⋮
│    async def set(self, session_id: str, slots: dict[str, Any]) -> None:
⋮
│    async def clear(self, session_id: str) -> None:
⋮

app/assistant/stream.py:
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
│    def create(self, run_id: str) -> RunBuffer:
⋮
│    def get(self, run_id: str) -> RunBuffer | None:
⋮
│    async def append(self, run_id: str, event: dict[str, Any]) -> int | None:
⋮
│def get_stream_hub() -> StreamHub:
⋮

app/bodies/client.py:
⋮
│def mongo_database_url() -> str:
⋮
│def get_mongo_client() -> AsyncIOMotorClient:
⋮

app/bodies/store.py:
⋮
│def byte_size(text: str) -> int:
⋮
│def split_text(text: str, cap_bytes: int) -> list[str]:
⋮
│@dataclass
│class ParentTextItem:
⋮
│class BodyStore:
│    """正文外置存储门面: 进程级单例, 构造零 I/O(同 PgVectorStore 约定)。"""
│
⋮
│    async def save_parent_texts(self, items: Sequence[ParentTextItem]) -> int:
⋮
│    async def get_parent_texts(self, parent_ids: Sequence[str]) -> dict[str, str]:
⋮
│    async def count(self) -> dict[str, int]:
⋮
│def get_body_store() -> BodyStore:
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

app/cache/retrieval_cache.py:
⋮
│def acl_signature(principal: Principal | None) -> str:
⋮
│async def get_doc_gens(doc_keys: list[str]) -> dict[str, int]:
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

app/db/ast_guard.py:
⋮
│class ASTGuardError(ValueError):
⋮
│def _is_tautology(node: exp.Expression | None) -> bool:
⋮
│def _has_tautological_term(node: exp.Expression | None) -> bool:
⋮
│def validate_sql(
│    sql: str,
│    *,
│    allowed_tables: set[str],
│    mode: str = READ_MODE,
│    max_rows: int | None = None,
│    normalize: bool = True,
⋮
│def estimate_scan_rows(conn: Any, sql: str, params: dict | None = None) -> int | None:
⋮

app/db/datadsl.py:
⋮
│class DslError(ValueError):
⋮
│class FilterOp(str, Enum):
⋮
│def parse_plan(payload: dict | str) -> DataOpPlan:
⋮
│def predicate_sql(filt: Filter, placeholder: str) -> tuple[str, list[str], list[Any]]:
⋮

app/db/dataops.py:
⋮
│def decide_tier(est_rows: int) -> tuple[str, str]:
⋮
│def record_audit(
│    *,
│    trace_id: str,
│    scope: DataScope | None,
│    user_id: str,
│    role: str,
│    nl_question: str,
│    generated: str,
│    final_sql: str,
│    decision: str,
⋮
│def plan_data_op(
│    payload: dict | str,
│    *,
│    caller_user_id: str,
│    caller_role: str,
│    nl_question: str = "",
│    trace_id: str = "",
⋮
│def approval_allowed(approver_role: str, approver_user_id: str, actor_user_id: str) -> str | None:
⋮

app/db/models.py:
⋮
│class Document(Base):
⋮
│class Reimbursement(Base):
⋮
│class LongTermMemoryRow(Base):
⋮
│class ChatMessage(Base):
⋮
│class ReportArtifact(Base):
⋮

app/db/policy.py:
⋮
│class PolicyError(ValueError):
⋮
│def table_columns(table: str) -> frozenset[str]:
⋮
│def checked_ident(name: str) -> str:
⋮
│class WriteSpec:
⋮
│def spec_for(entity: str) -> WriteSpec:
⋮

app/db/rls.py:
⋮
│class RlsError(RuntimeError):
⋮
│def read_role() -> str | None:
⋮
│def write_role() -> str | None:
⋮
│async def ensure_db_principals() -> None:
⋮
│async def ensure_rls_policies() -> None:
⋮
│async def ensure_audit_immutability() -> None:
⋮
│async def rls_status() -> dict[str, object]:
⋮

app/db/scope.py:
⋮
│class ScopeError(ValueError):
⋮
│@dataclass(frozen=True)
│class DataScope:
│    """一次调用被授权触达的数据范围。"""
│
⋮
│    def dept_scope_text(self) -> str:
⋮
│    def session_settings(self) -> dict[str, str]:
⋮
│    def scope_params(self) -> dict[str, object]:
⋮
│    def scope_clause(self, alias: str = "") -> str:
⋮
│def all_dept_roles() -> frozenset[str]:
⋮
│def resolve_scope_sync(user_id: str, role: str) -> DataScope:
⋮
│async def ensure_data_scope_backfill() -> None:
⋮

app/db/sequences.py:
⋮
│def next_numbered(
│    session: Session,
│    *,
│    sequence: str,
│    prefix: str,
│    table: str,
│    column: str,
│    start: int,
⋮

app/db/session.py:
⋮
│def async_database_url() -> str:
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
│def get_sync_engine(db_role: str | None = None) -> Engine:
⋮
│@contextmanager
│def scoped_connection(
│    db_role: str | None = None,
│    session_settings: dict[str, str] | None = None,
│    timeout_ms: int | None = None,
⋮
│def execute_readonly_sql(
│    sql: str,
│    allowed_tables: set[str],
│    params: dict | None = None,
│    *,
│    db_role: str | None = None,
│    session_settings: dict[str, str] | None = None,
⋮
│class ScanCostExceeded(ValueError):
⋮
│def execute_scoped_select(
│    sql: str,
│    params: dict | None = None,
│    *,
│    db_role: str | None = None,
│    session_settings: dict[str, str] | None = None,
│    max_scan_rows: int | None = None,
│    timeout_ms: int | None = None,
⋮

app/docgen/docx_builder.py:
⋮
│def _add_images(doc: Any, images: list[dict[str, Any]]) -> None:
⋮

app/docgen/genstore.py:
⋮
│def new_token() -> str:
⋮
│def gen_root() -> Path:
⋮
│def new_file_name(ext: str, title: str = "") -> str:
⋮
│def build_path(token: str, file_name: str) -> Path | None:
⋮
│def parse_spec(raw: Any) -> tuple[dict[str, Any] | None, str]:
⋮

app/docgen/images.py:
⋮
│async def resolve_images(spec_images: Any, dest_dir: Path) -> tuple[list[dict[str, Any]], list[str]
⋮

app/docgen/pptx_builder.py:
⋮
│def _add_images(prs: Any, images: list[dict[str, Any]]) -> None:
⋮

app/docgen/xlsx_builder.py:
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
│@dataclass
│class ParsedBlock:
⋮
│def modality_of(path: Path) -> str:
⋮
│def _parse_md(path: Path) -> list[ParsedBlock]:
│    """Split markdown by headings; section = current heading path."""
⋮
│    def flush() -> None:
⋮
│def _parse_docx(path: Path) -> list[ParsedBlock]:
│    """Split docx by Heading styles (supports EN/CN style names)."""
⋮
│    def flush() -> None:
⋮

app/docs/service.py:
⋮
│class UploadError(ValueError):
⋮
│async def check_existing(doc_key: str) -> Document | None:
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

app/kg/vocab.py:
⋮
│def normalize_entity_name(raw: object) -> str:
⋮
│def normalize_entity_type(raw: object) -> str:
⋮
│def normalize_relation(raw: object, *, enabled: bool = True) -> tuple[str, bool]:
⋮
│def entity_type_hint() -> str:
⋮

app/llm.py:
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

app/mcp_servers/analytics_server.py:
⋮
│@mcp.tool()
│def plan_data_op(
│    plan: dict[str, Any] | str,
│    caller_user_id: str = "",
│    caller_role: str = "",
│    caller_intent_text: str = "",
│    trace_id: str = "",
⋮

app/memory/graph_store.py:
⋮
│def get_driver() -> AsyncDriver | None:
⋮
│async def ensure_schema() -> None:
⋮
│async def upsert_entities(
│    user_id: str,
│    entities: Sequence[dict],
│    relations: Sequence[dict],
│    *,
│    source: str = "turn",
│    user_aliases: Iterable[str] = (),
⋮
│async def user_subgraph(user_id: str, limit: int = 60) -> dict[str, list[dict]]:
⋮

app/memory/graph_vocab.py:
⋮
│def clean_name(raw: object) -> str:
⋮
│def fold_self(name: str) -> str:
⋮
│def looks_like_artifact(raw: object) -> bool:
⋮
│def entity_type(raw: object) -> str | None:
⋮
│def entity_type_hint() -> str:
⋮
│def type_label(kind: str) -> str:
⋮
│def canonical_relation(raw: object) -> str | None:
⋮
│@dataclass(frozen=True)
│class GraphPlan:
│    """语义过滤的中间结果(还没做"是否锚定在用户身上"的连通性判定)。"""
│
⋮
│    def endpoint_names(self) -> set[str]:
⋮
│def plan(
│    entities: Sequence[dict],
│    relations: Sequence[dict],
│    *,
│    user_aliases: Iterable[str] = (),
│) -> GraphPlan:
│    """把 LLM 给的 entities/relations 过一遍语义口径(纯函数, 不碰图)。
│
│    ``user_aliases`` 是该用户已知的自称(画像里的姓名、工号), 用于把这些端点折叠成
│    "我" —— 折叠后"朱斌-毕业于-X"与"我-毕业于-X"才会并成同一条边。
⋮
│    def reject(reason: str) -> None:
⋮
│    def blocked_endpoint(name: str) -> bool:
⋮

app/memory/personal.py:
⋮
│@dataclass
│class PersonalContext:
⋮
│class PersonalMemoryAgent:
│    """个人级记忆的读写编排; 无状态, 进程级单例。"""
│
⋮
│    async def build(self, user_id: str, query: str) -> PersonalContext:
⋮
│    async def write(
│        self,
│        user_id: str,
│        session_id: str,
│        extraction: MemoryExtraction,
│        *,
│        source: str = SOURCE_TURN,
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
│    async def clear(self, user_id: str) -> bool:
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
│def is_conversation_product(*texts: object) -> bool:
⋮
│def spec_of(bucket: MemoryBucket | str) -> BucketSpec:
⋮
│def label_of(kind: str) -> str:
⋮

app/memory/temporal.py:
⋮
│def _build(y: int, mm: int, dd: int) -> datetime | None:
⋮
│def _from_digits(text: str) -> datetime | None:
⋮
│def parse_event_time(raw: object) -> datetime | None:
⋮
│def parse_embedded_date(text: str) -> tuple[str, datetime | None]:
⋮
│@dataclass(frozen=True)
│class TimedValue:
│    """一个带两轴时间的属性值: 现实何时成立(``valid_at``) + 系统何时听到(``recorded_at``)。
│
│    ``explicit`` 标记时间是不是用户明说的: 只有明说的才会在画像摘要里渲染成
│    "（自 2015-09）" —— 未明说的按"当下生效"处理, 给每个值都挂上记录时间既噪声也大
│    地挤占 ``profile_max_chars`` 预算。
⋮
│    def to_dict(self) -> dict[str, Any]:
⋮
│    @classmethod
│    def from_dict(cls, raw: Any, *, fallback_time: datetime | None = None) -> "TimedValue | None":
⋮
│def as_datetime(raw: Any) -> datetime | None:
⋮
│def is_historical(item: TimedValue, *, now: datetime, grace_days: int) -> bool:
⋮
│def history_rank(item: TimedValue, *, now: datetime, grace_days: int) -> tuple[int, float]:
⋮
│def utcnow() -> datetime:
⋮
│def format_month(stamp: datetime | None) -> str:
⋮
│def today_cst() -> datetime:
⋮

app/memory/vector_store.py:
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
│    async def list_recent(
│        self,
│        user_id: str,
│        kinds: Sequence[str],
│        limit: int = 5,
│        *,
│        order_by: str = "last_accessed_at",
⋮
│    async def update_memory_content(
│        self, user_id: str, memory_id: int, content: str, title: str = ""
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

app/rag/bm25.py:
⋮
│def tokenize(text: str) -> list[str]:
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
│    async def rebuild(self, chunks_or_iter) -> None:
⋮
│    async def index_chunks(self, chunks: Sequence[KnowledgeChunk], refresh: bool = True) -> None:
⋮
│    async def count(self) -> int:
⋮
│    async def search(
│        self, query: str, top_k: int, principal: Principal | None = None
⋮
│def get_es_bm25() -> ElasticBM25Retriever:
⋮

app/rag/embeddings.py:
⋮
│def get_embedder_client() -> httpx.AsyncClient:
⋮
│class OllamaEmbedder:
│    """Thin async client for Ollama's /api/embed endpoint.
│
│    bge-m3 produces 1024-dim dense vectors and natively supports
│    multilingual + long-context (8k) inputs, which fits enterprise
│    Chinese/English mixed documents.
⋮
│    async def embed_query(self, text: str) -> list[float]:
⋮

app/rag/ingest.py:
⋮
│def compute_doc_id(name: str, ext: str) -> str:
⋮

app/rag/reranker.py:
⋮
│def _get_client() -> httpx.AsyncClient:
⋮
│async def close_reranker_client() -> None:
⋮
│class TeiReranker:
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
│    async def assemble_parents(self, chunks: Sequence[KnowledgeChunk]) -> list[KnowledgeChunk]:
⋮
│    def format_context(
│        self,
│        chunks: Sequence[KnowledgeChunk],
│        meta_map: dict[str, dict[str, Any]] | None = None,
⋮

app/rag/vectorstore.py:
⋮
│async def upsert_rows(
│    session: AsyncSession, dialect_insert, rows: list[dict], pk_col, upsert_cols
⋮
│async def publish_in_session(
│    session: AsyncSession,
│    doc_id: str,
│    parent_rows: Sequence[ParentBlock],
│    chunks: Sequence[KnowledgeChunk],
│    vectors: Sequence[Sequence[float] | None],
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
│    async def iter_chunk_corpus(
│        self, page_size: int | None = None
⋮
│    async def publish_and_mark(
│        self,
│        doc_id: str,
│        parent_rows: Sequence[ParentBlock],
│        chunks: Sequence[KnowledgeChunk],
│        vectors: Sequence[Sequence[float] | None],
│        session: AsyncSession | None = None,
⋮
│    async def count(self) -> int:
⋮
│class ParentStore:
│    """父块持久化(doc_parents): 结构定位, 正文在 Mongo。构造零 I/O。"""
│
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮

app/schemas.py:
⋮
│class Role(str, Enum):
⋮
│class IntentResult(BaseModel):
⋮
│class ChatRequest(BaseModel):
│    """Inbound chat request from the Web UI / API clients."""
│
⋮
│    @field_validator("agent_targets")
│    @classmethod
│    def _normalize_agent_targets(cls, v: list[str]) -> list[str]:
⋮
│class KnowledgeChunk(BaseModel):
⋮
│class ParentBlock(BaseModel):
⋮

app/security/acl.py:
⋮
│@dataclass(frozen=True)
│class Principal:
⋮
│def parse_allowed_roles(stored: str) -> list[str]:
⋮
│def is_allowed_fields(
│    visibility: str,
│    owner_id: str,
│    dept_id: str,
│    allowed_roles: str,
│    principal: Principal,
⋮
│def can_manage_document(principal: Principal, created_by: str) -> bool:
⋮

app/security/audit.py:
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
│    def _flush_buf(self, buf: list[str]) -> None:
⋮
│    def flush(self, timeout: float = 2.0) -> bool:
⋮
│    def close(self, timeout: float = 2.0) -> None:
⋮
│def get_audit_logger() -> AuditLogger:
⋮

app/security/auth.py:
⋮
│def dataops_writable_roles() -> set[str]:
⋮
│def analytics_whitelist(role: Role) -> set[str]:
⋮
│def askdata_whitelist(role: Role) -> set[str]:
⋮
│def capability_allowed(capability: str) -> set[str]:
⋮
│def filter_tools_for_role(role: Role, server_name: str, tools: list[Any]) -> list[Any]:
⋮
│class PermissionDenied(Exception):
⋮

app/security/caller.py:
⋮
│@dataclass(frozen=True)
│class Caller:
⋮
│def set_caller(caller: Caller | None) -> Token:
⋮
│def reset_caller(token: Token) -> None:
⋮
│def current_caller() -> Caller | None:
⋮
│def caller_tool_args() -> dict[str, str]:
⋮
│def bind_caller(tool):
⋮
│def bind_caller_tools(tools: list) -> list:
⋮
│def resolve_caller(
│    user_id: str = "",
│    role: str = "",
│    department: str = "",
│    intent_text: str = "",
│    trace_id: str = "",
⋮
│def forbidden(reason: str, **extra: object) -> dict[str, object]:
⋮
│def guard_target_user(
│    caller: Caller | None,
│    target_user_id: str,
│    *,
│    what: str = "该员工的记录",
⋮
│def guard_owner(
│    caller: Caller | None,
│    owner_id: str,
│    *,
│    what: str = "该单据",
⋮

app/security/masking.py:
⋮
│def mask_text(text: str) -> str:
⋮
│def mask_sensitive(data: Any) -> Any:
⋮
│def mask_round_trip(
│    text: str,
│    *,
│    extra_patterns: Sequence[tuple[str, re.Pattern[str]]] | None = None,
⋮
│def dlp_columns() -> set[str]:
⋮

app/security/quota.py:
⋮
│def resolve_daily_limit() -> int:
⋮

app/security/spotlight.py:
⋮
│def is_suspicious(text: str) -> bool:
⋮
│def scan_rows(rows: list[dict[str, Any]], *, max_cells: int = 10) -> list[str]:
⋮
│def mark_payload(payload: Any, *, scope_note: str = "") -> Any:
⋮

app/security/url_guard.py:
⋮
│class UrlBlocked(ValueError):
⋮
│async def resolve_and_validate(url: str) -> list[str]:
⋮

app/semantic/catalog.py:
⋮
│def fingerprint(parts: list[str]) -> str:
⋮
│@dataclass(frozen=True)
│class DimCompat:
⋮
│@dataclass(frozen=True)
│class TimeSemantic:
⋮
│@dataclass(frozen=True)
│class RowPolicy:
⋮
│@dataclass(frozen=True)
│class ColPolicy:
⋮
│@dataclass(frozen=True)
│class Metric:
│    """一个指标定义(与 ``sem_metrics`` 表列一一对应)。"""
│
⋮
│    def caliber_note(self) -> str:
⋮
│@dataclass(frozen=True)
│class Dimension:
│    """一个维度定义(维度身份 = 语义概念 + 落在哪张表, 所以"部门"会有多行)。"""
│
⋮
│    def sample_values(self, limit: int = 6) -> str:
⋮
│@dataclass
│class MetricCatalog:
│    """一次语义层快照(进程级缓存的单位; 构建成本 = 几张元数据表的几条 SELECT)。"""
│
⋮
│    def compat_for(self, metric_id: str, dim_id: str) -> DimCompat | None:
⋮
│    def compatible_dim_ids(self, metric: Metric) -> frozenset[str]:
⋮
│    def denied_columns_for(self, role: str, table: str) -> frozenset[str]:
⋮
│    def get_metric(self, metric_id: str) -> Metric | None:
⋮
│    def domains(self) -> list[str]:
⋮
│    def metrics_in_domain(self, domain: str) -> list[Metric]:
⋮
│    def render_metrics(self, domain: str = "all", limit: int = 80) -> str:
⋮
│    def render_dimensions(self, limit: int = 60) -> str:
⋮

app/semantic/compiler.py:
⋮
│class CompileError(ValueError):
⋮
│@dataclass
│class Compiled:
⋮
│def compile_analytical(
│    spec: QuerySpec,
│    *,
│    catalog: MetricCatalog,
│    scope: Any,
│    check: ValidationResult | None = None,
│    allowed_tables: set[str] | None = None,
│    max_rows: int = 100,
⋮
│def transpile_sql(sql: str, dialect: str) -> str:
⋮

app/semantic/dsl.py:
⋮
│class DslError(ValueError):
⋮
│class SpecTime(BaseModel):
⋮
│def parse_dsl(payload: dict | str) -> QuerySpec:
⋮

app/semantic/link.py:
⋮
│@dataclass(frozen=True)
│class Candidate:
│    """一条链接候选(指标/维度/枚举归属)。"""
│
⋮
│    def to_dict(self) -> dict[str, Any]:
⋮
│def _build(kind: str, target_id: str, catalog: MetricCatalog, *, score: float, channel: str,
⋮

app/semantic/registry.py:
⋮
│def get_catalog(force_refresh: bool = False) -> MetricCatalog:
⋮
│def load_definitions():
⋮
│async def ensure_seeded() -> dict[str, int]:
⋮
│def table_obj(name: str):
⋮

app/semantic/seed.py:
⋮
│def build_catalog(origin: str = "seed", built_at: float = 0.0) -> MetricCatalog:
⋮

app/semantic/timesem.py:
⋮
│class TimeError(ValueError):
⋮
│@dataclass(frozen=True)
│class ResolvedRange:
⋮
│def today_cst() -> date:
⋮
│def detect_phrase(text: str) -> str:
⋮
│def normalize_range(raw: str) -> str:
⋮
│def _month_start(d: date) -> date:
⋮
│def _quarter_start(d: date) -> date:
⋮
│def _add_months(d: date, months: int) -> date:
⋮
│def _add_years(d: date, years: int) -> date:
⋮
│def resolve(
│    time: SpecTime | dict | None,
│    *,
│    compare: str = "none",
│    today: date | None = None,
│    default_gran: str = "none",
│    max_span_days: int | None = None,
⋮
│def _with_compare(r: ResolvedRange, compare: str) -> ResolvedRange:
⋮

app/semantic/validate.py:
⋮
│@dataclass(frozen=True)
│class Issue:
│    """一条校验结论: 错误码 + 可读原因 + 出问题的字段。"""
│
⋮
│    def to_dict(self) -> dict[str, str]:
⋮
│@dataclass(frozen=True)
│class CallerScope:
│    """校验需要的最小作用域信息(从 ``DataScope`` 投影, 避免本模块依赖 DB 层)。"""
│
⋮
│    @classmethod
│    def from_scope(cls, scope: Any) -> "CallerScope":
⋮
│@dataclass
│class ValidationResult:
⋮

app/tools/_http.py:
⋮
│def get_web_client() -> httpx.AsyncClient:
⋮
│def get_search_client() -> httpx.AsyncClient:
⋮

app/tracing.py:
⋮
│def init_langfuse() -> bool:
⋮

scripts/askdata_eval/__main__.py:
⋮
│def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
⋮
│def load_cases_for(args: argparse.Namespace) -> list[dict[str, Any]]:
⋮

scripts/askdata_eval/cases.py:
⋮
│def load_cases(*, limit: int | None = None, tag: str = "") -> list[dict[str, Any]]:
⋮

scripts/askdata_eval/judge.py:
⋮
│def compare_columns(expected: list[str], actual: list[str]) -> tuple[bool, str]:
⋮
│def compare_rows(expected: list[dict[str, Any]], actual: list[dict[str, Any]],
⋮
│def judge_with_llm(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
⋮
│def dsl_exact_match(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[bool, str]:
⋮
│def mis_clarified(case: dict[str, Any], actual: dict[str, Any]) -> bool:
⋮

scripts/askdata_eval/report.py:
⋮
│def reports_dir(base: Path | None = None) -> Path:
⋮
│def render_markdown(summary: dict[str, Any]) -> str:
⋮
│def write_reports(summary: dict[str, Any], base: Path | None = None) -> tuple[Path, Path]:
⋮

scripts/askdata_eval/runner.py:
⋮
│def eval_scope() -> DataScope:
⋮
│async def run_agent(case: dict[str, Any], *, agent: Any = None) -> dict[str, Any]:
⋮
│async def probe_agent_mode() -> str:
⋮

scripts/clean_personal_graph.py:
⋮
│def should_drop_node(name: str, etype: str) -> str:
⋮

scripts/demo_reimburse.py:
⋮
│async def turn(client: httpx.AsyncClient, session: str, message: str) -> dict:
⋮

scripts/gen_arch_xmind.py:
⋮
│def add_node(parent: ET.Element, text: str, children=None, color=None, folded=False):
⋮

scripts/gen_repo_map.py:
⋮
│def collect_fnames(root: Path, io: InputOutput, all_files: bool) -> list[str]:
⋮

scripts/msmarco_eval/__main__.py:
⋮
│def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
│def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
│def dcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
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

scripts/smoke_rerank.py:
⋮
│def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
⋮

scripts/stress_concurrency.py:
⋮
│async def one_turn(
│    client: httpx.AsyncClient, base: str, message: str, user_id: str, session_id: str
⋮
│async def probe_readiness(client: httpx.AsyncClient, base: str) -> dict[str, Any]:
⋮

scripts/test_askdata.py:
⋮
│def gateway_base() -> str:
⋮
│def chat(text: str, *, targets: list[str] | None = None, session: str = "", user: str = USER) -> di
⋮
│def askdata_of(data: dict) -> dict:
⋮

scripts/test_askdata_offline.py:
⋮
│class _CatalogWith:
│    """测试替身: 只换 metrics, 其他查表逻辑走真目录。"""
│
⋮
│    def get_metric(self, metric_id: str):
⋮
│def test_compiler() -> None:
│    print("\n4) 编译器: 作用域注入 / 绑参 / AST 自校验 / 同环比形状")
⋮
│    def compile(raw: dict, *, mode: str = "analytical") -> compiler.Compiled:
⋮
│def test_clarify_gate() -> None:
│    print("\n7) 澄清门与开关")
⋮
│    async def run_gate(slots: dict, candidates: list[dict]) -> dict:
⋮

scripts/test_ast_guard_offline.py:
⋮
│def denied(name: str, sql: str, mode: str = READ_MODE) -> None:
⋮

scripts/test_contract_workflow_offline.py:
⋮
│def test_agent_card() -> None:
⋮
│def test_prompts_format() -> None:
⋮
│def test_config_fields() -> None:
⋮

scripts/test_dataops_dsl_offline.py:
⋮
│def plan_rejected(name: str, payload: dict) -> None:
⋮

scripts/test_finance_agentic_offline.py:
⋮
│def test_agent_card() -> None:
⋮
│def test_prompts_format() -> None:
⋮
│def test_config_fields() -> None:
⋮

scripts/test_kg_edges_offline.py:
⋮
│def test_entity_identity() -> None:
⋮
│def test_name_normalize() -> None:
⋮
│def test_relation_synonym() -> None:
⋮
│def test_relation_inverse() -> None:
⋮
│def test_self_relation_dropped() -> None:
⋮
│def test_uncontrolled_word_fallback() -> None:
⋮
│def test_relation_cap() -> None:
⋮
│def test_vocab_switch_off() -> None:
⋮
│def test_migration_grouping() -> None:
⋮

scripts/test_multi_agent.py:
⋮
│def parse_frame(frame: str) -> dict | None:
⋮
│async def stream_chat(
│    client: httpx.AsyncClient,
│    message: str,
│    session_id: str,
│    *,
│    targets: list[str] | None = None,
│    role: str = ROLE,
│    user: str = USER,
│    stop_after: int = 0,
⋮
│def status_texts(events: list[dict], stage: str) -> list[str]:
⋮
│def agent_intervals(trace_id: str, expect: int = 0, wait_s: float | None = None) -> list[dict]:
⋮
│async def get_agents(client: httpx.AsyncClient, role: str) -> list[dict]:
⋮
│def domains_of(items: list[dict]) -> set[str]:
⋮

scripts/test_sse_resume.py:
⋮
│def parse_frame(frame: str) -> tuple[int | None, dict | None]:
⋮

scripts/test_stream_hub.py:
⋮
│async def replay_test() -> None:
⋮
│async def live_test() -> None:
⋮
│async def main_all() -> None:
⋮

scripts/test_tools_flow.py:
⋮
│def parse_frame(frame: str) -> dict | None:
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

web-ui/src/router.js:
⋮
│const routes = [
│  { path: "/", name: "chat", component: () => import("./views/ChatView.vue") },
│  {
│    path: "/upload",
│    name: "upload",
│    component: () => import("./views/UploadView.vue"),
│  },
│  {
│    path: "/memory",
│    name: "memory",
│    component: () => import("./views/MemoryView.vue"),
│  },
│  {
⋮
│    component: () => import("./views/GraphView.vue"),
│  },
│  {
⋮
│    component: () => import("./views/DataOpsView.vue"),
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
