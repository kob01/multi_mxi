"""Assistant orchestration graph (LangGraph).

Routing policy (single-entry multi-agent):
    user -> [build_context] -> [resolve_time] -> [rewrite_query]
         -> [classify_intent] -> one of:
        knowledge_qa    -> kb_retrieve -> judge -> kb_generate   (confident hits)
                                            |-> kb_requery -> kb_retrieve (no-result retry)
                                            `-> refuse  (still empty -> fixed reply, no LLM)
        chitchat        -> chitchat
        tool_call       -> tool_execute   (Assistant -> MCP business tools / 能力域进程内工具)
        agent_delegate  -> agent_delegate (Assistant -> A2A specialist)
    或(调用方显式点选智能体时) -> multi_agent_execute (同一问题并发委派 N 个智能体 + 分节合并)
    -> [persist_memory] -> END

多智能体并发委派(multi_agent_execute): 复合问法的 LLM 自动拆分已下线 —— 想拆得准要靠
不断加提示词, 控住误拆的成本与得到的收益不成比例。改由调用方(ChatRequest.agent_targets,
前端多选)显式指定本轮要问哪几个专业智能体: 同一个(已消解指代的)问题原样下发给每个智能体,
并发跑、各自成节、程序化拼接。四处刻意的设计:
- 触发口径零推断: 只有点选了智能体才走这条路, 未点选与改动前行为完全一致; 这一轮也不再
  跑意图漏斗(分派给谁已由用户决定, 省一次分类开销);
- 委派走同一份 helper(:meth:`_delegate_task`): 可信身份 metadata / Agent Card 地址覆盖 /
  角色闸门 / 审计都与单智能体委派路径同源, 不会漂出第二套委派语义;
- 并发上限与逐位隔离: Semaphore(multi_agent_parallelism) + 每个智能体 asyncio.wait_for 上限
  + 异常隔离, 一个智能体挂了/超时/无权/域键不合法都只降级它自己那一节, 不连坐整轮;
- 合并不调 LLM 且不逐 token 流式: 各节内容已是智能体产出的事实, 再过一次 LLM 只会引入
  改写与编造风险; N 路 token 流交错会糊在一段文本里, 故整篇按点选顺序一次性下发。
本轮仍产出一个合成的 IntentResult(AGENT_DELEGATE + 首个智能体 + layer=explicit), 目的是让
落库/审计/前端路由标签的字段形状与单意图路径一致, 不是为了再养一套分类口径。
四个单意图路由节点(kb_retrieve/tool_execute/agent_delegate/chitchat)只是薄壳, 与并发分支
共用同一套 helper 实现。

build_context 是架构图里 "Business Context" 的汇聚点: 并行拉
Session Memory(Redis) + 个人级记忆各桶(User Memory 的 profile/preference/habit、
Episodic、Personal Knowledge、Personal Graph), 拼成下游共享的 history/memory_ctx;
各桶各自单独降级, 任一通道不可用只影响拼接内容, 不阻断对话; 降级事实会合并成
一条 context_degraded 审计留痕。写路径上
persist_memory 把一轮对话的一次提取分桶落盘, 会话摘要溢出时还会往
Episodic Memory 沉一条情节。注: 架构图里的 Project Memory 本轮未实现。

Working State 通过 LangGraph checkpointer 持久化(Redis 可用时 AsyncRedisSaver,
否则降级 InMemorySaver), 详见 ``AssistantOrchestrator.setup()``。

kb_retrieve runs hybrid search (pgvector dense + Elasticsearch BM25 -> RRF ->
rerank), 结果缓存于 Retrieval Cache(key 含 ACL 签名)。The ONLY relevance cutoff
is the rerank confidence threshold: hits scoring below it are treated as noise,
so an empty result means "no relevant document". On the first miss the query is
re-rewritten with a different strategy (RETRY_REWRITE_PROMPT) and retrieved
once more; if the second pass is still empty the Assistant answers
"未找到相关文档" directly (and returns NO reference sources) instead of
letting the LLM hallucinate over noise.

rewrite_query resolves pronouns/ellipsis ("那它的劣势呢" -> "XX 的劣势")
BEFORE intent classification, so the classifier routes on the resolved
standalone question and every downstream route shares one
disambiguated query. rewrite_query/chitchat/意图 LLM 兜底层共享 Prompt Cache。

Every node writes an audit record under the same trace_id.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.common_tools import lookup_employee_by_name
from app.assistant.a2a_client import AGENT_PROFILES, get_a2a_pool, is_agent_domain
from app.assistant.intent import IntentRecognizer, needs_current_time
from app.assistant.mcp_client import get_mcp_pool
from app.assistant.memory import get_memory_store
from app.assistant.prompts import (
    DIRECT_PROMPT,
    KB_ANSWER_PROMPT,
    QUERY_REWRITE_PROMPT,
    RETRY_REWRITE_PROMPT,
)
from app.assistant.stream import RunOverloaded, get_stream_hub, new_run_id
from app.cache.prompt_cache import cached_llm_call
from app.cache.retrieval_cache import cached_retrieve, invalidate_all
from app.cache.tool_cache import wrap_tools_for_cache
from app.chat_store import get_chat_store
from app.config import get_settings
from app.llm import extract_reasoning, get_chat_model, get_streaming_chat_model
from app.memory import graph_store
from app.memory.extraction import extract_memories
from app.memory.personal import PersonalContext, get_personal_agent
from app.rag.retriever import HybridRetriever
from app.schemas import (
    ChatRequest,
    ChatResponse,
    IntentResult,
    IntentType,
    KnowledgeChunk,
    Role,
)
from app.security.acl import Principal, is_allowed
from app.security.audit import get_audit_logger, new_trace_id
from app.security.auth import (
    PermissionDenied,
    check_agent_permission,
    check_capability_permission,
    check_mcp_permission,
    filter_tools_for_role,
)
from app.security.caller import Caller, reset_caller, set_caller
from app.security.masking import mask_text
from app.security.quota import check_capability_quota, remaining, resolve_daily_limit
from app.tools import CAPABILITY_TOOLS

logger = logging.getLogger(__name__)

# 内网统一按东八区(北京时间)计时, 不依赖容器 TZ(容器默认 UTC 会差 8 小时)。
_CST = timezone(timedelta(hours=8))
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 改写输出清洗: 需要剥掉的引号字符(含中文引号/书名号类包裹符)。
_QUOTE_CHARS = "\"'`“”‘’「」『』《》"
# 改写结果长度上限, 超过视为 LLM 失控输出, 回退原话。
_MAX_REWRITE_LEN = 200
# 消息长度达到该值且不含指代/省略标记时, 视为自包含问题, 跳过改写 LLM 调用。
_SELF_CONTAINED_LEN = 40
# 低于该长度的闲聊轮不触发个人记忆提取: "你好""谢谢" 这类寒暄不可能同
# 时携带值得跨会话记住的信息, 直接挡住这部分轮次的提取成本。
_MIN_MEMORY_MESSAGE_LEN = 12
# 显式记忆指令触发词(knowledge 桶唯一的写入门): "记一下/记住/帮我记"这类说法
# 命中才调 LLM 提炼落库。词表来自 settings.memory_record_keywords, 每次现拼(量小
# 成本忽略); 只扫用户消息, 助手回答里的"为您记住"不该触发记录。


def _memory_command_pattern(settings) -> re.Pattern:
    keywords = [k.strip() for k in settings.memory_record_keywords.split(",") if k.strip()]
    return re.compile("|".join(re.escape(k) for k in keywords)) if keywords else None


# 指代词/省略/追问标记: 命中则即使消息较长也必须走改写。
# 只保留真正的上下文依赖标记(人称/指示代词、回指短语、追问语气词),
# 不含"如何/哪个"等泛疑问词——它们大量出现在自包含问题中, 会触发无效改写。
_CONTEXT_DEPENDENT = re.compile(
    r"(它|他|她|这个|那个|这些|那些|上面说的|刚才说的|前面说|刚才说|呢$|呢[?？])"
)

# 能力域(tool_execute 的进程内工具分支, 计划 D2)的域内提示: 追加进 system_context,
# 把"怎么交付"钉死 —— web 要带来源 URL; docgen 要先检索后成文(禁编造)并把 download_url 输出为 Markdown 链接。
CAPABILITY_HINTS: dict[str, str] = {
    "web": (
        "可用工具说明: search_web 联网检索, fetch_url 抓取指定网页正文。"
        "时效性/外部事实必须以检索结果为准并在回答中附上来源 url; "
        "检索失败(返回 degraded/error)时如实说明未能联网核实, 不要编造来源。"
    ),
    "docgen": (
        "可用工具说明: generate_docx/xlsx/pptx/pdf/md/image 按工具说明里的 spec 结构生成可下载文件; "
        "本域还提供 search_web / fetch_url 用于联网检索。"
        "【先调研后成文】当文档内容依赖外部事实、近期进展、时效信息或用户未提供的原文"
        "(如\"调研近几个月 X 的技术发展\"\"最新行业动态\")时, 必须先调用 search_web"
        "(必要时换词多检、用 fetch_url 展开重点来源)取得真实资料, 正文与结论一律以检索结果为准并在"
        "文末列出来源 URL; 严禁在未检索时凭模型记忆编造事实/数据/来源——用户要的是调研, 不是看似"
        "完整的虚构报告。仅当用户已把原文/数据交给你、或纯排版转存时才可直接成文无需检索。"
        "要把图表/照片放进文档时, 在 spec 里用 images: [{\"src\": 本地文件名或图片URL}] 引用; "
        "分析图表要进 office 文档时取 render_chart 返回的 png_url(位图才能进 docx/pptx/pdf)。"
        "【交付链接】必须先真实调用 generate_* 并拿到返回的 download_url, 才能声称\"已生成\"; "
        "把 download_url 原样输出为 Markdown 链接, 形如 [《文件标题》](download_url), 不要改写/截断/编造地址, "
        "也不要输出裸路径纯文本(前端不会把裸 /api/ 路径渲染成可点链接)。"
        "spec 校验失败时按 error 提示修正后最多重试一次。"
    ),
}


def _platform_clock_text() -> str:
    """Format the platform clock as a prompt-ready string (UTC+8)."""
    now = datetime.now(_CST)
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {_WEEKDAYS[now.weekday()]} (UTC+8)"


class AssistantState(TypedDict):
    """State carried through the orchestration graph."""

    message: str
    session_id: str
    user_id: str
    role: Role
    department: str
    trace_id: str
    run_id: str  # SSE 流式运行的缓冲区 id; 空串 = 非流式调用(/api/chat/Studio)
    thinking: bool  # 本轮是否开启深度思考(生成节点逐 token 流式 + 透出思考)
    message_id: int | None  # 会话记录落库后的助手消息 id(persist_memory 回填)
    thinking_text: str  # 本轮生成的完整思考内容(供历史记录落库)
    artifacts: list[dict[str, Any]]  # 结构化交付物(name/url/title); office 下载走 answer 链接, 此字段预留
    history: str
    memory_ctx: str  # 长期记忆(Vector + Graph 通道)拼接结果, 与 history 分开审计
    current_time: str
    intent: IntentResult | None
    rewritten_query: str
    answer: str
    route: Literal["assistant_kb", "mcp_tool", "a2a_agent", "direct", "multi_agent"]
    target: str | None
    docs_meta: list[dict[str, Any]]
    # --- 多智能体并发委派 state (multi_agent_execute) ---
    # agent_targets: 调用方显式点选的智能体域(已清洗去重保序); agent_results: 同序的
    # [{index, domain, agent, route, answer, ok, status, error, elapsed_ms}]
    agent_targets: list[str]
    agent_results: list[dict[str, Any]]
    # --- Retrieve-Judge Loop state (knowledge_qa path) ---
    kb_query: str  # query used for the current retrieval attempt
    kb_chunks: list[Any]  # ACL-cleared parent blocks for the current attempt
    kb_meta_map: dict[str, dict[str, Any]]
    kb_attempt: int  # retrieval attempts so far
    kb_acl_blocked: bool  # hits existed but were all dropped by ACL


class AssistantOrchestrator:
    """Single-entry Assistant that routes across KB / MCP / A2A layers."""

    # rewrite_query / chitchat 共用同一个 self._llm, 显式记录温度/模型名
    # 以便与 Prompt Cache 的 key 保持一致 (与 __init__ 里构造参数同源)。
    _LLM_TEMPERATURE = 0.3

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=self._LLM_TEMPERATURE)
        self._intent = IntentRecognizer()
        self._memory = get_memory_store()
        self._audit = get_audit_logger()
        self._retriever: HybridRetriever | None = None
        # 并发多 run 同时首建检索器会各自跑一遍 rebuild_bm25(ES 全量重建),
        # 用锁把首建收敛成一次。
        self._retriever_lock: asyncio.Lock | None = None
        # 图不再在 __init__ 里直接编译: Checkpointer 需要 await setup() 建索引,
        # 而 __init__ 是同步的(FastAPI/orchestrator 单例构造时机决定)。
        self._graph = None
        self._checkpointer: BaseCheckpointSaver | None = None
        # AsyncRedisSaver 自带 async context manager 协议(__aenter__ 内部就会
        # 调 asetup() 建索引), 用 ExitStack 持有它, 才能把它的生命周期从
        # setup() 局部作用域延长到进程整个存活期, 并在 shutdown() 里干净关闭。
        self._exit_stack: AsyncExitStack | None = None
        self._setup_lock: asyncio.Lock | None = None
        self._setup_done = False
        # 后台流式 run 任务强引用: asyncio 只弱引用 task, 不持引用会被 GC 掉
        self._stream_tasks: set[asyncio.Task] = set()
        # 工具集与 ReAct 图的按 (target, role) 缓存: 每一轮 tool_call 都重新
        # wrap_tools_for_cache + create_agent 等于在热路径上重复编译一张图
        # (专业智能体层的同源做法见 app/agents/hr_agent/executor.py 的 _ensure_agent)。
        self._toolset_cache: dict[tuple[str, str, float], list[Any]] = {}
        self._agent_cache: dict[tuple[str, str, float], Any] = {}
        # ES BM25 全量重建的后台任务强引用(入库路径不陪重建等完)
        self._rebuild_tasks: set[asyncio.Task] = set()
        self._rebuild_running = False

    # ---------------- lifecycle ----------------

    async def setup(self) -> None:
        """幂等的异步初始化: 建 Checkpointer + Neo4j schema, 然后编译图。

        由 ``app.main`` 的 lifespan 在启动时主动调一次; ``handle()`` 也会
        兜底调一次(幂等, 已初始化就直接返回), 这样 LangGraph Studio 直接
        拿 ``get_graph()`` 时不会因为跳过 lifespan 而拿到未编译的图。
        """
        if self._setup_done:
            return
        if self._setup_lock is None:
            self._setup_lock = asyncio.Lock()
        async with self._setup_lock:
            if self._setup_done:
                return
            self._checkpointer = await self._build_checkpointer()
            await graph_store.ensure_schema()  # 内部已吞异常, 不会抛出
            self._graph = self._build_graph(self._checkpointer)
            self._setup_done = True

    async def shutdown(self) -> None:
        """释放 Checkpointer 持有的 Redis 连接(供 FastAPI lifespan 关闭时调用)。"""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        self._setup_done = False
        self._checkpointer = None
        self._graph = None

    async def _build_checkpointer(self) -> BaseCheckpointSaver:
        """Working State 存储: 优先 Redis, 降级 InMemorySaver(等价于未持久化)。"""
        settings = self._settings
        if not settings.checkpoint_enabled or not settings.redis_enabled:
            logger.info("checkpoint 未启用或 Redis 未启用, Working State 降级为 InMemorySaver")
            return InMemorySaver()
        try:
            from langgraph.checkpoint.redis import AsyncRedisSaver

            # AsyncRedisSaver(url) 直接构造即可(from_conn_string 是同义包装且返回
            # 的是上下文管理器对象本身, 不能对其调 .setup()); __aenter__ 内部已经
            # 调了 asetup() 建索引, 不需要再手动调一次 .setup()。
            self._exit_stack = AsyncExitStack()
            saver = await self._exit_stack.enter_async_context(AsyncRedisSaver(settings.redis_url))
            logger.info("Working State checkpointer: AsyncRedisSaver (%s)", settings.redis_url)
            return saver
        except Exception as exc:  # noqa: BLE001 - checkpointer 不可用绝不能阻断启动
            logger.warning(
                "AsyncRedisSaver 初始化失败(Redis 需带 RedisJSON+RediSearch 模块, "
                "即 redis-stack-server 镜像), Working State 降级为 InMemorySaver: %s", exc
            )
            if self._exit_stack is not None:
                await self._exit_stack.aclose()
                self._exit_stack = None
            return InMemorySaver()

    # ---------------- graph nodes ----------------

    async def build_context(self, state: AssistantState) -> dict[str, Any]:
        """架构图里 "Business Context" 的汇聚点(见模块 docstring)。

        Session Memory(Redis) -> history; 个人级记忆各桶(profile / preference /
        habit / episode / knowledge / graph)由 ``PersonalMemoryAgent.build`` 并行
        拉取并各自降级, 任一路不可用只影响拼接内容, 不会让整条对话链路失败。
        """
        session_id = state.get("session_id") or ""
        user_id = state.get("user_id") or ""
        message = state["message"]
        # 降级原因收集器是本轮局部字典逐层传参, 不用实例属性: orchestrator
        # 是进程单例, 并发多个 run 同时跑 build_context 时实例态会串轮。
        degraded_reasons: dict[str, str] = {}
        await self._emit_status(state, "understanding", "正在理解问题…")

        history, history_ok = await self._safe_history_text(session_id, degraded_reasons)
        memory_ctx = ""
        memory_ok = True
        if self._settings.long_term_memory_enabled and user_id:
            ctx, memory_ok = await self._safe_personal_context(user_id, message, degraded_reasons)
            memory_ctx = ctx.as_prompt()
            self._audit.log(
                state.get("trace_id") or "", "assistant", "personal_context_built",
                ctx.audit(), session_id,
            )
        # 降级事实进审计: 两路都挂也只记一条, 正常轮次零额外开销;
        # detail 只落异常类名+截断摘要, 避免异常文本夹带连接串等信息入审计文件。
        degraded = [name for name, ok in (("session_memory", history_ok), ("personal_memory", memory_ok)) if not ok]
        if degraded:
            self._audit.log(
                state.get("trace_id") or "", "assistant", "context_degraded",
                {"channels": degraded, "reasons": degraded_reasons}, session_id,
            )
        # 长期记忆拼进 history 一起往下传: 下游各节点(rewrite/classify/generate/
        # chitchat/tool/agent)统一只读 history 一个字段, 不需要各自感知
        # memory_ctx; memory_ctx 单独留在 state 里只用于审计可观测性。
        full_history = f"{history}\n{memory_ctx}".strip() if memory_ctx else history
        return {"history": full_history, "memory_ctx": memory_ctx}

    async def _safe_history_text(
        self, session_id: str, degraded_reasons: dict[str, str] | None = None
    ) -> tuple[str, bool]:
        """返回 (历史文本, 是否成功); 失败只记日志, 降级事实由调用方合入审计。"""
        try:
            return await self._memory.history_text(session_id), True
        except Exception as exc:  # noqa: BLE001 - 记忆层故障不能阻断对话
            logger.warning("session memory 读取失败, 本轮无历史上下文: %s", exc)
            self._note_degraded(degraded_reasons, "session_memory", exc)
            return "", False

    async def _safe_personal_context(
        self, user_id: str, query: str, degraded_reasons: dict[str, str] | None = None
    ) -> tuple[PersonalContext, bool]:
        """个人记忆召回兜底: 编排层内部已逐桶降级, 这里只兜满意外异常。"""
        try:
            return await get_personal_agent().build(user_id, query), True
        except Exception as exc:  # noqa: BLE001 - 记忆读失败只是这轮少点背景
            logger.warning("个人记忆召回失败, 本轮无长期记忆上下文: %s", exc)
            self._note_degraded(degraded_reasons, "personal_memory", exc)
            return PersonalContext(), False

    @staticmethod
    def _note_degraded(
        reasons: dict[str, str] | None, channel: str, exc: Exception
    ) -> None:
        # 只落异常类名+截断摘要, 避免异常文本夹带连接串等信息入审计文件。
        if reasons is not None:
            reasons[channel] = f"{exc.__class__.__name__}: {str(exc)[:200]}"

    async def resolve_time(self, state: AssistantState) -> dict[str, Any]:
        """Pre-fetch the platform clock for time-sensitive questions.

        Runs before intent classification so every downstream route (KB / MCP /
        A2A / chitchat) shares one authoritative "now", instead of letting the
        LLM guess today's date from its (stale) training data. The clock is read
        in-process (no external service dependency), so this can never fail.
        """
        if not needs_current_time(state["message"]):
            return {"current_time": ""}
        current_time = _platform_clock_text()
        self._audit.log(
            state.get("trace_id") or "", "assistant", "time_resolved",
            {"current_time": current_time}, state.get("session_id"),
        )
        return {"current_time": current_time}

    async def classify_intent(self, state: AssistantState) -> dict[str, Any]:
        # 用改写后的独立问题分类: "那帮我查一下它的余额" 消解为
        # "查一下 XX 的余额" 后才能正确判出 tool_call 而非 knowledge_qa。
        query = state.get("rewritten_query") or state["message"]
        intent = await self._intent.classify(query, state.get("history", ""))
        await self._emit_status(state, "routed", f"意图识别: {intent.intent.value}")
        self._audit.log(
            state.get("trace_id") or new_trace_id(), "assistant", "intent_classified",
            intent.model_dump(), state.get("session_id"),
        )
        return {"intent": intent, "target": intent.target}

    async def rewrite_query(self, state: AssistantState) -> dict[str, Any]:
        """Condense a context-dependent follow-up into a standalone question.

        Runs BEFORE intent classification so both the classifier and all
        downstream routes operate on the disambiguated question. Multi-turn
        follow-ups like "那它的劣势呢" or "她是哪个部门来着" carry unresolved
        pronouns; classifying/retrieving on the raw utterance fails.

        Skipped (zero cost) when there is no history or the message is already
        a long, self-contained question; falls back to the raw message on any
        LLM failure or implausible output so downstream routes can never be
        blocked by rewriting.
        """
        message = state["message"]
        history = state.get("history") or ""
        rewritten = message
        skipped = ""
        if not history.strip():
            skipped = "no_history"
        elif len(message) >= _SELF_CONTAINED_LEN and not _CONTEXT_DEPENDENT.search(message):
            # Long utterances without pronouns/ellipsis markers are almost
            # certainly self-contained; skip the LLM call to save latency.
            skipped = "self_contained"
        else:
            prompt = QUERY_REWRITE_PROMPT.format(history=history, message=message)

            async def _invoke() -> str:
                # Name the LLM run so the rewrite prompt/response is easy
                # to locate in the LangSmith trace tree.
                resp = await self._llm.ainvoke(
                    prompt,
                    config={"run_name": "rewrite_query", "tags": ["rewrite_query"]},
                )
                return str(resp.content)

            try:
                # Prompt Cache: 改写是纯函数式调用(同 prompt -> 同结果), 不含
                # 权限/实时数据, 可以安全缓存。
                raw = await cached_llm_call(
                    self._settings.llm_model, self._LLM_TEMPERATURE, prompt, _invoke
                )
                rewritten = self._clean_rewrite(raw, message)
            except Exception as exc:
                logger.warning("query rewrite failed, fallback to raw message: %s", exc)
                rewritten = message
        # Always emit the audit record (even unchanged) so the rewrite result
        # is visible for every turn that goes through this node.
        self._audit.log(
            state.get("trace_id") or "", "assistant", "query_rewritten",
            {
                "original": message,
                "rewritten": rewritten,
                "changed": rewritten != message,
                "skipped": skipped,
            },
            state.get("session_id"),
        )
        return {"rewritten_query": rewritten}

    @staticmethod
    def _clean_rewrite(raw: str, fallback: str) -> str:
        """Sanitize the LLM rewrite output; return ``fallback`` if implausible.

        Guards against: surrounding quotes (incl. CJK), echoed prompt prefixes
        ("改写后的独立查询:"), multi-line output, and absurd lengths.
        """
        text = raw.strip()
        # Take the first non-empty line only (LLM may append explanations).
        for line in text.splitlines():
            if line.strip():
                text = line.strip()
                break
        text = text.strip(_QUOTE_CHARS)
        # Drop echoed prompt prefixes like "改写后的独立查询: xxx".
        for prefix in ("改写后的独立查询", "改写后的查询", "改写后", "独立查询", "查询"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip(": :").strip(_QUOTE_CHARS)
                break
        # Reject empty / absurdly long / absurdly short rewrites.
        if not text or len(text) > _MAX_REWRITE_LEN or len(text) < 2:
            return fallback
        return text

    # ---------------- knowledge_qa: retrieve-judge loop ----------------

    async def kb_retrieve(self, state: AssistantState) -> dict[str, Any]:
        """单意图路径的一次检索(薄壳: 取 query/attempt -> ``_kb_retrieve_once`` -> 回写 state)。"""
        query = state.get("kb_query") or state.get("rewritten_query") or state["message"]
        attempt = int(state.get("kb_attempt") or 0) + 1
        kb = await self._kb_retrieve_once(state, query, attempt)
        return {
            "kb_query": query,
            "kb_chunks": kb["chunks"],
            "kb_meta_map": kb["meta_map"],
            "kb_attempt": kb["attempt"],
            "kb_acl_blocked": kb["acl_blocked"],
        }

    async def _kb_retrieve_once(
        self, state: AssistantState, query: str, attempt: int
    ) -> dict[str, Any]:
        """One retrieval pass with rerank thresholding + ACL trim.

        图节点只薄壳地组装与回写 ``kb_*`` state, 真正的检索一次走这一份实现。
        query 与 attempt 都由参数传入, 因此本 helper 不读 ``kb_*`` state。返回
        ``{chunks, meta_map, acl_blocked, attempt}``。

        The query comes from ``kb_query`` (set by ``rewrite_query`` on the
        first pass, or by ``kb_requery`` on a retry). Retrieval channels and
        RRF fusion apply no cutoff; only the rerank stage drops chunks below
        ``retrieval_score_threshold``, so an empty result here means "no
        relevant document", not "nothing matched".
        """
        retriever = await self._get_retriever()
        await self._emit_status(
            state, "searching", "重新检索知识库…" if attempt > 1 else "检索知识库…"
        )
        # 统一身份主体: 检索阶段据此做 Metadata Filter 前置权限裁剪。
        principal = Principal(
            user_id=state.get("user_id") or "",
            department=state.get("department") or "",
            role=self._role_of(state),
        )

        async def _invoke_retrieve() -> tuple[list[KnowledgeChunk], str]:
            return await retriever.retrieve(query, principal=principal)

        # Retrieval Cache: key 含 ACL 签名, 命中即跳过一次混合检索(Embedding +
        # ES + rerank), 但下方的 assemble_parents / is_allowed 仍然照常执行
        # —— 缓存在权限裁剪之前, 不影响最终授权校验这道纵深防御。
        # 注: cached payload 是 retrieve() 的输出, 已在内部 attach_texts 补好子块正文,
        # 故命中时不再走一次 chunk_id 主键回表; 文档重入库路径已调 invalidate_all(),
        # 缓存里不会残留陈旧 chunk_id, 无需为本方案新增失效机制。
        children, score_mode, cache_hit = await cached_retrieve(
            query, self._settings.rag_top_k, self._settings.rerank_top_n,
            principal, _invoke_retrieve,
        )
        # Assemble hit child chunks into complete parent section blocks so
        # the LLM answers from full sections (with page/section citations).
        chunks = await retriever.assemble_parents(children) if children else []
        # 最终授权校验 (纵深防御): 进入 Context Builder 前逐条复核, 拦截
        # 父块组装/索引脏数据可能引入的越权块; 无权块在拼接前剔除并审计。
        # 与发布态门禁(status!='ready')共用这一个出口: 一次性批量查未就绪 doc,
        # 不逐块查, 也不新增第二套权限判断。
        authorized: list[KnowledgeChunk] = []
        dropped_docs: set[str] = set()
        not_ready: set[str] = set()
        if chunks:
            try:
                from app.docs.service import docs_not_ready

                not_ready = await docs_not_ready([c.doc_id for c in chunks])
            except Exception as exc:  # DB 抖动: 不误伤, 仅跳过发布态门禁
                logger.warning("docs_not_ready check failed, skip status gate: %s", exc)
        for c in chunks:
            if c.doc_id in not_ready:
                # 正在入库/入库失败的半篇文档: 视同无权, 不进入回答(默认拒)。
                dropped_docs.add(c.doc_id)
                continue
            if is_allowed(c, principal):
                authorized.append(c)
            else:
                dropped_docs.add(c.doc_id)
        if dropped_docs:
            self._audit.log(
                state.get("trace_id") or "", "assistant", "acl_final_check_dropped",
                {"user_id": principal.user_id, "role": principal.role.value,
                 "department": principal.department, "dropped_docs": sorted(dropped_docs)},
                state.get("session_id"),
            )
        meta_map: dict[str, dict[str, Any]] = {}
        if authorized:
            try:
                from app.docs.service import get_meta_map

                meta_map = await get_meta_map(list({c.doc_id for c in authorized}))
            except Exception as exc:  # database down must not break chat
                logger.warning("doc metadata lookup failed, degrade to plain context: %s", exc)
        top_score = max((c.score for c in authorized), default=None)
        self._audit.log(
            state.get("trace_id") or "", "assistant", "kb_retrieved",
            {"attempt": attempt, "query": query, "chunks": [c.chunk_id for c in authorized],
             "scores": [c.score for c in authorized],
             "top_score": top_score, "dropped_by_acl": sorted(dropped_docs),
             "score_mode": score_mode, "cache_hit": cache_hit,
             "threshold": (
                 self._settings.retrieval_score_threshold if score_mode == "rerank"
                 else None
             )},
            state.get("session_id"),
        )
        return {
            "chunks": authorized,
            "meta_map": meta_map,
            "acl_blocked": bool(dropped_docs) and not authorized,
            "attempt": attempt,
        }

    def _judge_retrieval(self, state: AssistantState) -> Literal["generate", "retry", "refuse"]:
        """Decide whether the retrieval is trustworthy enough to generate from.

        ``generate``: confident hits survived threshold + ACL. ``retry``:
        below threshold and the retry budget is still available. ``refuse``:
        still below threshold after the retry, or the only hits were dropped
        by ACL (a permission fact, not a knowledge gap — answering from an
        empty context would invite fabrication).

        只是图条件边的 state 适配器, 判定口径在 :meth:`_judge`。
        """
        return self._judge(
            state.get("kb_chunks") or [],
            bool(state.get("kb_acl_blocked")),
            int(state.get("kb_attempt") or 0),
        )

    def _judge(
        self, chunks: list[Any], acl_blocked: bool, attempt: int
    ) -> Literal["generate", "retry", "refuse"]:
        """判定口径的唯一实现: 图条件边按它决定生成/重检/拒答。"""
        if chunks:
            return "generate"
        if acl_blocked:
            # 越权命中被全部剔除: 与"知识库没有"是不同事实, 交给生成节点
            # 用空上下文回答会诱导编造, 直接明确拒答。
            return "refuse"
        if attempt <= self._settings.retrieval_max_retries:
            return "retry"
        return "refuse"

    async def kb_requery(self, state: AssistantState) -> dict[str, Any]:
        """单意图路径的换策略重检(薄壳: 只把新查询写回 ``kb_query``)。"""
        query = state.get("kb_query") or state.get("rewritten_query") or state["message"]
        return {"kb_query": await self._kb_requery_query(state, query)}

    async def _kb_requery_query(self, state: AssistantState, query: str) -> str:
        """Rewrite the failing query with a different retrieval strategy.

        Falls back to keyword-stripping when the LLM is unavailable or
        echoes the same query, so the retry is never a no-op loop.

        审计里的 ``attempt`` 从 state 取: 上一道 ``kb_retrieve`` 已把递增后的尝试次数
        写回 ``kb_attempt``。真正重检那一轮是它后面的下一次 ``kb_retrieve``。
        """
        rewritten = ""
        try:
            resp = await self._llm.ainvoke(
                RETRY_REWRITE_PROMPT.format(
                    history=state.get("history") or "(无)",
                    message=state["message"],
                    query=query,
                ),
                config={"run_name": "kb_requery", "tags": ["kb_requery"]},
            )
            rewritten = self._clean_rewrite(str(resp.content), "")
        except Exception as exc:
            logger.warning("retry rewrite failed, use heuristic fallback: %s", exc)
        if not rewritten or rewritten == query:
            rewritten = self._keyword_fallback(query)
        self._audit.log(
            state.get("trace_id") or "", "assistant", "kb_requery",
            {"from": query, "to": rewritten, "attempt": state.get("kb_attempt")},
            state.get("session_id"),
        )
        return rewritten

    @staticmethod
    def _keyword_fallback(query: str) -> str:
        """Heuristic retry query: strip question words and punctuation."""
        text = re.sub(r"(请问|帮我|我想问|怎么|如何|是什么|有没有)", " ", query)
        text = re.sub(r"[?？。！!,，、;；:：\"'`“”‘’（）()]+", " ", text)
        text = re.sub(r"(的|了|吗|呢|啊|呀|吧)$", "", text.strip())
        text = re.sub(r"\s+", " ", text).strip()
        return text or query

    async def kb_generate(self, state: AssistantState) -> dict[str, Any]:
        """单意图路径的知识库生成(薄壳: 从 ``kb_*`` state 组装后交给 helper)。"""
        query = state.get("kb_query") or state.get("rewritten_query") or state["message"]
        kb = {
            "chunks": state.get("kb_chunks") or [],
            "meta_map": state.get("kb_meta_map") or {},
            "acl_blocked": bool(state.get("kb_acl_blocked")),
            "attempt": int(state.get("kb_attempt") or 0),
        }
        return await self._kb_generate_answer(
            state, query, kb, stream=self._stream_enabled(state), original=state["message"]
        )

    async def _kb_generate_answer(
        self,
        state: AssistantState,
        query: str,
        kb: dict[str, Any],
        *,
        stream: bool,
        original: str,
    ) -> dict[str, Any]:
        """Answer from confident chunks; refuse explicitly when there are none.

        ``original`` 是写进 prompt 的"用户原话": 单意图路径传原始消息(资料是按改写后
        的问题检的, 附上原话避免偏离用户真实问法); 非流式内部调用可传同一个问题。
        """
        chunks = kb["chunks"]
        meta_map = kb["meta_map"]
        if not chunks:
            # Judge decided the knowledge base has nothing relevant: answer
            # without the LLM so noise can never be turned into fiction.
            # ACL-blocked uses the same generic wording on purpose — telling
            # the caller "相关资料存在但你无权查看" would leak document existence.
            # docs_meta 返回空列表: 接口不携带任何参考来源。
            answer = "未找到相关文档，无法回答该问题。建议联系对应部门或转人工咨询。"
            self._audit.log(
                state.get("trace_id") or "", "assistant", "kb_refused",
                {"query": query, "attempts": kb["attempt"],
                 "reason": "acl_blocked" if kb["acl_blocked"] else "below_threshold"},
                state.get("session_id"),
            )
            return {"answer": answer, "route": "assistant_kb", "docs_meta": [], "thinking_text": ""}
        retriever = await self._get_retriever()
        context = retriever.format_context(chunks, meta_map)
        # 生成与检索语义对齐: 资料是按改写后的问题检索的, 生成也应以同一问题
        # 作答; 若发生过改写, 附上原话避免偏离用户真实问法。
        message = query if query == original else f"{query}(用户原话: {original})"
        prompt = KB_ANSWER_PROMPT.format(
            context=context,
            history=state.get("history", "(无)"),
            message=message,
            current_time=self._now_text(state),
        )
        # 流式链路(SSE run): 逐 token 推送, 思考开启时额外透出 think; 否则保持
        # 一次性 ainvoke(非流式 /api/chat 调用方行为完全不变)。
        if stream and self._stream_enabled(state):
            await self._emit_status(state, "generating", "基于知识库生成回答…")
            answer, think_text = await self._stream_answer(state, prompt)
        else:
            resp = await self._llm.ainvoke(prompt)
            answer, think_text = str(resp.content), ""
        self._audit.log(
            state.get("trace_id") or "", "assistant", "kb_answered",
            {"chunks": [c.chunk_id for c in chunks], "attempts": kb["attempt"]},
            state.get("session_id"),
        )
        docs_meta = [
            {
                "doc_key": c.doc_id,
                "title": c.title,
                "section": c.section,
                "page_no": c.page_no,
                "tags": (meta_map.get(c.doc_id) or {}).get("tags", []),
            }
            for c in chunks
        ]
        return {
            "answer": answer, "route": "assistant_kb", "docs_meta": docs_meta,
            "thinking_text": think_text,
        }

    async def tool_execute(self, state: AssistantState) -> dict[str, Any]:
        """单意图路径的工具分派(薄壳: 取 target/query 后交给 ``_tool_react``)。"""
        intent = state["intent"]
        target = intent.target if intent and intent.target else "hr"
        query = state.get("rewritten_query") or state["message"]
        return await self._tool_react(state, query, target)

    async def _tool_react(
        self, state: AssistantState, query: str, target: str
    ) -> dict[str, Any]:
        """Run a small ReAct loop over the target domain's tools.

        两类工具源(计划 D2): ``target in CAPABILITY_TOOLS`` 的**能力域**(web 联网检索/
        docgen 文件生成)直接取进程内工具集 —— 跳过 MCP 连接池与 ``check_mcp_permission``
        (那是 MCP 专用, 会对未知 server 默认拒); 其余 target 维持原有 MCP 分派路径不变。
        两路在此之后共用同一套: 权限 Mask -> 注入 lookup_employee_by_name -> Tool Cache
        包装 -> ReAct 循环, 热路径其余不动。query/target 由参数传入, 节点只是薄壳。

        工具集与编译好的 ReAct 图按 (target, role, 工具清单版本) 缓存: 一次工具调用
        在旧写法里要付"重新包一层 Tool Cache + 重新编译一张图"的代价, 而这两件事的
        产物在清单不变时完全等价。版本戳用 MCP 发现时刻(:meth:`MCPClientPool.cache_stamp`),
        清单一变缓存自然失效, 不会把"新上的工具"藏在一个旧 agent 里。
        """
        role = self._role_of(state)
        trace_id = state.get("trace_id") or ""
        session_id = state.get("session_id") or ""

        capability_tools = CAPABILITY_TOOLS.get(target)
        domain_hint = ""
        if capability_tools is not None:
            # 能力域: 工具在本进程内, 不走 MCP 的角色×工具矩阵, 但不是"没权限层"——
            # 域级角色闸门(哪些角色能用) + 每人每日配额(能用多少次)都在这拦。
            try:
                check_capability_permission(role, target)
            except PermissionDenied as exc:
                logger.warning("能力域权限拒绝: role=%s target=%s err=%s", role.value, target, exc)
                self._audit.log(
                    trace_id, "assistant", "capability_permission_denied",
                    {"role": role.value, "capability": target, "reason": str(exc)},
                    session_id,
                )
                return {
                    "answer": f"权限不足:{exc}", "status": "denied",
                    "route": "mcp_tool", "target": target,
                }
            quota_ok, used = await check_capability_quota(target, state.get("user_id") or "")
            limit = resolve_daily_limit()
            if not quota_ok:
                self._audit.log(
                    trace_id, "assistant", "capability_quota_exceeded",
                    {"capability": target, "user_id": state.get("user_id") or "",
                     "used": used, "limit": limit},
                    session_id,
                )
                return {
                    "answer": (
                        f"今日 {target} 能力用量已达上限({limit} 次), 本轮不再处理;"
                        "请明天再来, 或把需求改成不需要联网检索/文件生成的问法。"
                    ),
                    "status": "denied", "route": "mcp_tool", "target": target,
                }
            self._audit.log(
                trace_id, "assistant", "capability_dispatch",
                {"capability": target, "role": role.value,
                 "used": used, "limit": limit, "remaining": remaining(used, limit)},
                session_id,
            )
            await self._emit_status(state, "tool", f"正在使用 {target} 能力工具…")
            all_tools = list(capability_tools)
            domain_hint = CAPABILITY_HINTS.get(target, "")
            stamp = 0.0  # 进程内工具集是静态的, 不需要版本失效
        else:
            try:
                check_mcp_permission(role, target, "*")
            except PermissionDenied as exc:
                # 权限拒绝是合规上最该留痕的事件(与 acl_final_check_dropped 同等对待),
                # 转答复展示给用户的同时必须落审计, 否则拒绝记录只存在于用户界面。
                logger.warning("MCP 权限拒绝: role=%s target=%s err=%s", role.value, target, exc)
                self._audit.log(
                    trace_id, "assistant", "mcp_permission_denied",
                    {"role": role.value, "target": target, "reason": str(exc)},
                    session_id,
                )
                # status=denied: 多任务合并靠它区分"办成了"与"被拒了", 不能只看 answer 非空。
                return {
                    "answer": f"权限不足:{exc}", "status": "denied",
                    "route": "mcp_tool", "target": target,
                }
            all_tools = await get_mcp_pool().get_tools(target)
            stamp = get_mcp_pool().cache_stamp(target)

        # 权限Mask: 按角色×工具白名单矩阵过滤, 隐藏工具对 LLM 不可见、不可调。
        # 能力域未建矩阵 -> 透传全部(app/security/auth.py 的既定语义)。
        tools = self._tools_for(target, role, all_tools, stamp)
        visible_names = [t.name for t in tools]
        self._audit.log(
            state.get("trace_id") or "", "assistant", "tools_filtered",
            {"server": target, "role": role.value, "visible_tools": visible_names},
            state.get("session_id"),
        )
        if not tools:
            return {
                "answer": f"权限不足: 角色 {role.value} 在 {target} 域无可用工具。",
                "status": "denied", "route": "mcp_tool", "target": target,
            }

        agent = self._react_agent(target, role, tools, stamp)
        # 身份/时间走 System 消息, 与用户请求文本分离; 并显式区分"当前操作者"
        # (登录态)与"任务目标用户"(消息中指定的他人), 否则 LLM 会把操作者
        # 工号误用作目标员工的查询参数(如"查张三的余额"却传了自己的工号)。
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={state.get('user_id') or 'anonymous'}; "
            f"当前角色 role={role.value}; 当前时间={self._now_text(state)}。"
            "操作者身份仅代表登录态, 不等于任务目标用户: 若用户消息中指定了目标员工"
            "(工号/姓名), 以消息指定的为准; 仅当查询\"我/本人\"相关数据且未指定他人时, "
            "才默认使用操作者 employee_id。caller_* 字段由系统注入且会覆盖你填的值, "
            "无需也不要在工具参数里传它们。"
        )
        if domain_hint:
            system_context += f"\n{domain_hint}"
        dispatch_kind = "capability" if capability_tools is not None else "mcp"
        self._audit.log(
            trace_id, "assistant", "mcp_dispatch",
            {"server": target, "kind": dispatch_kind, "tools": [t.name for t in tools]},
            session_id,
        )
        await self._emit_status(state, "tool", f"正在调用 {target} 域业务工具…")
        # 用消解后的独立问题驱动 ReAct: "审批到哪一步了" 已改写为
        # "FIN5000 审批到哪一步了", 工具才能拿到正确的查询对象。
        result = await agent.ainvoke(
            {"messages": [("system", system_context), ("user", query)]}
        )
        answer = "工具调用未产生回复。"
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                answer = str(msg.content)
                break
        return {"answer": answer, "status": "ok", "route": "mcp_tool", "target": target}

    # 缓存上限: 域×角色×清单版本的笛卡尔积本来有界, 封顶只是防止版本频繁更替时
    # 旧条目无限堆积(每条都是一个编译好的图, 不能当垃圾留着)。
    _TOOLSET_CACHE_MAX = 64

    def _tools_for(
        self, target: str, role: Role, all_tools: list[Any], stamp: float
    ) -> list[Any]:
        """权限 Mask -> 注入跨域解析工具 -> Tool Cache 包装(结果按版本缓存)。"""
        key = (target, role.value, stamp)
        cached = self._toolset_cache.get(key)
        if cached is not None:
            return cached
        tools = filter_tools_for_role(role, target, all_tools)
        # 跨域基础解析能力(姓名->工号)注入: 用户只给姓名时先解析工号再调业务工具。
        tools = [*tools, lookup_employee_by_name]
        # 身份注入 + Tool Cache: ReAct 循环里工具由 LLM 自主决定何时以何参数调用,
        # 缓存与"谁在调"都只能在工具本体上做。只读前缀白名单命中的工具额外接 Redis
        # 缓存(注入的 caller_* 已在 key 里, 所以结果按调用者隔离); 写操作工具只注身份、
        # 不缓存。见 app/cache/tool_cache.wrap_tools_for_cache。
        wrapped = wrap_tools_for_cache(tools, target, role.value)
        self._toolset_cache[key] = wrapped
        self._evict_stale(self._toolset_cache, stamp)
        return wrapped

    def _react_agent(self, target: str, role: Role, tools: list[Any], stamp: float) -> Any:
        """取/建一个编译好的 ReAct 图(同一批工具只编译一次)。"""
        key = (target, role.value, stamp)
        agent = self._agent_cache.get(key)
        if agent is not None:
            return agent
        agent = create_agent(self._llm, tools)
        self._agent_cache[key] = agent
        self._evict_stale(self._agent_cache, stamp)
        return agent

    def _evict_stale(self, cache: dict[tuple, Any], stamp: float) -> None:
        """先把"版本已不是当前这一批"的条目扫掉, 超上限再按插入序淘汰最旧的。"""
        for key in [k for k in cache if k[2] != stamp]:
            cache.pop(key, None)
        while len(cache) > self._TOOLSET_CACHE_MAX:
            cache.pop(next(iter(cache)), None)

    async def agent_delegate(self, state: AssistantState) -> dict[str, Any]:
        """单意图路径的 A2A 委派(薄壳)。"""
        intent = state["intent"]
        target = intent.target if intent and intent.target else "hr"
        query = state.get("rewritten_query") or state["message"]
        return await self._delegate_task(state, query, target)

    async def _delegate_task(
        self, state: AssistantState, query: str, target: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Delegate to a specialist agent over the A2A protocol.

        单意图委派与多智能体并发分支共用这一份实现: 可信身份 metadata / Agent Card
        地址覆盖 / 角色闸门 / 审计只此一处, 不会漂出第二套委派语义。两个只有并发分支
        需要的东西:
        - ``status``(ok/denied): 并发分支靠它区分"办成了"与"被拒了", 不能只看
          answer 非空(被拒时也带着一段拒答文本回来); 它不是 AssistantState 的键,
          单意图节点顺路带回也不影响图(LangGraph 只取声明过的键);
        - ``timeout``: 逐位上限只由并发分支传入, 单意图维持全局 ``a2a_timeout`` 口径。

        故意不接入 Tool Cache(对原方案的一处修正): AGENT_DELEGATE 按
        INTENT_PROMPT 的定义就是"需要专业系统多步办理的复杂业务"(如"我要报销"
        "帮我开在职证明"), 属于写/办理类操作, 缓存会把一次"提交成功"的响应
        复用给下一次本应真实发生的提交, 造成业务数据不一致; 而且下方构造的
        task 文本里含 `[当前时间=...]`, 天然每一轮都不同, 即使去接入缓存几乎
        也不会命中。多智能体并发分支同样不缓存(与这口径一致)。
        """
        agent_name = f"{target}_agent"
        role = self._role_of(state)
        try:
            check_agent_permission(role, agent_name)
        except PermissionDenied as exc:
            logger.warning("A2A 委派权限拒绝: role=%s agent=%s err=%s", role.value, agent_name, exc)
            self._audit.log(
                state.get("trace_id") or "", "assistant", "a2a_permission_denied",
                {"role": role.value, "agent": agent_name, "reason": str(exc)},
                state.get("session_id"),
            )
            return {
                "answer": f"权限不足:{exc}", "status": "denied",
                "route": "a2a_agent", "target": target,
            }

        # 任务文本不含操作者工号: 身份经协议级 metadata 结构化下发(见下方 send,
        # 专业智能体只信 metadata), 由下游自行注入。文本中避免出现裸工号标签,
        # 防止下游 LLM 把"当前操作者"误当成"任务目标用户"(目标以消息文本为准)。
        # 用消解后的独立问题作为当前请求, 下游无需再做指代消解。
        task = f"[当前时间={self._now_text(state)}] {query}"
        if state.get("history"):
            task = f"对话背景:\n{state['history']}\n\n当前请求: {task}"
        self._audit.log(state.get("trace_id") or "", "assistant", "a2a_delegate", {"agent": agent_name}, state.get("session_id"))
        await self._emit_status(state, "delegate", f"正在委派 {agent_name} 专业智能体办理…")
        # 可信身份经协议级 metadata 结构化下发 (而非文本标签), 供专业智能体做权限分级。
        # send_guarded 而不 send: 智能体侧的 ReAct 循环没有全局预算, 卡住时这一条委派
        # 会永远不返回并永久占住一个并发额度; 超时降级为可读文本比挂死好得多。
        answer = await get_a2a_pool().send_guarded(
            target,
            task,
            metadata={"user_id": state.get("user_id") or "", "role": role.value},
            timeout=timeout,
        )
        return {"answer": answer, "status": "ok", "route": "a2a_agent", "target": target}

    async def chitchat(self, state: AssistantState) -> dict[str, Any]:
        """单意图路径的直答(薄壳)。"""
        return await self._chitchat_answer(
            state,
            state["message"],
            stream=self._stream_enabled(state),
            rewritten=state.get("rewritten_query") or "",
        )

    async def _chitchat_answer(
        self, state: AssistantState, message: str, *, stream: bool, rewritten: str = ""
    ) -> dict[str, Any]:
        prompt = DIRECT_PROMPT.format(current_time=self._now_text(state))
        history = state.get("history") or ""
        parts = [prompt]
        # 传入对话历史: 即使改写失败回退原话, 模型仍能看到上下文。
        if history.strip():
            parts.append(f"对话历史:\n{history}")
        # 改写节点已把指代消解成独立问题; 若与原话不同则附上, 帮助模型理解上下文。
        if rewritten and rewritten != message:
            parts.append(
                f"用户: {message}\n(结合对话历史, 该问题指的是: {rewritten})"
            )
        else:
            parts.append(f"用户: {message}")
        full_prompt = "\n\n".join(parts)

        # 流式链路(SSE run): 逐 token 推送; 此时不走 Prompt Cache(缓存命中的
        # 重放没有思考过程, 且命中时几乎零延迟, 缓存收益小于体验损失)。
        if stream and self._stream_enabled(state):
            await self._emit_status(state, "generating", "正在思考回答…")
            answer, think_text = await self._stream_answer(state, full_prompt)
            return {"answer": answer, "route": "direct", "thinking_text": think_text}

        async def _invoke() -> str:
            resp = await self._llm.ainvoke(full_prompt)
            return str(resp.content)

        # Prompt Cache: key 是完整 prompt 的 hash, 历史/改写结果不同会得到不同
        # key, 不会把某一轮的闲聊回复错误命中给另一轮。
        answer = await cached_llm_call(
            self._settings.llm_model, self._LLM_TEMPERATURE, full_prompt, _invoke
        )
        return {"answer": answer, "route": "direct", "thinking_text": ""}

    async def persist_memory(self, state: AssistantState) -> dict[str, Any]:
        masked_answer = mask_text(state["answer"])
        masked_message = mask_text(state["message"])
        session_id = state.get("session_id") or ""
        # append 的返回值是本轮新折叠出的会话摘要(没发生溢出压缩时为空串)。
        session_summary = await self._memory.append(session_id, masked_message, masked_answer)
        await self._write_personal_memory(state, masked_message, masked_answer, session_summary)
        # 显式"记一下"指令是 knowledge 桶唯一的写入入口(自动提取与情节蒸馏已下线):
        # 关键词命中才进 LLM 提炼链路, 未命中零成本; 失败只记日志不阻断对话。
        await self._record_explicit_knowledge(state, masked_message, masked_answer)
        # 页面会话记录落库(app/chat_store.py): 失败静默降级, 不阻断对话。
        intent = state.get("intent")
        message_id = await get_chat_store().save_turn(
            session_id=session_id,
            user_id=state.get("user_id") or "",
            role=self._role_of(state).value,
            department=state.get("department") or "",
            trace_id=state.get("trace_id") or "",
            user_message=masked_message,
            answer=masked_answer,
            thinking=state.get("thinking_text") or "",
            route=state.get("route") or "",
            target=state.get("target") or "",
            intent=intent.intent.value if intent else "",
            docs_meta=state.get("docs_meta") or [],
            artifacts=state.get("artifacts") or [],
        )
        self._audit.log(
            state.get("trace_id") or "", "assistant", "turn_completed",
            {"route": state.get("route"), "answer_len": len(state["answer"]),
             "chat_message_id": message_id}, session_id,
        )
        return {"message_id": message_id}

    async def _record_explicit_knowledge(
        self, state: AssistantState, message: str, answer: str
    ) -> None:
        """命中显式记忆指令词 -> 提炼并分"同话题更新/新增"落 knowledge 桶。"""
        settings = self._settings
        if not (settings.long_term_memory_enabled and settings.memory_record_enabled):
            return
        user_id = state.get("user_id") or ""
        if not user_id:
            return
        pattern = _memory_command_pattern(settings)
        if pattern is None or not pattern.search(message):
            return
        try:
            stats = await get_personal_agent().remember_knowledge(
                user_id, state.get("session_id") or "", message, answer
            )
            if any(stats.values()):
                self._audit.log(
                    state.get("trace_id") or "", "assistant", "explicit_knowledge_recorded",
                    {"user_id": user_id, **stats}, state.get("session_id") or "",
                )
        except Exception as exc:  # noqa: BLE001 - 记不上只是这轮没记上
            logger.warning("显式知识记录失败, 本轮跳过: %s", exc)

    async def _write_personal_memory(
        self,
        state: AssistantState,
        message: str,
        answer: str,
        session_summary: str = "",
    ) -> None:
        """一次提取 -> 分桶落盘(画像/偏好/习惯/情节/知识/图谱), 失败只记日志不抛出。

        闲聊轮不再一律跳过提取: "我在研发部""以后都用 Markdown 回复" 这类自我陈述
        恰恰会被意图分类归为 chitchat, 按路由一律跳过就等于个人记忆永远写不进
        去; 只保留短消息过滤(真正的寒暄不会超过 ``_MIN_MEMORY_MESSAGE_LEN``),
        无信息量的轮次交给提取 prompt 自己返回空列表。会话摘要 -> 情节的沉淀不
        依赖提取, 闲聊轮同样可能触发窗口溢出, 所以那一步放在路由判定之外。
        """
        if not self._settings.long_term_memory_enabled:
            return
        user_id = state.get("user_id") or ""
        if not user_id:
            return
        session_id = state.get("session_id") or ""
        agent = get_personal_agent()
        try:
            worth_extracting = (
                state.get("route") != "direct" or len(message) >= _MIN_MEMORY_MESSAGE_LEN
            )
            if worth_extracting:
                # 先把已存的偏好/习惯喂给提取器判重(标量直读, 零 embedding),
                # 否则每轮都会把同一件事的不同说法当新记忆写入, 偏好桶越堆越呆。
                existing_prefs, existing_habits = await agent.existing_stable_texts(user_id)
                extraction = await extract_memories(
                    message,
                    answer,
                    existing_preferences=existing_prefs,
                    existing_habits=existing_habits,
                )
                stats = await agent.write(user_id, session_id, extraction)
                if stats:
                    self._audit.log(
                        state.get("trace_id") or "", "assistant", "personal_memory_written",
                        stats, session_id,
                    )
            if session_summary:
                await agent.add_session_episode(user_id, session_id, session_summary)
        except Exception as exc:  # noqa: BLE001 - 记忆写失败只是少一条记忆
            logger.warning("个人记忆写入失败, 本轮跳过: %s", exc)

    # ---------------- 多智能体并发委派(显式点选) ----------------

    @staticmethod
    def _agent_display(domain: str) -> str:
        """分节标题/状态提示用的可读智能体名; 未注册域退回裸域名(不抛异常)。"""
        return (AGENT_PROFILES.get(domain) or (domain, ""))[0]

    @staticmethod
    def normalize_agent_targets(
        domains: list[str], max_targets: int
    ) -> tuple[list[str], list[str], list[str]]:
        """把点选列表清洗成 ``(可执行域, 非法域键, 超上限被截断的项)``(纯函数, 可离线单测)。

        只做两件事: 剥掉未注册的域键(不在 ``AGENT_URLS`` 里的一律不执行, 也不发网络
        请求), 以及按 ``multi_agent_max_targets`` 截掉尾部。权限判定不在这里 —— 那是
        ``check_agent_permission`` 的单一职责(见 :meth:`_delegate_task`), 在这抢判会
        多出第二套权限口径。入参形状(去空白/转小写/去重保序)已由 ``ChatRequest`` 洗过。
        上限取 ``max(1, ...)``: 配成 0 会让"点了却没反响"变成难以报修的行为。
        """
        limit = max(1, max_targets)
        valid = [d for d in domains if is_agent_domain(d)]
        invalid = [d for d in domains if not is_agent_domain(d)]
        return valid[:limit], invalid, valid[limit:]

    async def _run_one_agent(
        self, state: AssistantState, domain: str, index: int, total: int, sem: asyncio.Semaphore
    ) -> dict[str, Any]:
        """一个智能体的一次委派: 并发闸门 + 逐位超时 + 异常隔离, 产出完整的一节结果。

        超时两道故意不同值: 内层 ``send_guarded`` 用全局 ``a2a_timeout``(默认 120s), 它
        回的是一段可读的降级文本(该节仍算"有内容"); 外层 ``wait_for`` 用
        ``multi_agent_timeout``(默认 150s), 只在内层也拦不住时才把这一节判为超时。
        """
        label = self._agent_display(domain)
        trace_id = state.get("trace_id") or ""
        session_id = state.get("session_id") or ""
        query = state.get("rewritten_query") or state["message"]
        outcome: dict[str, Any] = {
            "index": index, "domain": domain, "agent": label, "route": "a2a_agent",
            "target": domain, "answer": "", "ok": False, "status": "pending",
            "error": "", "elapsed_ms": 0,
        }
        await self._emit_status(state, "delegate", f"[{index + 1}/{total}] {label} 处理中…")
        started = time.perf_counter()
        try:
            async with sem:
                result = await asyncio.wait_for(
                    self._delegate_task(state, query, domain),
                    timeout=self._settings.multi_agent_timeout,
                )
            # 成败看 status 而不是"answer 非空": 权限被拒时也会带着一段拒答文本回来。
            status = str(result.get("status") or "")
            if status not in ("ok", "denied"):
                status = "ok" if (result.get("answer") or "").strip() else "empty"
            answer = str(result.get("answer") or "")
            outcome.update(
                {
                    "answer": answer, "status": status, "ok": status == "ok",
                    "error": ""
                    if status == "ok"
                    else (answer[:160] if status == "denied" else "未产生回答"),
                }
            )
        except asyncio.TimeoutError:
            outcome["status"] = "timeout"
            outcome["error"] = f"超过 {self._settings.multi_agent_timeout:.0f}s 未返回"
        except Exception as exc:  # noqa: BLE001 - 单个智能体失败不连坐其他节
            logger.warning("多智能体委派失败(domain=%s): %s", domain, exc)
            outcome["status"] = "error"
            outcome["error"] = f"{exc.__class__.__name__}: {str(exc)[:160]}"
        outcome["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        # 完成也发一条 status: 前端进度靠它从"处理中"推到"已完成/未完成"(并发下
        # 完成顺序不等于点选顺序, 所以每条都自带序号与智能体名)。
        await self._emit_status(
            state, "delegate",
            f"[{index + 1}/{total}] {label}{'已完成' if outcome['ok'] else '未完成'}",
        )
        self._audit.log(
            trace_id, "assistant",
            "agent_task_completed" if outcome["ok"] else "agent_task_failed",
            {
                "index": index, "domain": domain, "agent": label, "route": outcome["route"],
                "status": outcome["status"], "elapsed_ms": outcome["elapsed_ms"],
                "answer_len": len(outcome["answer"]), "error": outcome["error"],
            },
            session_id,
        )
        return outcome

    def _fallback_agent_outcome(
        self, domain: str, index: int, exc: BaseException
    ) -> dict[str, Any]:
        """gather 兼顶路径: 分支连 _run_one_agent 的隔离都走不到时也要凑出一节。"""
        return {
            "index": index, "domain": domain, "agent": self._agent_display(domain),
            "route": "a2a_agent", "target": domain, "answer": "", "ok": False,
            "status": "error", "error": f"{exc.__class__.__name__}: {str(exc)[:160]}",
            "elapsed_ms": 0,
        }

    async def multi_agent_execute(self, state: AssistantState) -> dict[str, Any]:
        """把同一个问题并发下发给用户点选的每个专业智能体, 分节合并成一条回答。

        点选是本轮唯一的调度依据(系统不推断"该问谁"), 所以这一轮不需要意图漏斗:
        ``IntentResult`` 是合成出来的(AGENT_DELEGATE + 首个可执行域 + layer=explicit),
        只为让落库/审计/前端路由标签的字段形状与单意图路径一致。非法域与超限项也各
        占一节: 用户点的每一项都有交代, 不会被静默吞掉。
        """
        targets = list(state.get("agent_targets") or [])
        domains, invalid, overflow = self.normalize_agent_targets(
            targets, self._settings.multi_agent_max_targets
        )
        total = len(domains)
        self._audit.log(
            state.get("trace_id") or "", "assistant", "multi_agent_planned",
            {
                "requested": targets, "domains": domains, "invalid": invalid,
                "dropped": overflow, "parallelism": max(1, self._settings.multi_agent_parallelism),
                "timeout": self._settings.multi_agent_timeout,
            },
            state.get("session_id"),
        )
        await self._emit_status(
            state, "delegate",
            f"正在并发委派 {total} 个专业智能体…" if total else "点选的智能体都不可执行…",
        )
        results: list[dict[str, Any]] = []
        if domains:
            sem = asyncio.Semaphore(max(1, self._settings.multi_agent_parallelism))
            raw = await asyncio.gather(
                *(self._run_one_agent(state, d, i, total, sem) for i, d in enumerate(domains)),
                return_exceptions=True,
            )
            results.extend(
                self._fallback_agent_outcome(d, i, res) if isinstance(res, BaseException) else res
                for i, (d, res) in enumerate(zip(domains, raw))
            )
        # 非法域/超上限项补成独立小节(不参与并发, 只负责"有交代")。
        for pos, d in enumerate(invalid):
            results.append(
                {
                    "index": len(domains) + pos, "domain": d, "agent": self._agent_display(d),
                    "route": "a2a_agent", "target": d, "answer": "", "ok": False,
                    "status": "invalid", "error": "未注册的专业智能体域", "elapsed_ms": 0,
                }
            )
        for pos, d in enumerate(overflow):
            results.append(
                {
                    "index": len(domains) + len(invalid) + pos, "domain": d,
                    "agent": self._agent_display(d), "route": "a2a_agent", "target": d,
                    "answer": "", "ok": False, "status": "dropped",
                    "error": f"超出单次可点选上限({self._settings.multi_agent_max_targets})",
                    "elapsed_ms": 0,
                }
            )
        answer = self._merge_agent_answers(
            results, max_chars=self._settings.multi_agent_answer_chars
        )
        primary = domains[0] if domains else (targets[0] if targets else "")
        intent = IntentResult(
            intent=IntentType.AGENT_DELEGATE, target=primary or None, confidence=1.0,
            reason=f"用户显式点选 {total} 个专业智能体并发委派", layer="explicit",
        )
        if self._stream_enabled(state) and answer:
            # 整篇一条 token 下发(不逐 token): N 路生成的 token 交错会糊成一团。
            await self._emit(state, {"type": "token", "delta": answer})
        self._audit.log(
            state.get("trace_id") or "", "assistant", "multi_agent_merged",
            {
                "sections": len(results),
                "ok": sum(1 for r in results if r.get("ok")),
                "invalid": len(invalid), "dropped": len(overflow), "domains": domains,
            },
            state.get("session_id"),
        )
        return {
            "answer": answer, "route": "multi_agent", "target": primary or None,
            "intent": intent, "agent_results": results,
        }

    @classmethod
    def _merge_agent_answers(
        cls, results: list[dict[str, Any]], *, max_chars: int = 0
    ) -> str:
        """把各智能体的结果拼成"一节一个智能体"的 Markdown(纯函数, 不调 LLM、可离线单测)。
    
        刻意不再过一道 LLM 综合: 各节内容已是智能体产出的事实, 重写只会带来改写与编造
        风险。被拒/超时/非法域/超限都单独成节并在末尾汇总一句, 用户点的每一项都能对上号。
        """
        if not results:
            return "本轮没有可执行的专业智能体。"
        succeeded = [r for r in results if r.get("ok")]
        if not succeeded:
            reasons = "; ".join(
                f"{r.get('agent') or r.get('domain')}: {r.get('error') or '未知错误'}"
                for r in results
            )
            return f"点选的 {len(results)} 个专业智能体这次都没能给出结果: {reasons}"
        denied = [r for r in results if str(r.get("status") or "") == "denied"]
        invalid = [r for r in results if str(r.get("status") or "") == "invalid"]
        dropped = [r for r in results if str(r.get("status") or "") == "dropped"]
        failed = [
            r for r in results
            if not r.get("ok")
            and str(r.get("status") or "") not in ("denied", "invalid", "dropped")
        ]
        parts: list[str] = []
        if len(results) > 1:
            parts.append(f"你点了 {len(results)} 个专业智能体, 同一个问题已并发下发, 各自结果如下:")
        for pos, r in enumerate(results, start=1):
            agent = str(r.get("agent") or r.get("domain") or "").strip() or f"第 {pos} 项"
            domain = str(r.get("domain") or "").strip()
            head = f"## {pos}. {agent}"
            if domain and domain != agent:
                head += f"（{domain}）"
            status = str(r.get("status") or "")
            if status in ("invalid", "dropped", "denied"):
                body = f"> 未执行: {r.get('error') or '未知原因'}"
            elif not r.get("ok"):
                body = f"> 未完成: {r.get('error') or '未知错误'}"
            else:
                body = str(r.get("answer") or "").strip()
                # 并发 N 份长文本会把回答长度乘以 N, 截断优于把整页掉进历史消息里。
                if max_chars > 0 and len(body) > max_chars:
                    body = (
                        f"{body[:max_chars]}\n\n"
                        f"> 本节内容已截断(超出 {max_chars} 字), 需要完整结果请单独点选该智能体。"
                    )
            parts.append(f"{head}\n\n{body}")
        if denied:
            parts.append(f"> 有 {len(denied)} 个智能体当前角色无权访问, 需要相应角色后才能委派。")
        if failed:
            parts.append(f"> 有 {len(failed)} 个智能体未能给出结果, 可稍后单独重试。")
        if invalid:
            parts.append(f"> 有 {len(invalid)} 项不是已注册的专业智能体域, 未执行。")
        if dropped:
            parts.append(f"> 有 {len(dropped)} 项超出单次可点选上限, 未执行, 请分次询问。")
        return "\n\n".join(parts)
    
    # ---------------- SSE 流式推送 ----------------

    @staticmethod
    def _stream_enabled(state: AssistantState) -> bool:
        """只要本次调用挂在流式 run 上, 生成节点就逐 token 推送。

        thinking 只决定推送里有没有 think 事件(以及模型的思考开关),
        不影响正文是否流式 —— 关闭思考时前端依然能看到打字机效果。
        """
        return bool(state.get("run_id"))

    async def _emit(self, state: AssistantState, event: dict[str, Any]) -> None:
        """向当前 run 的事件缓冲区写一条事件(非流式调用时无缓冲区, 静默丢弃)。"""
        run_id = state.get("run_id") or ""
        if run_id:
            await get_stream_hub().append(run_id, event)

    async def _emit_status(self, state: AssistantState, stage: str, text: str) -> None:
        await self._emit(state, {"type": "status", "stage": stage, "text": text})

    async def _stream_answer(self, state: AssistantState, prompt: str) -> tuple[str, str]:
        """逐 token 流式生成: think/token 事件经 StreamHub 实时下发(可断点续传)。

        返回 ``(回答全文, 思考全文)``; 流中途异常但已有部分内容时直接返回
        已生成部分(前端体验优先), 无任何内容时回退一次性 ainvoke。
        """
        thinking_on = bool(state.get("thinking"))
        # 必须按本轮 thinking 取对应变体: self._llm 是按全局默认(开思考)构造的,
        # 拿它跑"关闭思考"的请求会从服务端默认值里意外拿到 reasoning。
        llm = get_streaming_chat_model(
            self._settings.llm_model,
            temperature=self._LLM_TEMPERATURE,
            thinking=thinking_on,
        )
        answer_parts: list[str] = []
        think_parts: list[str] = []
        try:
            async for chunk in llm.astream(prompt):
                reasoning = extract_reasoning(chunk)
                if reasoning:
                    think_parts.append(reasoning)
                    await self._emit(state, {"type": "think", "delta": reasoning})
                piece = chunk.content if isinstance(chunk.content, str) else ""
                if piece:
                    answer_parts.append(piece)
                    await self._emit(state, {"type": "token", "delta": piece})
            return "".join(answer_parts), "".join(think_parts)
        except Exception as exc:  # noqa: BLE001 - 流式失败不能断了本轮回答
            if answer_parts:
                logger.warning("token streaming broke mid-answer, keep partial: %s", exc)
                return "".join(answer_parts), "".join(think_parts)
            logger.warning("llm streaming unavailable, fallback to ainvoke: %s", exc)
            resp = await llm.ainvoke(prompt)
            answer = str(resp.content)
            # 降级路径不丢正文: 无 run 时 _emit 静默丢弃, 前端凭 result 事件补渲染
            await self._emit(state, {"type": "token", "delta": answer})
            return answer, extract_reasoning(resp)

    # ---------------- routing ----------------

    @staticmethod
    def _role_of(state: AssistantState) -> Role:
        """Normalise role; LangGraph Studio may pass a plain string."""
        role = state.get("role")
        if isinstance(role, Role):
            return role
        try:
            return Role(str(role))
        except ValueError:
            return Role.EMPLOYEE

    @staticmethod
    def _now_text(state: AssistantState) -> str:
        """Render the resolved platform time for prompt / task injection."""
        return state.get("current_time") or "(未获取)"

    @staticmethod
    def _route_after_rewrite(state: AssistantState) -> str:
        """点选了智能体就走并发委派分支, 否则走原来的意图分类分派。

        总开关 ``multi_agent_enabled`` 在入图前就把 ``agent_targets`` 洗空(见
        :meth:`_run_graph`), 所以这里不重复判开关: 否则"关着开关但 state 里还有值"
        这种不一致状态得靠两处口径同步才能不出现。
        """
        if state.get("agent_targets"):
            return "multi_agent_execute"
        return "classify_intent"

    @staticmethod
    def _route_by_intent(state: AssistantState) -> str:
        intent = state["intent"]
        if intent is None:
            return "kb_retrieve"
        return {
            IntentType.KNOWLEDGE_QA: "kb_retrieve",
            IntentType.TOOL_CALL: "tool_execute",
            IntentType.AGENT_DELEGATE: "agent_delegate",
            IntentType.CHITCHAT: "chitchat",
        }[intent.intent]

    def _build_graph(self, checkpointer: BaseCheckpointSaver | None = None):
        g = StateGraph(AssistantState)
        g.add_node("build_context", self.build_context)
        g.add_node("resolve_time", self.resolve_time)
        g.add_node("rewrite_query", self.rewrite_query)
        g.add_node("classify_intent", self.classify_intent)
        g.add_node("kb_retrieve", self.kb_retrieve)
        g.add_node("kb_requery", self.kb_requery)
        g.add_node("kb_generate", self.kb_generate)
        g.add_node("tool_execute", self.tool_execute)
        g.add_node("agent_delegate", self.agent_delegate)
        g.add_node("chitchat", self.chitchat)
        g.add_node("multi_agent_execute", self.multi_agent_execute)
        g.add_node("persist_memory", self.persist_memory)

        # rewrite_query 前置于意图识别: 分类器与全部分派路由(含多智能体并发分支)
        # 共享消解后的独立问题, 避免"那帮我查一下它的余额"因指代未消解而误分类;
        # 并发委派拿到的也是同一个消解后的问题, 下游智能体不需再做消解。
        g.add_edge(START, "build_context")
        g.add_edge("build_context", "resolve_time")
        g.add_edge("resolve_time", "rewrite_query")
        g.add_conditional_edges(
            "rewrite_query",
            self._route_after_rewrite,
            {
                "classify_intent": "classify_intent",
                "multi_agent_execute": "multi_agent_execute",
            },
        )
        g.add_conditional_edges(
            "classify_intent",
            self._route_by_intent,
            {
                "kb_retrieve": "kb_retrieve",
                "tool_execute": "tool_execute",
                "agent_delegate": "agent_delegate",
                "chitchat": "chitchat",
            },
        )
        # Retrieve-Judge Loop: 检索后按结果判定 —— 有相关文档直接生成;
        # 无结果且预算未用尽则换改写重检一次; 重检后仍无结果则明确拒答(不进 LLM)。
        g.add_conditional_edges(
            "kb_retrieve",
            self._judge_retrieval,
            {
                "generate": "kb_generate",
                "retry": "kb_requery",
                "refuse": "kb_generate",
            },
        )
        g.add_edge("kb_requery", "kb_retrieve")
        # 多智能体并发分支: 执行与分节合并在同一节点内完成, 同样汇入 persist_memory。
        for node in ("kb_generate", "tool_execute", "agent_delegate", "chitchat", "multi_agent_execute"):
            g.add_edge(node, "persist_memory")
        g.add_edge("persist_memory", END)
        return g.compile(checkpointer=checkpointer)

    # ---------------- public API ----------------

    async def _get_retriever(self) -> HybridRetriever:
        if self._retriever is None:
            if self._retriever_lock is None:
                self._retriever_lock = asyncio.Lock()
            # 双检锁: 多个并发 run 同时首建时, ES BM25 全量重建只跑一次。
            async with self._retriever_lock:
                if self._retriever is None:
                    retriever = HybridRetriever()
                    await retriever.rebuild_bm25()
                    self._retriever = retriever
        return self._retriever

    async def refresh_knowledge(self) -> None:
        """Rebuild the ES BM25 index after a document (re)ingest (drift repair).

        重建是"把全库子块从 PG 流式读出来再 bulk 进 ES", 分钟级; 它过去在入库请求
        里同步等完 —— 一个管理员重入库一篇文档, 全厂人的检索都跟着变慢(而且同一个
        事件循环里没人能抢过它)。现在: 缓存立刻失效(语义不变), 重建丢给后台单飞任务。

        单飞: 同时来了十篇入库也只跑一次全量重建(重建本身幂等且以全库为输入, 多次
        重跑纯浪费); 后台任务失败只记日志 —— ES 是可从 PG 全量重建的派生索引。
        """
        # 文档重新入库后旧 chunk_id 可能已被删除/重建, Retrieval Cache 必须同步失效,
        # 否则会命中缓存里的脏 chunk_id(即使 TTL 未到)。这道不能推迟到后台。
        await invalidate_all()
        if self._rebuild_running:
            logger.info("BM25 全量重建已在后台进行中, 本次入库不重复发起")
            return
        if self._retriever is None:
            return  # 检索器还未首建(下一次 _get_retriever 会连带重建 BM25)
        self._rebuild_running = True
        task = asyncio.create_task(self._rebuild_bm25_bg(), name="bm25-rebuild")
        self._rebuild_tasks.add(task)
        task.add_done_callback(self._rebuild_tasks.discard)

    async def _rebuild_bm25_bg(self) -> None:
        """后台重建 BM25; 任何异常都不外溢(派生索引下次启动还会重补)。"""
        try:
            if self._retriever is not None:
                await self._retriever.rebuild_bm25()
        except Exception as exc:  # noqa: BLE001 - ES 索引反正下次启动会重建
            logger.warning("后台 BM25 重建失败(下次入库/启动会重试): %s", exc)
        finally:
            self._rebuild_running = False

    @property
    def ready(self) -> bool:
        """图是否已编译完成(就绪探针读它, 不必读到编排器内部字段)。"""
        return self._graph is not None

    def ensure_graph_for_studio(self):
        """LangGraph Studio 的同步取图入口(详见 ``get_graph()``)。

        Studio 的 dev server 不跑本项目的 FastAPI lifespan, 也就不会调用
        ``setup()``; 这里同步兜底编译一份用 InMemorySaver 的图 —— 与
        pyproject.toml 里 langgraph-cli 注释说的"内存版 in-memory 后端"一致,
        本地调试场景下本来也不需要真的把 checkpoint 落到 Redis。
        """
        if self._graph is None:
            self._graph = self._build_graph(InMemorySaver())
        return self._graph

    async def handle(self, req: ChatRequest) -> ChatResponse:
        """Handle one user turn end-to-end (non-streaming).

        非流式入口同样过流式闸门的同一个并发上限: 一次 run 不论是不停往缓冲区写
        事件还是一次性返回, 占用的下游(LLM 配额/PG 连接池/事件循环)完全同一份,
        只给一路设限等于闸门形同虚设。拿不到额度直接抛(路由层转 503)。
        """
        await self.setup()  # 幂等: lifespan 已初始化过就直接返回
        hub = get_stream_hub()
        if not hub.try_acquire_run():
            raise RunOverloaded(f"同时处理的对话已达上限({hub.inflight}), 请稍后重试")
        trace_id = new_trace_id()
        try:
            self._audit.log(
                trace_id, "user", "message_received",
                {"user_id": req.user_id, "role": req.role.value,
                 "department": req.department, "message": req.message},
                req.session_id,
            )
            final: AssistantState = await self._run_graph(req, trace_id, run_id="", thinking=False)
            resp = self._build_response(req, final, trace_id)
            intent = final.get("intent") or IntentResult(intent=IntentType.CHITCHAT)
            memory_snapshot, _ = await self._safe_history_text(req.session_id)
            self._audit.log(
                trace_id,
                "handle",
                "final",
                {
                    "intent": intent.intent,
                    "confidence": intent.confidence,
                    "reason": intent.reason,
                    "answer": final["answer"],
                    "route": final.get("route", "direct"),
                    "target": final.get("target"),
                    "_memory": memory_snapshot,
                },
                req.session_id,
            )
            return resp
        finally:
            hub.release_run()

    async def handle_stream(self, req: ChatRequest) -> str:
        """流式入口: 后台任务跑图, 事件落 StreamHub, 立即返回 run_id。

        HTTP 连接与图执行解耦: 客户端断开(刷新/断网)不影响 run 继续跑,
        重连凭 run_id + Last-Event-ID 从断点重放并续流(app/assistant/stream.py)。

        并发闸门在创建缓冲区之前取, 并在 run 终态的 finally 里归还(包括被 cancel
        和异常路径): 额度泄漏会不可逆地收紧闸门, 比过载本身更难排查。
        """
        await self.setup()
        hub = get_stream_hub()
        if not hub.try_acquire_run():
            raise RunOverloaded(
                f"同时进流的对话已达上限({hub.inflight}), 请稍后重试"
            )
        trace_id = new_trace_id()
        run_id = new_run_id()
        thinking = (
            req.thinking if req.thinking is not None else self._settings.llm_thinking_enabled
        )
        # 从取到额度到后台任务真正跑起来的这段代码(建缓冲区/写审计)一旦抛出,
        # 任务里的 finally 根本不会执行, 额度就永久丢了 —— 故整段纳进保护。
        try:
            hub.create(run_id)
            self._audit.log(
                trace_id, "user", "message_received",
                {"user_id": req.user_id, "role": req.role.value,
                 "department": req.department, "message": req.message,
                 "run_id": run_id, "thinking": thinking},
                req.session_id,
            )
        except BaseException:
            hub.release_run()
            raise

        async def _pipeline() -> None:
            try:
                final: AssistantState = await self._run_graph(
                    req, trace_id, run_id=run_id, thinking=thinking
                )
                resp = self._build_response(req, final, trace_id)
                event = resp.model_dump(mode="json")
                event["type"] = "result"
                event["thinking_text"] = final.get("thinking_text") or ""
                await hub.append(run_id, event)
                await hub.append(run_id, {"type": "done", "status": "completed"})
            except asyncio.CancelledError:
                # 服务进程优雅关停: 标记中断即可, 缓冲区保留给重启后的
                # 前端(此时续流已无意义, done 未写入, 前端按 404/降级处理)
                logger.warning("streaming run %s cancelled during shutdown", run_id)
                raise
            except Exception as exc:  # noqa: BLE001 - 异常经 error 事件透出给前端
                logger.exception("streaming run %s failed", run_id)
                await hub.append(run_id, {"type": "error", "message": str(exc)})
                await hub.append(run_id, {"type": "done", "status": "error"})
            finally:
                await hub.finish(run_id)
                hub.release_run()

        try:
            task = asyncio.create_task(_pipeline(), name=f"chat-stream:{run_id}")
        except BaseException:
            # 创建任务失败(如事件循环已关): 闸门必须当场归还
            hub.release_run()
            raise
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)
        return run_id

    def _entry_agent_targets(self, req: ChatRequest) -> list[str]:
        """总开关与入参点选在此一处合流: 关了就把点选洗空。

        图内条件路由(:meth:`_route_after_rewrite`)只看 ``agent_targets``, 不需要在两个
        地方保持"开关 + 点选"的同步口径; 抽成方法也让开关行为能离线断言(不起整栈)。
        """
        if not self._settings.multi_agent_enabled:
            return []
        return list(req.agent_targets or [])

    async def _run_graph(
        self, req: ChatRequest, trace_id: str, *, run_id: str, thinking: bool
    ) -> AssistantState:
        """执行一次完整图调用(流式/非流式共用同一状态初始化)。

        进图前先把调用者绑到当前上下文(app/security/caller.py): 工具包装层在真正
        发起调用前从这里取 ``caller_*`` 注入参数, 与 LLM 填的同名字段冲突时一律覆盖。
        asyncio 任务创建时复制当前上下文, 所以图节点、并发的多路委派与 ReAct 工具调用
        看的都是这一份; 离开本轮时必须复位, 否则同一个任务上下文里的下一轮会读到
        上一个工号。
        """
        from app.tracing import langfuse_callback

        caller_token = set_caller(
            Caller(
                user_id=(req.user_id or "").strip(),
                role=req.role.value,
                department=(req.department or "").strip(),
            )
        )
        # 总开关在一处生效(见 :meth:`_entry_agent_targets`), 关了就直接忽略点选。
        agent_targets = self._entry_agent_targets(req)
        try:
            return await self._graph.ainvoke(
                {
                    "message": req.message,
                    "session_id": req.session_id,
                    "user_id": req.user_id,
                    "role": req.role,
                    "department": req.department,
                    "trace_id": trace_id,
                    "run_id": run_id,
                    "thinking": thinking,
                    "message_id": None,
                    "thinking_text": "",
                    "artifacts": [],
                    "history": "",
                    "memory_ctx": "",
                    "current_time": "",
                    "intent": None,
                    "rewritten_query": "",
                    "answer": "",
                    "route": "direct",
                    "target": None,
                    "docs_meta": [],
                    "kb_query": "",
                    "kb_chunks": [],
                    "kb_meta_map": {},
                    "kb_attempt": 0,
                    "kb_acl_blocked": False,
                    "agent_targets": agent_targets,
                    "agent_results": [],
                },
                # Working State Checkpoint: thread_id 用 session_id, 每轮对话都完整
                # 传入上方所有 AssistantState 字段, 不会与上一轮残留的 checkpoint
                # 状态串台(字段默认全量重置, 见 ``AssistantState``)。
                # Langfuse: 顶层挂 CallbackHandler, 回调沿 LangGraph 传播到全部子
                # run(节点内 LLM/工具/MCP 调用), 整轮对话归并为一条 trace;
                # 传 trace_id 使 Langfuse trace id 与 audit.jsonl 全链路审计号对齐;
                # 未启用时 langfuse_callback() 返回空 dict, 行为与改动前一致。
                {
                    "configurable": {"thread_id": req.session_id},
                    **langfuse_callback(req.session_id, req.user_id, trace_id),
                },
            )
        finally:
            reset_caller(caller_token)

    def _build_response(
        self, req: ChatRequest, final: AssistantState, trace_id: str
    ) -> ChatResponse:
        """由图终态组装结构化响应(非流式返回体 / 流式 result 事件同源)。"""
        intent = final.get("intent") or IntentResult(intent=IntentType.CHITCHAT)
        metadata: dict[str, Any] = {
            "confidence": intent.confidence,
            "reason": intent.reason,
            "docs": final.get("docs_meta", []),
        }
        # 多智能体轮额外携逐个智能体的结果: 前端可据此展示哪个智能体办成了/被拒/超时。
        agent_results = final.get("agent_results") or []
        if agent_results:
            metadata["agents"] = [
                {
                    "index": r.get("index"),
                    "domain": r.get("domain"),
                    "agent": r.get("agent"),
                    "route": r.get("route"),
                    "status": r.get("status") or "",
                    "ok": bool(r.get("ok")),
                    "error": r.get("error") or "",
                    "elapsed_ms": r.get("elapsed_ms"),
                }
                for r in agent_results
            ]
        return ChatResponse(
            session_id=req.session_id,
            answer=mask_text(final["answer"]),
            intent=intent.intent,
            route=final.get("route", "direct"),
            target=final.get("target"),
            trace_id=trace_id,
            message_id=final.get("message_id"),
            artifacts=final.get("artifacts") or [],
            metadata=metadata,
        )


_orchestrator: AssistantOrchestrator | None = None


def get_orchestrator() -> AssistantOrchestrator:
    """Process-wide singleton orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AssistantOrchestrator()
    return _orchestrator


def get_graph():
    """Graph factory exposed to LangGraph Studio (see langgraph.json).

    Enables LangSmith tracing first so every Studio run is captured as a
    trace under the configured project. Studio 不跑 FastAPI lifespan, 因此这里
    走同步兜底编译(见 ``ensure_graph_for_studio``), 用 InMemorySaver。

    注意: Langfuse 靠代码里显式挂 CallbackHandler(见 ``_run_graph``), Studio 自行
    发起的 invoke 不经我们的调用点, 所以 Studio 里的运行只有 LangSmith trace。
    """
    from app.tracing import init_tracing

    init_tracing()
    return get_orchestrator().ensure_graph_for_studio()
