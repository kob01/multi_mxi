
.dockerignore

.gitignore

.python-version

.qoder/repowiki/zh/content/RAG 知识底座.md

.qoder/repowiki/zh/content/项目总体架构.md

.qoder/repowiki/zh/meta/repowiki-metadata.json

CONFIG_RULES.md

LICENSE

README.md

app/__init__.py

app/agents/__init__.py

app/agents/common_tools.py:
⋮
│@tool
│def lookup_employee_by_name(name: str) -> dict[str, Any]:
⋮

app/agents/finance_agent/__init__.py

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
│    def __init__(self) -> None:
⋮
│    async def _ensure_agent(self, role: Role = Role.EMPLOYEE) -> Any:
⋮
│    async def invoke(self, user_text: str, user_id: str, role: Role) -> str:
⋮
│class FinanceAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge: A2A task -> FinanceAgent invocation."""
│
│    def __init__(self) -> None:
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮
│    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/finance_agent/server.py:
⋮
│def create_app():
⋮

app/agents/hr_agent/__init__.py

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
│    def __init__(self) -> None:
⋮
│    async def _ensure_agent(self, role: Role = Role.EMPLOYEE) -> Any:
⋮
│    async def invoke(self, user_text: str, user_id: str = "", role: Role = Role.EMPLOYEE) -> str:
⋮
│class HRAgentExecutor(AgentExecutor):
│    """A2A AgentExecutor bridge for HR_Agent."""
│
│    def __init__(self) -> None:
⋮
│    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮
│    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
⋮

app/agents/hr_agent/server.py:
⋮
│def create_app():
⋮

app/assistant/__init__.py

app/assistant/a2a_client.py:
⋮
│def _pin_card_url(card, base_url: str, domain: str):
⋮
│class A2AClientPool:
│    """Lazily-resolved A2A clients keyed by agent domain."""
│
│    def __init__(self) -> None:
⋮
│    async def _get_client(self, domain: str) -> A2AClient:
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
│def get_a2a_pool() -> A2AClientPool:
⋮

app/assistant/graph.py:
⋮
│def _platform_clock_text() -> str:
⋮
│class AssistantState(TypedDict):
⋮
│class AssistantOrchestrator:
│    """Single-entry Assistant that routes across KB / MCP / A2A layers."""
│
⋮
│    def __init__(self) -> None:
⋮
│    async def setup(self) -> None:
⋮
│    async def shutdown(self) -> None:
⋮
│    async def _build_checkpointer(self) -> BaseCheckpointSaver:
⋮
│    async def build_context(self, state: AssistantState) -> dict[str, Any]:
⋮
│    async def _safe_history_text(self, session_id: str) -> str:
⋮
│    async def _safe_personal_context(self, user_id: str, query: str) -> PersonalContext:
⋮
│    async def resolve_time(self, state: AssistantState) -> dict[str, Any]:
⋮
│    async def classify_intent(self, state: AssistantState) -> dict[str, Any]:
⋮
│    async def rewrite_query(self, state: AssistantState) -> dict[str, Any]:
│        """Condense a context-dependent follow-up into a standalone question.
│
│        Runs BEFORE intent classification so both the classifier and all four
│        downstream routes operate on the disambiguated question. Multi-turn
│        follow-ups like "那它的劣势呢" or "她是哪个部门来着" carry unresolved
│        pronouns; classifying/retrieving on the raw utterance fails.
│
│        Skipped (zero cost) when there is no history or the message is already
│        a long, self-contained question; falls back to the raw message on any
│        LLM failure or implausible output so downstream routes can never be
⋮
│        if not history.strip():
⋮
│        else:
│            prompt = QUERY_REWRITE_PROMPT.format(history=history, message=message)
│
│            async def _invoke() -> str:
⋮
│    @staticmethod
│    def _clean_rewrite(raw: str, fallback: str) -> str:
⋮
│    async def kb_retrieve(self, state: AssistantState) -> dict[str, Any]:
│        """One retrieval pass with rerank thresholding + ACL trim.
│
│        The query comes from ``kb_query`` (set by ``rewrite_query`` on the
│        first pass, or by ``kb_requery`` on a retry). Retrieval channels and
│        RRF fusion apply no cutoff; only the rerank stage drops chunks below
│        ``retrieval_score_threshold``, so an empty result here means "no
│        relevant document", not "nothing matched".
⋮
│        async def _invoke_retrieve() -> tuple[list[KnowledgeChunk], str]:
⋮
│    def _judge_retrieval(self, state: AssistantState) -> Literal["generate", "retry", "refuse"]:
⋮
│    async def kb_requery(self, state: AssistantState) -> dict[str, Any]:
⋮
│    @staticmethod
│    def _keyword_fallback(query: str) -> str:
⋮
│    async def kb_generate(self, state: AssistantState) -> dict[str, Any]:
⋮
│    async def tool_execute(self, state: AssistantState) -> dict[str, Any]:
⋮
│    async def agent_delegate(self, state: AssistantState) -> dict[str, Any]:
⋮
│    async def chitchat(self, state: AssistantState) -> dict[str, Any]:
│        prompt = DIRECT_PROMPT.format(current_time=self._now_text(state))
⋮
│        async def _invoke() -> str:
⋮
│    async def persist_memory(self, state: AssistantState) -> dict[str, Any]:
⋮
│    async def _write_personal_memory(
│        self,
│        state: AssistantState,
│        message: str,
│        answer: str,
│        session_summary: str = "",
⋮
│    @staticmethod
│    def _stream_enabled(state: AssistantState) -> bool:
⋮
│    async def _emit(self, state: AssistantState, event: dict[str, Any]) -> None:
⋮
│    async def _emit_status(self, state: AssistantState, stage: str, text: str) -> None:
⋮
│    async def _stream_answer(self, state: AssistantState, prompt: str) -> tuple[str, str]:
⋮
│    @staticmethod
│    def _role_of(state: AssistantState) -> Role:
⋮
│    @staticmethod
│    def _now_text(state: AssistantState) -> str:
⋮
│    @staticmethod
│    def _route_by_intent(state: AssistantState) -> str:
⋮
│    def _build_graph(self, checkpointer: BaseCheckpointSaver | None = None):
⋮
│    async def _get_retriever(self) -> HybridRetriever:
⋮
│    async def refresh_knowledge(self) -> None:
⋮
│    def ensure_graph_for_studio(self):
⋮
│    async def handle(self, req: ChatRequest) -> ChatResponse:
⋮
│    async def handle_stream(self, req: ChatRequest) -> str:
│        """流式入口: 后台任务跑图, 事件落 StreamHub, 立即返回 run_id。
│
│        HTTP 连接与图执行解耦: 客户端断开(刷新/断网)不影响 run 继续跑,
│        重连凭 run_id + Last-Event-ID 从断点重放并续流(app/assistant/stream.py)。
⋮
│        async def _pipeline() -> None:
⋮
│    async def _run_graph(
│        self, req: ChatRequest, trace_id: str, *, run_id: str, thinking: bool
⋮
│    def _build_response(
│        self, req: ChatRequest, final: AssistantState, trace_id: str
⋮
│def get_orchestrator() -> AssistantOrchestrator:
⋮
│def get_graph():
⋮

app/assistant/intent.py:
⋮
│def needs_current_time(message: str) -> bool:
⋮
│def _detect_target(message: str) -> str | None:
⋮
│def _l2_normalize(vec: list[float]) -> list[float]:
⋮
│def _dot(a: list[float], b: list[float]) -> float:
⋮
│class IntentRecognizer:
│    """Three-layer intent funnel: rule -> bge-m3 embedding -> LLM -> keyword."""
│
│    def __init__(self) -> None:
⋮
│    def _rule_classify(self, message: str) -> IntentResult | None:
⋮
│    async def _ensure_seeds(self) -> None:
⋮
│    def _semantic_classify_sync(self, query_vec: list[float]) -> IntentResult | None:
⋮
│    async def _semantic_classify(self, query: str) -> IntentResult | None:
⋮
│    def _parse_llm(self, content: str) -> IntentResult:
⋮
│    async def _llm_classify(self, message: str, history: str) -> IntentResult | None:
│        """LLM catch-all; returns None on failure so the keyword net engages.
│
│        接入 Prompt Cache: 意图分类是"同一段 prompt -> 同一个 JSON 结果"的纯
│        函数式调用, 不涉及权限/实时数据, 完全可以按 prompt 内容缓存。
⋮
│        async def _invoke() -> str:
⋮
│    def _fallback(self, message: str) -> IntentResult:
⋮
│    async def classify(self, message: str, history: str) -> IntentResult:
⋮

app/assistant/mcp_client.py:
⋮
│class MCPClientPool:
│    """Pool of MCP tool connections keyed by server name."""
│
│    def __init__(self) -> None:
⋮
│    async def get_tools(self, server: str | None = None) -> list[BaseTool]:
⋮
│    async def refresh(self) -> None:
⋮
│def get_mcp_pool() -> MCPClientPool:
⋮
│async def call_mcp_tool(server: str, tool_name: str, args: dict[str, Any]) -> Any:
⋮
│async def call_mcp_tool_text(server: str, tool_name: str, args: dict[str, Any]) -> str:
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
│    def __init__(self) -> None:
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
│    async def history_text(self, session_id: str) -> str:
⋮
│    async def append(self, session_id: str, user: str, assistant: str) -> str:
⋮
│def get_memory_store() -> MemoryStore:
⋮

app/assistant/prompts.py

app/assistant/router.py:
⋮
│def _stream_response(run_id: str, from_id: int) -> StreamingResponse:
│    """把一个 run 的事件缓冲区包装成 SSE 响应(重放 + 续读一体)。"""
⋮
│    async def frames() -> AsyncIterator[str]:
⋮
│@router.post("/chat", response_model=ChatResponse)
│async def chat(req: ChatRequest) -> ChatResponse:
⋮
│@router.post("/chat/stream")
│async def chat_stream(req: ChatRequest) -> StreamingResponse:
⋮
│@router.get("/chat/stream/{run_id}")
│async def chat_stream_resume(
│    run_id: str,
│    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
⋮
│@router.get("/sessions")
│async def list_sessions(user_id: str, limit: int = 50) -> list[dict]:
⋮
│@router.get("/sessions/{session_id}/messages")
│async def get_session_messages(session_id: str) -> list[dict]:
⋮
│@router.delete("/sessions/{session_id}")
│async def delete_session(session_id: str) -> dict[str, str]:
⋮
│@router.get("/health")
│async def health() -> dict[str, str]:
⋮

app/assistant/stream.py:
⋮
│def new_run_id() -> str:
⋮
│class RunBuffer:
│    """单个 run 的事件缓冲: 自增 id + 轮询读取, 支持多读者断点重放。
│
│    事件量小(token 粒度 × 并发 run 数), 读侧用 50ms 轮询而不是 Condition:
│    避免 wait_for(Condition.wait()) 超时取消时的重获锁边界问题。
⋮
│    def __init__(self) -> None:
⋮
│    async def append(self, event: dict[str, Any]) -> int:
⋮
│    async def mark_done(self) -> None:
⋮
│    async def iterate(self, from_id: int = 0) -> AsyncIterator[tuple[int, dict[str, Any]]]:
⋮
│class StreamHub:
│    """run_id -> RunBuffer 的进程内注册表(惰性清理过期缓冲区)。"""
│
│    def __init__(self) -> None:
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

app/cache/__init__.py

app/cache/prompt_cache.py:
⋮
│def _key(model: str, temperature: float, prompt: str) -> str:
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
│def _key(query: str, top_k: int, top_n: int, principal: Principal | None) -> str:
⋮
│async def cached_retrieve(
│    query: str,
│    top_k: int,
│    top_n: int,
│    principal: Principal | None,
│    invoke: Callable[[], Awaitable[tuple[list[KnowledgeChunk], str]]],
⋮
│async def invalidate_all() -> None:
│    """清空全部 Retrieval Cache (文档重新入库 / BM25 索引重建时调用)。"""
⋮
│    async def _do() -> None:
⋮

app/cache/tool_cache.py:
⋮
│def is_cacheable_tool_name(name: str) -> bool:
⋮
│def _key(server: str, tool_name: str, args: dict, role: str) -> str:
⋮
│async def cached_tool_call(
│    server: str,
│    tool_name: str,
│    args: dict,
│    role: str,
│    invoke: Callable[[], Awaitable[str]],
⋮
│def wrap_tools_for_cache(tools: list[BaseTool], server: str, role: str) -> list[BaseTool]:
⋮
│def _wrap_one(tool: BaseTool, server: str, role: str) -> BaseTool:
│    from app.assistant.mcp_client import flatten_mcp_result
│
│    async def _coro(**kwargs) -> str:
│        async def _invoke() -> str:
⋮

app/chat_store.py:
⋮
│class ChatStore:
│    """会话/消息的异步 DAO; 构造期不做任何 I/O。"""
│
│    def __init__(self) -> None:
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮
│    async def save_turn(
│        self,
│        *,
│        session_id: str,
│        user_id: str,
│        role: str,
│        department: str,
│        trace_id: str,
│        user_message: str,
│        answer: str,
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
│    服务名(tei-rerank/elasticsearch/mongo/...)灌进宿主机进程, 解析失败后静默降级
│    (见 CONFIG_RULES.md 第 5 条)。容器侧一律靠 docker-compose 的
│    ``env_file: docker/.env`` + ``environment:`` 注入真实环境变量。
⋮
│    @field_validator("pg_password", "deepseek_api_key", "langsmith_api_key", "mongo_password", mode
│    @classmethod
│    def _read_from_secret_file(cls, value: str, info) -> str:
⋮
│    @property
│    def base_dir(self) -> Path:
⋮
│@lru_cache
│def get_settings() -> Settings:
⋮

app/db/__init__.py

app/db/models.py:
⋮
│def _utcnow() -> datetime:
⋮
│class Base(DeclarativeBase):
⋮
│class Document(Base):
⋮
│class Tag(Base):
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
│class KnowledgeChunkRow(Base):
⋮
│class DocParentRow(Base):
⋮
│class DocChunkRow(Base):
⋮
│class LongTermMemoryRow(Base):
⋮
│class UserProfileRow(Base):
⋮
│class ChatSession(Base):
⋮
│class ChatMessage(Base):
⋮

app/db/schema_docs.py

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
│def _table_columns(sync_conn, table: str) -> set[str]:
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
│def execute_readonly_sql(sql: str, allowed_tables: set[str]) -> list[dict]:
⋮

app/docs/__init__.py

app/docs/parsers.py:
⋮
│@dataclass
│class ParsedBlock:
⋮
│def supported_extensions() -> set[str]:
⋮
│def modality_of(path: Path) -> str:
⋮
│def _parse_txt(path: Path) -> list[ParsedBlock]:
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
│def _parse_pptx(path: Path) -> list[ParsedBlock]:
⋮
│def _parse_xlsx(path: Path) -> list[ParsedBlock]:
⋮
│def _parse_subtitle(path: Path) -> list[ParsedBlock]:
⋮
│async def _mineru_parse(filename: str, data: bytes) -> str:
⋮
│async def _parse_image(path: Path) -> list[ParsedBlock]:
⋮
│async def parse_blocks(path: Path) -> tuple[str, list[ParsedBlock]]:
⋮

app/docs/router.py:
⋮
│class IngestRequest(BaseModel):
⋮
│class AclRequest(BaseModel):
⋮
│@router.post("/upload")
│async def upload_doc(file: UploadFile = File(...), uploader: str = Form("anonymous")) -> dict:
⋮
│@router.post("/ingest")
│async def ingest_doc(req: IngestRequest) -> dict:
⋮
│@router.put("/{doc_key}/acl")
│async def update_doc_acl(doc_key: str, req: AclRequest) -> dict:
⋮
│@router.delete("/{doc_key}")
│async def delete_doc(doc_key: str, operator: str = "anonymous") -> dict:
⋮
│@router.get("")
│async def list_docs() -> list[dict]:
⋮
│@router.get("/tags")
│async def list_all_tags() -> list[dict]:
⋮

app/docs/service.py:
⋮
│class UploadError(ValueError):
⋮
│def _upload_dir() -> Path:
⋮
│def _safe_filename(filename: str) -> str:
⋮
│def save_upload(filename: str, data: bytes) -> tuple[str, Path, str]:
⋮
│async def check_existing(doc_key: str) -> Document | None:
⋮
│async def _existing_tag_names() -> list[str]:
⋮
│async def suggest_tags(text: str) -> list[str]:
⋮
│def normalize_acl(
│    visibility: str, owner_id: str, dept_id: str, allowed_roles: list[str] | str
⋮
│async def _set_doc_status(doc_key: str, status: str) -> None:
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
│async def docs_not_ready(doc_ids: Sequence[str]) -> set[str]:
⋮
│async def _get_or_create_tag(session, name: str) -> int:
⋮
│async def get_meta_map(doc_keys: list[str]) -> dict[str, dict[str, Any]]:
⋮
│async def list_documents() -> list[dict[str, Any]]:
⋮
│async def list_tags() -> list[dict[str, Any]]:
⋮

app/kg/__init__.py

app/kg/extract.py:
⋮
│@dataclass
│class DocKG:
│    """一篇文档的结构化实体关系抽取结果, 两个字段都可能为空列表。"""
│
⋮
│    @property
│    def is_empty(self) -> bool:
⋮
│def _llm():
⋮
│def _parse(raw: str) -> DocKG:
⋮
│async def extract_doc_graph(title: str, tags: list[str], text: str) -> DocKG:
⋮

app/kg/router.py:
⋮
│def _principal(user_id: str, role: str, department: str) -> Principal:
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
│@router.post("/documents/{doc_key}/rebuild")
│async def rebuild_document(doc_key: str, operator: str = "anonymous") -> dict:
⋮
│@router.post("/rebuild-all")
│async def rebuild_all_docs(operator: str = "anonymous") -> dict:
⋮

app/kg/service.py:
⋮
│def _doc_chunk(doc: Document) -> KnowledgeChunk:
⋮
│async def _load_doc_with_tags(doc_key: str) -> tuple[Document | None, list[str]]:
⋮
│async def _load_doc_meta(doc_key: str) -> tuple[Any | None, list[str]]:
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
│def _eid(kind: str, key: str) -> str:
⋮
│async def query_subgraph(
│    allowed_doc_keys: Sequence[str],
│    focus: str | None = None,
│    hops: int | None = None,
│    limit: int | None = None,
⋮
│async def _focus_entities(session, focus: str, keys: list[str], hops: int, limit: int):
⋮

app/llm.py:
⋮
│def is_deepseek(model: str) -> bool:
⋮
│def _deepseek_extra_body(thinking: bool, settings) -> dict[str, Any]:
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
│def _ensure_app_logging() -> None:
⋮
│def _log_dependency_endpoints() -> None:
⋮
│@asynccontextmanager
│async def lifespan(app: FastAPI):
⋮
│def create_app() -> FastAPI:
│    """Build the gateway application."""
⋮
│    if index_file.is_file():
│        assets_dir = DIST_DIR / "assets"
⋮
│        @app.get("/{full_path:path}", include_in_schema=False)
│        async def spa(full_path: str) -> FileResponse:
⋮
│    else:
│
│        @app.get("/{full_path:path}", include_in_schema=False)
│        async def spa_dev(full_path: str) -> JSONResponse:
│            # 无构建产物 = dev 模式: 页面由 vite 提供, 本进程只做 API 与代理目标
│            return JSONResponse(
│                {
│                    "detail": "web/dist 无前端构建产物, 开发期请用 vite dev 模式",
│                    "dev_server": VITE_DEV_URL,
│                    "how_to": "cd web-ui && pnpm dev (代理目标读 VITE_API_TARGET, 默认本网关)",
│                    "api_docs": "/docs",
│                    "health": "/api/health",
⋮

app/mcp_servers/__init__.py

app/mcp_servers/finance_server.py:
⋮
│def _next_order_no(session: Session) -> str:
⋮
│def _order_dict(o: Reimbursement) -> dict[str, Any]:
⋮
│@mcp.tool()
│def create_reimbursement(user_id: str, title: str, amount: float, category: str, reason: str = "") 
⋮
│@mcp.tool()
│def query_reimbursement(order_no: str) -> dict[str, Any]:
⋮
│@mcp.tool()
│def list_reimbursements(user_id: str) -> list[dict[str, Any]]:
⋮
│@mcp.tool()
│def finance_budget_query(department: str) -> dict[str, Any]:
⋮
│@mcp.tool()
│def get_reimbursement_policy(category: str) -> dict[str, Any]:
⋮
│@mcp.tool()
│def execute_sql(sql: str) -> list[dict[str, Any]]:
⋮

app/mcp_servers/hr_server.py:
⋮
│def _next_ticket_no(session: Session) -> str:
⋮
│def _ticket_dict(t: HRTicket) -> dict[str, Any]:
⋮
│@mcp.tool()
│def create_hr_ticket(user_id: str, category: str, title: str, description: str) -> dict[str, Any]:
⋮
│@mcp.tool()
│def query_hr_ticket(ticket_no: str) -> dict[str, Any]:
⋮
│@mcp.tool()
│def list_hr_tickets(user_id: str) -> list[dict[str, Any]]:
⋮
│@mcp.tool()
│def cancel_hr_ticket(ticket_no: str) -> dict[str, Any]:
⋮
│@mcp.tool()
│def get_leave_balance(user_id: str) -> dict[str, Any]:
⋮
│@mcp.tool()
│def execute_sql(sql: str) -> list[dict[str, Any]]:
⋮

app/memory/__init__.py

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
│    """一轮对话的结构化个人记忆提取结果, 各字段都可能为空列表。
│
│    ``facts`` 是引入分桶前的遗留字段: 解析时并入 ``knowledge``, 老调用方仍可读。
⋮
│    @property
│    def is_empty(self) -> bool:
⋮
│def _llm():
⋮
│def _parse_profile(raw: object) -> list[dict]:
⋮
│def _parse_episodes(raw: object) -> list[EpisodeRecord]:
⋮
│def _parse_knowledge(raw: object) -> list[KnowledgeRecord]:
⋮
│def _parse(raw: str) -> MemoryExtraction:
⋮
│async def extract_memories(message: str, answer: str) -> MemoryExtraction:
⋮

app/memory/graph_store.py:
⋮
│def _auth() -> tuple[str, str] | None:
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
⋮
│async def entity_names(user_id: str, limit: int = 200) -> list[str]:
⋮
│async def user_subgraph(user_id: str, limit: int = 60) -> dict[str, list[dict]]:
⋮
│async def close_driver() -> None:
⋮

app/memory/personal.py:
⋮
│def _reflector():
⋮
│def _iso(value: datetime | None) -> str:
⋮
│def _item_dict(hit: MemoryHit) -> dict[str, Any]:
⋮
│@dataclass
│class PersonalContext:
│    """一轮对话召回的个人记忆; 空桶不产生 prompt 小节。"""
│
⋮
│    def as_prompt(self) -> str:
⋮
│    def audit(self) -> dict[str, Any]:
⋮
│    @property
│    def hit_ids(self) -> list[int]:
⋮
│class PersonalMemoryAgent:
│    """个人级记忆的读写编排; 无状态, 进程级单例。"""
│
⋮
│    async def build(self, user_id: str, query: str) -> PersonalContext:
⋮
│    async def _safe_profile(self, user_id: str) -> str:
⋮
│    async def _safe_stable_buckets(self, user_id: str) -> tuple[list[MemoryHit], list[MemoryHit]]:
⋮
│    async def _safe_vector_recall(self, user_id: str, query: str) -> list[MemoryHit]:
⋮
│    def _split_vector_hits(
│        self, hits: list[MemoryHit]
⋮
│    def _episode_fresh(self, hit: MemoryHit, now: datetime) -> bool:
⋮
│    async def _safe_graph(self, user_id: str, query: str) -> list[str]:
⋮
│    async def _safe_legacy_facts(self, user_id: str, query: str) -> list[MemoryHit]:
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
│    async def _upsert_bucket(
│        self,
│        store,
│        user_id: str,
│        session_id: str,
│        bucket: MemoryBucket,
│        items: Sequence[tuple],
│        *,
│        source: str,
⋮
│    async def add_session_episode(self, user_id: str, session_id: str, summary: str) -> bool:
⋮
│    async def reflect(self, user_id: str, *, force: bool = False) -> int:
⋮
│    async def overview(self, user_id: str) -> dict[str, Any]:
⋮
│    async def delete(self, user_id: str, item_id: int) -> bool:
⋮
│    async def clear(self, user_id: str, bucket: MemoryBucket | str) -> int:
⋮
│def get_personal_agent() -> PersonalMemoryAgent:
⋮

app/memory/profile_store.py:
⋮
│def _normalize_key(key: str) -> str:
⋮
│def _is_single_value(key: str) -> bool:
⋮
│def _clean(value: Any, max_len: int = 120) -> str:
⋮
│def merge_attributes(
│    old: dict[str, Any] | None, items: list[dict]
⋮
│def render_summary(attrs: dict[str, Any], max_chars: int | None = None) -> str:
⋮
│class UserProfileStore:
│    """画像的异步 DAO; 构造期不做任何 I/O, DB 不可用一律静默降级。"""
│
│    def __init__(self) -> None:
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

app/memory/router.py:
⋮
│def _require_self(user_id: str, operator: str) -> None:
⋮
│@router.get("/overview")
│async def memory_overview(user_id: str, operator: str = "") -> dict:
⋮
│@router.get("/graph")
│async def memory_graph(user_id: str, operator: str = "", limit: int = 60) -> dict:
⋮
│@router.delete("/items/{item_id}")
│async def delete_memory_item(item_id: int, user_id: str, operator: str = "") -> dict:
⋮
│@router.delete("/bucket/{bucket}")
│async def clear_memory_bucket(bucket: str, user_id: str, operator: str = "") -> dict:
⋮
│@router.post("/reflect")
│async def reflect_memories(user_id: str, operator: str = "") -> dict:
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
│@dataclass
│class MemoryHit:
⋮
│def _hit(
│    row: LongTermMemoryRow,
│    *,
│    score: float = 0.0,
│    title: str | None = None,
│    kind: str | None = None,
⋮
│class LongTermMemoryStore:
│    """跨会话个人记忆的向量存储与召回 (按 user_id 隔离)。"""
│
│    def __init__(self) -> None:
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
│    async def touch(self, memory_ids: Sequence[int]) -> None:
⋮
│    async def delete_items(self, user_id: str, memory_ids: Sequence[int]) -> int:
⋮
│    async def clear_bucket(self, user_id: str, kind: str) -> int:
⋮
│def get_long_term_store() -> LongTermMemoryStore:
⋮

app/rag/__init__.py

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
│    """BM25 lexical channel backed by Elasticsearch."""
│
│    def __init__(self, url: str | None = None, index: str | None = None) -> None:
⋮
│    async def ensure_index(self) -> None:
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

app/rag/embeddings.py:
⋮
│class OllamaEmbedder:
│    """Thin async client for Ollama's /api/embed endpoint.
│
│    bge-m3 produces 1024-dim dense vectors and natively supports
│    multilingual + long-context (8k) inputs, which fits enterprise
│    Chinese/English mixed documents.
⋮
│    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
⋮
│    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
⋮
│    async def embed_query(self, text: str) -> list[float]:
⋮

app/rag/ingest.py:
⋮
│def compute_doc_id(name: str, ext: str) -> str:
⋮
│def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
⋮
│def _locate(needle: str, haystack: str, cursor: int) -> tuple[int, int]:
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
│    def __init__(self, base_url: str | None = None) -> None:
⋮
│    @staticmethod
│    def _doc_text(chunk: KnowledgeChunk, max_chars: int) -> str:
⋮
│    async def rerank(
│        self, query: str, chunks: Sequence[KnowledgeChunk], top_n: int
⋮
│    async def health(self) -> bool:
⋮
│    async def probe(self) -> bool:
⋮
│def get_reranker() -> TeiReranker:
⋮

app/rag/retriever.py:
⋮
│def _rrf_fuse(
│    dense: Sequence[KnowledgeChunk], sparse: Sequence[KnowledgeChunk], top_k: int
⋮
│class HybridRetriever:
│    """Enterprise knowledge retriever used by the Assistant's KB path."""
│
│    def __init__(self) -> None:
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
│def build_sql_filter(principal: Principal | None) -> ColumnElement[bool] | None:
⋮
│def _chunks_from_narrow(row: Any, score: float) -> KnowledgeChunk:
⋮
│def _chunk_row(c: KnowledgeChunk, v: Sequence[float] | None) -> dict[str, Any]:
⋮
│class ChunkStore:
│    """子块持久化 + ANN 检索(doc_chunks)。构造零 I/O。"""
│
│    def __init__(self) -> None:
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
│    async def upsert_chunks(
│        self, chunks: Sequence[KnowledgeChunk], vectors: Sequence[Sequence[float] | None]
⋮
│    @staticmethod
│    def _row_with_vec(c: KnowledgeChunk, v: Sequence[float] | None) -> dict[str, Any]:
⋮
│    @staticmethod
│    async def _upsert_batch(
│        session: AsyncSession, dialect_insert, rows: list[dict], pk_col, upsert_cols
⋮
│    async def delete_by_doc(self, doc_id: str) -> int:
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
│    def __init__(self) -> None:
⋮
│    def _sessions(self) -> async_sessionmaker[AsyncSession]:
⋮
│    async def get_blocks(self, parent_ids: Sequence[str]) -> dict[str, ParentBlock]:
⋮
│    async def list_ids(self, doc_id: str | None = None) -> set[str]:
⋮
│    async def delete_stale(self, doc_id: str, keep_ids: set[str]) -> int:
⋮
│    async def delete_by_doc(self, doc_id: str) -> int:
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

app/security/__init__.py

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
│def new_trace_id() -> str:
⋮
│class AuditLogger:
│    """Append-only JSONL audit sink (fan-out to SIEM in production)."""
│
│    def __init__(self, path: str | None = None) -> None:
⋮
│    def log(
│        self,
│        trace_id: str,
│        actor: str,
│        action: str,
│        detail: dict[str, Any] | None = None,
│        session_id: str | None = None,
⋮
│def get_audit_logger() -> AuditLogger:
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
│def mask_text(text: str) -> str:
⋮
│def mask_sensitive(data: Any) -> Any:
⋮

app/tracing.py:
⋮
│def init_tracing() -> bool:
⋮

data/knowledge/SAJ技术滑行教程V1.0.pdf

data/knowledge/finance_policy.md

data/knowledge/hr_faq.md

data/knowledge/reimburse_training.srt

docker/Dockerfile

docker/docker-compose.yml

docker/init/01_vector.sql

docker/mineru/Dockerfile

langgraph.json

main.py:
│def main():
⋮

pyproject.toml

requirements.txt

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

scripts/ingest_knowledge.py:
⋮
│async def main() -> None:
⋮

scripts/init_db.py:
⋮
│async def _vector_version() -> str:
⋮
│async def main() -> None:
⋮

scripts/msmarco_eval/__init__.py

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
│def _pos_len(sample, did: str) -> int:
⋮
│def _qrels_arm(sample, meta: dict[str, dict]) -> dict[str, list[str]]:
⋮
│def context_efficiency(
│    rankings: dict[str, list[str]],
│    qrels: dict[str, list[str]],
│    corpus: dict[str, dict],
│    meta: dict[str, dict],
│    sample,
│    top_n: int,
⋮
│async def run_arm(
│    retriever,
│    arm: dict,
│    queries: list[dict],
│    *,
│    top_k: int,
│    top_n: int,
│    rerank: bool,
│    concurrency: int,
│    ks: list[int],
⋮
│def _headline_keys(ks: list[int]) -> list[str]:
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
│    """A self-contained MS MARCO retrieval benchmark sample.
│
│    Attributes:
│        queries: ordered list of ``{"query_id", "query"}``.
│        corpus:  ``docid -> {"title", "text"}`` for every passage that appears
│            as a positive OR a negative of the sampled queries (the pool the
│            retriever has to search).
│        qrels:   ``query_id -> [relevant docids]`` (the positives only).
│        meta:    provenance / build info (source, sizes, seed, ...).
⋮
│    @property
│    def n_queries(self) -> int:
⋮
│    @property
│    def n_corpus(self) -> int:
⋮
│    def summary(self) -> dict:
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
│async def _truncate_chunks(store: ChunkStore) -> None:
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
│) -> tuple[dict[str, list[str]], dict[str, Any]]:
│    """Run every query through the retriever; return rankings + timing.
│
│    Args:
│        threshold: rerank confidence cutoff; 0.0 keeps the full ranked list so
│            @k ranking metrics reflect ordering, not confidence filtering.
│        rerank: when False, run dense+sparse+RRF only (skips the cross-encoder).
│
│    Returns:
│        ``(rankings, stats)`` where ``rankings`` maps ``query_id -> ranked
│        doc_ids`` and ``stats`` carries latency + score-mode breakdown.
⋮
│    async def _timed_attach(seq):
⋮
│    async def one(item: dict) -> None:
⋮
│    def _p95(xs: list[float]) -> float:
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
│def _fmt(v: float) -> str:
⋮
│def render_markdown(report: dict) -> str:
⋮
│def write_reports(report: dict, out_dir: Path | None = None) -> tuple[Path, Path]:
⋮

scripts/seed_business_data.py:
⋮
│def _ts(dt: datetime) -> datetime:
⋮
│async def seed(force: bool) -> None:
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

uv.lock

web-ui/.npmrc

web-ui/index.html

web-ui/package.json

web-ui/pnpm-lock.yaml

web-ui/pnpm-workspace.yaml

web-ui/src/App.vue

web-ui/src/components/AppHeader.vue

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

web-ui/src/main.js

web-ui/src/mock/employees.js

web-ui/src/router.js:
⋮
│const routes = [
│  { path: '/', name: 'chat', component: () => import('./views/ChatView.vue') },
│  { path: '/upload', name: 'upload', component: () => import('./views/UploadView.vue') },
│  { path: '/memory', name: 'memory', component: () => import('./views/MemoryView.vue') },
│  { path: '/graph', name: 'graph', component: () => import('./views/GraphView.vue') },
⋮

web-ui/src/styles/global.css

web-ui/src/views/ChatView.vue

web-ui/src/views/GraphView.vue

web-ui/src/views/MemoryView.vue

web-ui/src/views/UploadView.vue

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
