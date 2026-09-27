"""Assistant orchestration graph (LangGraph).

Routing policy (single-entry multi-agent):
    user -> [build_context] -> [resolve_time] -> [rewrite_query]
         -> [classify_intent] -> one of:
        knowledge_qa    -> kb_retrieve -> judge -> kb_generate   (confident hits)
                                            |-> kb_requery -> kb_retrieve (no-result retry)
                                            `-> refuse  (still empty -> fixed reply, no LLM)
        chitchat        -> chitchat
        tool_call       -> tool_execute   (Assistant -> MCP business tools)
        agent_delegate  -> agent_delegate (Assistant -> A2A specialist)
    -> [persist_memory] -> END

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
standalone question and all four downstream routes share one
disambiguated query. rewrite_query/chitchat/意图 LLM 兜底层共享 Prompt Cache。

Every node writes an audit record under the same trace_id.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.common_tools import lookup_employee_by_name
from app.assistant.a2a_client import get_a2a_pool
from app.assistant.intent import IntentRecognizer, needs_current_time
from app.assistant.mcp_client import get_mcp_pool
from app.assistant.memory import get_memory_store
from app.assistant.prompts import (
    DIRECT_PROMPT,
    KB_ANSWER_PROMPT,
    QUERY_REWRITE_PROMPT,
    RETRY_REWRITE_PROMPT,
)
from app.assistant.stream import get_stream_hub, new_run_id
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
    check_mcp_permission,
    filter_tools_for_role,
)
from app.security.masking import mask_text

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
# 指代词/省略/追问标记: 命中则即使消息较长也必须走改写。
# 只保留真正的上下文依赖标记(人称/指示代词、回指短语、追问语气词),
# 不含"如何/哪个"等泛疑问词——它们大量出现在自包含问题中, 会触发无效改写。
_CONTEXT_DEPENDENT = re.compile(
    r"(它|他|她|这个|那个|这些|那些|上面说的|刚才说的|前面说|刚才说|呢$|呢[?？])"
)


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
    history: str
    memory_ctx: str  # 长期记忆(Vector + Graph 通道)拼接结果, 与 history 分开审计
    current_time: str
    intent: IntentResult | None
    rewritten_query: str
    answer: str
    route: Literal["assistant_kb", "mcp_tool", "a2a_agent", "direct"]
    target: str | None
    docs_meta: list[dict[str, Any]]
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

        Runs BEFORE intent classification so both the classifier and all four
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
        """One retrieval pass with rerank thresholding + ACL trim.

        The query comes from ``kb_query`` (set by ``rewrite_query`` on the
        first pass, or by ``kb_requery`` on a retry). Retrieval channels and
        RRF fusion apply no cutoff; only the rerank stage drops chunks below
        ``retrieval_score_threshold``, so an empty result here means "no
        relevant document", not "nothing matched".
        """
        retriever = await self._get_retriever()
        query = state.get("kb_query") or state.get("rewritten_query") or state["message"]
        await self._emit_status(
            state, "searching",
            "重新检索知识库…" if int(state.get("kb_attempt") or 0) else "检索知识库…",
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
        attempt = int(state.get("kb_attempt") or 0) + 1
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
            "kb_query": query,
            "kb_chunks": authorized,
            "kb_meta_map": meta_map,
            "kb_attempt": attempt,
            "kb_acl_blocked": bool(dropped_docs) and not authorized,
        }

    def _judge_retrieval(self, state: AssistantState) -> Literal["generate", "retry", "refuse"]:
        """Decide whether the retrieval is trustworthy enough to generate from.

        ``generate``: confident hits survived threshold + ACL. ``retry``:
        below threshold and the retry budget is still available. ``refuse``:
        still below threshold after the retry, or the only hits were dropped
        by ACL (a permission fact, not a knowledge gap — answering from an
        empty context would invite fabrication).
        """
        chunks = state.get("kb_chunks") or []
        if chunks:
            return "generate"
        if state.get("kb_acl_blocked"):
            # 越权命中被全部剔除: 与"知识库没有"是不同事实, 交给生成节点
            # 用空上下文回答会诱导编造, 直接明确拒答。
            return "refuse"
        if int(state.get("kb_attempt") or 0) <= self._settings.retrieval_max_retries:
            return "retry"
        return "refuse"

    async def kb_requery(self, state: AssistantState) -> dict[str, Any]:
        """Rewrite the failing query with a different retrieval strategy.

        Falls back to keyword-stripping when the LLM is unavailable or
        echoes the same query, so the retry is never a no-op loop.
        """
        query = state.get("kb_query") or state.get("rewritten_query") or state["message"]
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
        return {"kb_query": rewritten}

    @staticmethod
    def _keyword_fallback(query: str) -> str:
        """Heuristic retry query: strip question words and punctuation."""
        text = re.sub(r"(请问|帮我|我想问|怎么|如何|是什么|有没有)", " ", query)
        text = re.sub(r"[?？。！!,，、;；:：\"'`“”‘’（）()]+", " ", text)
        text = re.sub(r"(的|了|吗|呢|啊|呀|吧)$", "", text.strip())
        text = re.sub(r"\s+", " ", text).strip()
        return text or query

    async def kb_generate(self, state: AssistantState) -> dict[str, Any]:
        """Answer from confident chunks; refuse explicitly when there are none."""
        chunks = state.get("kb_chunks") or []
        meta_map = state.get("kb_meta_map") or {}
        query = state.get("kb_query") or state.get("rewritten_query") or state["message"]
        if not chunks:
            # Judge decided the knowledge base has nothing relevant: answer
            # without the LLM so noise can never be turned into fiction.
            # ACL-blocked uses the same generic wording on purpose — telling
            # the caller "相关资料存在但你无权查看" would leak document existence.
            # docs_meta 返回空列表: 接口不携带任何参考来源。
            answer = "未找到相关文档，无法回答该问题。建议联系对应部门或转人工咨询。"
            self._audit.log(
                state.get("trace_id") or "", "assistant", "kb_refused",
                {"query": query, "attempts": state.get("kb_attempt"),
                 "reason": "acl_blocked" if state.get("kb_acl_blocked") else "below_threshold"},
                state.get("session_id"),
            )
            return {"answer": answer, "route": "assistant_kb", "docs_meta": []}
        retriever = await self._get_retriever()
        context = retriever.format_context(chunks, meta_map)
        # 生成与检索语义对齐: 资料是按改写后的问题检索的, 生成也应以同一问题
        # 作答; 若发生过改写, 附上原话避免偏离用户真实问法。
        message = query if query == state["message"] else (
            f"{query}(用户原话: {state['message']})"
        )
        prompt = KB_ANSWER_PROMPT.format(
            context=context,
            history=state.get("history", "(无)"),
            message=message,
            current_time=self._now_text(state),
        )
        # 流式链路(SSE run): 逐 token 推送, 思考开启时额外透出 think; 否则保持
        # 一次性 ainvoke(非流式 /api/chat 调用方行为完全不变)。
        if self._stream_enabled(state):
            await self._emit_status(state, "generating", "基于知识库生成回答…")
            answer, think_text = await self._stream_answer(state, prompt)
        else:
            resp = await self._llm.ainvoke(prompt)
            answer, think_text = str(resp.content), ""
        self._audit.log(
            state.get("trace_id") or "", "assistant", "kb_answered",
            {"chunks": [c.chunk_id for c in chunks], "attempts": state.get("kb_attempt")},
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
        """Run a small ReAct loop over the target domain's MCP tools."""
        intent = state["intent"]
        target = intent.target if intent and intent.target else "hr"
        role = self._role_of(state)
        try:
            check_mcp_permission(role, target, "*")
        except PermissionDenied as exc:
            # 权限拒绝是合规上最该留痕的事件(与 acl_final_check_dropped 同等对待),
            # 转答复展示给用户的同时必须落审计, 否则拒绝记录只存在于用户界面。
            logger.warning("MCP 权限拒绝: role=%s target=%s err=%s", role.value, target, exc)
            self._audit.log(
                state.get("trace_id") or "", "assistant", "mcp_permission_denied",
                {"role": role.value, "target": target, "reason": str(exc)},
                state.get("session_id"),
            )
            return {"answer": f"权限不足:{exc}", "route": "mcp_tool", "target": target}

        all_tools = await get_mcp_pool().get_tools(target)
        # 权限Mask: 按角色×工具白名单矩阵过滤, 隐藏工具对 LLM 不可见、不可调。
        tools = filter_tools_for_role(role, target, all_tools)
        # 跨域基础解析能力(姓名->工号)注入: 用户只给姓名时先解析工号再调业务工具。
        tools = [*tools, lookup_employee_by_name]
        # Tool Cache: ReAct 循环里工具由 LLM 自主决定何时以何参数调用, 缓存逻辑
        # 只能下推到工具本身; 只读前缀白名单命中的工具会被包一层, 写操作工具
        # (create_*/cancel_* 等)原样传递, 不会被缓存。
        tools = wrap_tools_for_cache(tools, target, role.value)
        visible_names = [t.name for t in tools]
        self._audit.log(
            state.get("trace_id") or "", "assistant", "tools_filtered",
            {"server": target, "role": role.value, "visible_tools": visible_names},
            state.get("session_id"),
        )
        if not tools:
            return {
                "answer": f"权限不足: 角色 {role.value} 在 {target} 域无可用工具。",
                "route": "mcp_tool", "target": target,
            }

        agent = create_agent(self._llm, tools)
        # 身份/时间走 System 消息, 与用户请求文本分离; 并显式区分"当前操作者"
        # (登录态)与"任务目标用户"(消息中指定的他人), 否则 LLM 会把操作者
        # 工号误用作目标员工的查询参数(如"查张三的余额"却传了自己的工号)。
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={state.get('user_id') or 'anonymous'}; "
            f"当前时间={self._now_text(state)}。"
            "操作者身份仅代表登录态, 不等于任务目标用户: 若用户消息中指定了目标员工"
            "(工号/姓名), 以消息指定的为准; 仅当查询\"我/本人\"相关数据且未指定他人时, "
            "才默认使用操作者 employee_id。"
        )
        self._audit.log(state.get("trace_id") or "", "assistant", "mcp_dispatch", {"server": target}, state.get("session_id"))
        await self._emit_status(state, "tool", f"正在调用 {target} 域业务工具…")
        # 用消解后的独立问题驱动 ReAct: "审批到哪一步了" 已改写为
        # "FIN5000 审批到哪一步了", 工具才能拿到正确的查询对象。
        query = state.get("rewritten_query") or state["message"]
        result = await agent.ainvoke(
            {"messages": [("system", system_context), ("user", query)]}
        )
        answer = "工具调用未产生回复。"
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                answer = str(msg.content)
                break
        return {"answer": answer, "route": "mcp_tool", "target": target}

    async def agent_delegate(self, state: AssistantState) -> dict[str, Any]:
        """Delegate to a specialist agent over the A2A protocol.

        故意不接入 Tool Cache(对原方案的一处修正): AGENT_DELEGATE 按
        INTENT_PROMPT 的定义就是"需要专业系统多步办理的复杂业务"(如"我要报销"
        "帮我开在职证明"), 属于写/办理类操作, 缓存会把一次"提交成功"的响应
        复用给下一次本应真实发生的提交, 造成业务数据不一致; 而且下方构造的
        task 文本里含 `[当前时间=...]`, 天然每一轮都不同, 即使去接入缓存几乎
        也不会命中。
        """
        intent = state["intent"]
        target = intent.target if intent and intent.target else "hr"
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
            return {"answer": f"权限不足:{exc}", "route": "a2a_agent", "target": target}

        # 任务文本不含操作者工号: 身份经协议级 metadata 结构化下发(见下方 send,
        # 专业智能体只信 metadata), 由下游自行注入。文本中避免出现裸工号标签,
        # 防止下游 LLM 把"当前操作者"误当成"任务目标用户"(目标以消息文本为准)。
        # 用消解后的独立问题作为当前请求, 下游无需再做指代消解。
        query = state.get("rewritten_query") or state["message"]
        task = f"[当前时间={self._now_text(state)}] {query}"
        if state.get("history"):
            task = f"对话背景:\n{state['history']}\n\n当前请求: {task}"
        self._audit.log(state.get("trace_id") or "", "assistant", "a2a_delegate", {"agent": agent_name}, state.get("session_id"))
        await self._emit_status(state, "delegate", f"正在委派 {agent_name} 专业智能体办理…")
        # 可信身份经协议级 metadata 结构化下发 (而非文本标签), 供专业智能体做权限分级。
        answer = await get_a2a_pool().send(
            target,
            task,
            metadata={"user_id": state.get("user_id") or "", "role": role.value},
        )
        return {"answer": answer, "route": "a2a_agent", "target": target}

    async def chitchat(self, state: AssistantState) -> dict[str, Any]:
        prompt = DIRECT_PROMPT.format(current_time=self._now_text(state))
        rewritten = state.get("rewritten_query") or ""
        history = state.get("history") or ""
        parts = [prompt]
        # 传入对话历史: 即使改写失败回退原话, 模型仍能看到上下文。
        if history.strip():
            parts.append(f"对话历史:\n{history}")
        # 改写节点已把指代消解成独立问题; 若与原话不同则附上, 帮助模型理解上下文。
        if rewritten and rewritten != state["message"]:
            parts.append(
                f"用户: {state['message']}\n(结合对话历史, 该问题指的是: {rewritten})"
            )
        else:
            parts.append(f"用户: {state['message']}")
        full_prompt = "\n\n".join(parts)

        # 流式链路(SSE run): 逐 token 推送; 此时不走 Prompt Cache(缓存命中的
        # 重放没有思考过程, 且命中时几乎零延迟, 缓存收益小于体验损失)。
        if self._stream_enabled(state):
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
        )
        self._audit.log(
            state.get("trace_id") or "", "assistant", "turn_completed",
            {"route": state.get("route"), "answer_len": len(state["answer"]),
             "chat_message_id": message_id}, session_id,
        )
        return {"message_id": message_id}

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
                extraction = await extract_memories(message, answer)
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
        g.add_node("persist_memory", self.persist_memory)

        # rewrite_query 前置于意图识别: 分类器与全部四条路由共享消解后的
        # 独立问题, 避免"那帮我查一下它的余额"因指代未消解而误分类。
        g.add_edge(START, "build_context")
        g.add_edge("build_context", "resolve_time")
        g.add_edge("resolve_time", "rewrite_query")
        g.add_edge("rewrite_query", "classify_intent")
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
        for node in ("kb_generate", "tool_execute", "agent_delegate", "chitchat"):
            g.add_edge(node, "persist_memory")
        g.add_edge("persist_memory", END)
        return g.compile(checkpointer=checkpointer)

    # ---------------- public API ----------------

    async def _get_retriever(self) -> HybridRetriever:
        if self._retriever is None:
            self._retriever = HybridRetriever()
            await self._retriever.rebuild_bm25()
        return self._retriever

    async def refresh_knowledge(self) -> None:
        """Rebuild the ES BM25 index after a document (re)ingest (drift repair)."""
        if self._retriever is not None:
            await self._retriever.rebuild_bm25()
        # 文档重新入库后旧 chunk_id 可能已被删除/重建, Retrieval Cache 必须同步失效,
        # 否则会命中缓存里的脏 chunk_id(即使 TTL 未到)。
        await invalidate_all()

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
        """Handle one user turn end-to-end (non-streaming)."""
        await self.setup()  # 幂等: lifespan 已初始化过就直接返回
        trace_id = new_trace_id()
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

    async def handle_stream(self, req: ChatRequest) -> str:
        """流式入口: 后台任务跑图, 事件落 StreamHub, 立即返回 run_id。

        HTTP 连接与图执行解耦: 客户端断开(刷新/断网)不影响 run 继续跑,
        重连凭 run_id + Last-Event-ID 从断点重放并续流(app/assistant/stream.py)。
        """
        await self.setup()
        trace_id = new_trace_id()
        run_id = new_run_id()
        thinking = (
            req.thinking if req.thinking is not None else self._settings.llm_thinking_enabled
        )
        get_stream_hub().create(run_id)
        self._audit.log(
            trace_id, "user", "message_received",
            {"user_id": req.user_id, "role": req.role.value,
             "department": req.department, "message": req.message,
             "run_id": run_id, "thinking": thinking},
            req.session_id,
        )
        hub = get_stream_hub()

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

        task = asyncio.create_task(_pipeline(), name=f"chat-stream:{run_id}")
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)
        return run_id

    async def _run_graph(
        self, req: ChatRequest, trace_id: str, *, run_id: str, thinking: bool
    ) -> AssistantState:
        """执行一次完整图调用(流式/非流式共用同一状态初始化)。"""
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
            },
            # Working State Checkpoint: thread_id 用 session_id, 每轮对话都完整
            # 传入上方所有 AssistantState 字段, 不会与上一轮残留的 checkpoint
            # 状态串台(字段默认全量重置, 见 ``AssistantState``)。
            {"configurable": {"thread_id": req.session_id}},
        )

    def _build_response(
        self, req: ChatRequest, final: AssistantState, trace_id: str
    ) -> ChatResponse:
        """由图终态组装结构化响应(非流式返回体 / 流式 result 事件同源)。"""
        intent = final.get("intent") or IntentResult(intent=IntentType.CHITCHAT)
        return ChatResponse(
            session_id=req.session_id,
            answer=mask_text(final["answer"]),
            intent=intent.intent,
            route=final.get("route", "direct"),
            target=final.get("target"),
            trace_id=trace_id,
            message_id=final.get("message_id"),
            metadata={
                "confidence": intent.confidence,
                "reason": intent.reason,
                "docs": final.get("docs_meta", []),
            },
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
    """
    from app.tracing import init_tracing

    init_tracing()
    return get_orchestrator().ensure_graph_for_studio()
