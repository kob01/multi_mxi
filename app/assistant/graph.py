"""Assistant orchestration graph (LangGraph).

Routing policy (single-entry multi-agent):
    user -> [load_context] -> [resolve_time] -> [rewrite_query]
         -> [classify_intent] -> one of:
        knowledge_qa    -> kb_retrieve -> judge -> kb_generate   (confident hits)
                                            |-> kb_requery -> kb_retrieve (no-result retry)
                                            `-> refuse  (still empty -> fixed reply, no LLM)
        chitchat        -> chitchat
        tool_call       -> tool_execute   (Assistant -> MCP business tools)
        agent_delegate  -> agent_delegate (Assistant -> A2A specialist)
    -> [persist_memory] -> END

kb_retrieve runs hybrid search (Milvus dense + Elasticsearch BM25 -> RRF ->
rerank). The ONLY relevance cutoff is the rerank confidence threshold: hits
scoring below it are treated as noise, so an empty result means "no relevant
document". On the first miss the query is re-rewritten with a different
strategy (RETRY_REWRITE_PROMPT) and retrieved once more; if the second pass
is still empty the Assistant answers "未找到相关文档" directly (and returns
NO reference sources) instead of letting the LLM hallucinate over noise.

rewrite_query resolves pronouns/ellipsis ("那它的劣势呢" -> "XX 的劣势")
BEFORE intent classification, so the classifier routes on the resolved
standalone question and all four downstream routes share one
disambiguated query.

Every node writes an audit record under the same trace_id.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
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
from app.config import get_settings
from app.llm import get_chat_model
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
    history: str
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

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=0.3)
        self._intent = IntentRecognizer()
        self._memory = get_memory_store()
        self._audit = get_audit_logger()
        self._retriever: HybridRetriever | None = None
        self._graph = self._build_graph()

    # ---------------- graph nodes ----------------

    async def load_context(self, state: AssistantState) -> dict[str, Any]:
        history = self._memory.history_text(state.get("session_id") or "")
        return {"history": history}

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
            try:
                resp = await self._llm.ainvoke(
                    QUERY_REWRITE_PROMPT.format(history=history, message=message),
                    # Name the LLM run so the rewrite prompt/response is easy
                    # to locate in the LangSmith trace tree.
                    config={"run_name": "rewrite_query", "tags": ["rewrite_query"]},
                )
                rewritten = self._clean_rewrite(str(resp.content), message)
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
        # 统一身份主体: 检索阶段据此做 Metadata Filter 前置权限裁剪。
        principal = Principal(
            user_id=state.get("user_id") or "",
            department=state.get("department") or "",
            role=self._role_of(state),
        )
        children, score_mode = await retriever.retrieve(query, principal=principal)
        # Assemble hit child chunks into complete parent section blocks so
        # the LLM answers from full sections (with page/section citations).
        chunks = retriever.assemble_parents(children) if children else []
        # 最终授权校验 (纵深防御): 进入 Context Builder 前逐条复核, 拦截
        # 父块组装/索引脏数据可能引入的越权块; 无权块在拼接前剔除并审计。
        authorized: list[KnowledgeChunk] = []
        dropped_docs: set[str] = set()
        for c in chunks:
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
            except Exception as exc:  # MySQL down must not break chat
                logger.warning("doc metadata lookup failed, degrade to plain context: %s", exc)
        attempt = int(state.get("kb_attempt") or 0) + 1
        top_score = max((c.score for c in authorized), default=None)
        self._audit.log(
            state.get("trace_id") or "", "assistant", "kb_retrieved",
            {"attempt": attempt, "query": query, "chunks": [c.chunk_id for c in authorized],
             "scores": [c.score for c in authorized],
             "top_score": top_score, "dropped_by_acl": sorted(dropped_docs),
             "score_mode": score_mode,
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
        resp = await self._llm.ainvoke(prompt)
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
        return {"answer": str(resp.content), "route": "assistant_kb", "docs_meta": docs_meta}

    async def tool_execute(self, state: AssistantState) -> dict[str, Any]:
        """Run a small ReAct loop over the target domain's MCP tools."""
        intent = state["intent"]
        target = intent.target if intent and intent.target else "hr"
        role = self._role_of(state)
        try:
            check_mcp_permission(role, target, "*")
        except PermissionDenied as exc:
            return {"answer": f"权限不足:{exc}", "route": "mcp_tool", "target": target}

        all_tools = await get_mcp_pool().get_tools(target)
        # 权限Mask: 按角色×工具白名单矩阵过滤, 隐藏工具对 LLM 不可见、不可调。
        tools = filter_tools_for_role(role, target, all_tools)
        # 跨域基础解析能力(姓名->工号)注入: 用户只给姓名时先解析工号再调业务工具。
        tools = [*tools, lookup_employee_by_name]
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
        """Delegate to a specialist agent over the A2A protocol."""
        intent = state["intent"]
        target = intent.target if intent and intent.target else "hr"
        agent_name = f"{target}_agent"
        role = self._role_of(state)
        try:
            check_agent_permission(role, agent_name)
        except PermissionDenied as exc:
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
        resp = await self._llm.ainvoke("\n\n".join(parts))
        return {"answer": str(resp.content), "route": "direct"}

    async def persist_memory(self, state: AssistantState) -> dict[str, Any]:
        masked_answer = mask_text(state["answer"])
        await self._memory.append(state.get("session_id") or "", mask_text(state["message"]), masked_answer)
        self._audit.log(
            state.get("trace_id") or "", "assistant", "turn_completed",
            {"route": state.get("route"), "answer_len": len(state["answer"])}, state.get("session_id"),
        )
        return {}

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

    def _build_graph(self):
        g = StateGraph(AssistantState)
        g.add_node("load_context", self.load_context)
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
        g.add_edge(START, "load_context")
        g.add_edge("load_context", "resolve_time")
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
        return g.compile()

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

    async def handle(self, req: ChatRequest) -> ChatResponse:
        """Handle one user turn end-to-end."""
        trace_id = new_trace_id()
        self._audit.log(
            trace_id, "user", "message_received",
            {"user_id": req.user_id, "role": req.role.value,
             "department": req.department, "message": req.message},
            req.session_id,
        )
        final: AssistantState = await self._graph.ainvoke(
            {
                "message": req.message,
                "session_id": req.session_id,
                "user_id": req.user_id,
                "role": req.role,
                "department": req.department,
                "trace_id": trace_id,
                "history": "",
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
            }
        )
        intent = final.get("intent") or IntentResult(intent=IntentType.CHITCHAT)
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
                "_memory": self._memory.history_text(req.session_id),
            },
            req.session_id,
        )
        return ChatResponse(
            session_id=req.session_id,
            answer=mask_text(final["answer"]),
            intent=intent.intent,
            route=final.get("route", "direct"),
            target=final.get("target"),
            trace_id=trace_id,
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
    trace under the configured project.
    """
    from app.tracing import init_tracing

    init_tracing()
    return get_orchestrator()._graph
