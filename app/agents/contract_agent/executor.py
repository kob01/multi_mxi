"""Contract_Agent business logic + A2A AgentExecutor.

采购与合同初审专业智能体: 挂在 procurement MCP server 上的智能体, 已从"单循环 ReAct"
升级为显式 **LangGraph DAG 工作流**(LLM 推理 + 规则引擎 + 工作流编排), 范式平移自
finance_agent 第三代 Agentic。对外 A2A 契约(:class:`ContractAgentExecutor`、
:meth:`ContractAgent.invoke`)不变, 编排层 graph.py 零改动。

工作流节点(START -> structure -> [review | operate]):
- structure(json_mode): 抽取台账要素 + 条款锚点, 并分流"是否合同初审"; 非初审交办理型
  ReAct(带全部本域工具), 与升级前行为一致。
- rule_precheck(确定性, 非 LLM): 对**未脱敏原文**调 check_contract_clauses(进程内规则
  引擎, 见 app/procurement/rules.py)拿不可被模型漏判的红线清单(必备条款/高风险表述/
  供应商账号一致/金额分级/违约金超 30%)。规则侧永远看真实金额。
- chunk_review(分条款 + 有界并发 + 单块超时 + json_mode): 把送 LLM 的脱敏文本按条款切块
  逐块找规则漏掉的语义风险, **强制原文溯源**(quote 不在该块原文即丢弃), 缓解"中间遗忘";
  可选 RAG 挂载法规/模板做 few-shot(不可用即静默跳过)。
- aggregate(程序化汇总 + json_mode 只加分): 规则红线 + 已溯源语义风险合并去重, 风险等级
  取 max(规则, 模型)只升不降, 出口还原 PII 占位符, 产出结构化风险卡与待确认报告, 并
  submit_contract_review 以 PENDING_CONFIRM 落台账(登记 ≠ 放行, 最终确认走 HITL)。

安全边界(与其它智能体同源):
- 可逆 PII 脱敏: 送 LLM 的文本先 mask_round_trip, 出口 restore; 规则判定/原文定位用原文。
- 角色×工具白名单矩阵(app.security.auth.PROCUREMENT_TOOL_WHITELIST)硬控制可见工具;
  execute_sql 与 confirm_contract_review 仅管理角色。
- 身份只信 A2A Message.metadata, 缺失按 employee 最小权限; 用户文本里的角色字样一律忽略。
- RAG/HITL 落台账/脱敏全部"能降级就降级", 任一通道不可用只影响内容不阻断对话。

一键回滚: contract_workflow_enabled=false 即整轮退回单循环 ReAct(_legacy_invoke)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, TypedDict

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph

from app.agents.common_tools import take_lookup_tool, with_lookup_tool
from app.agents.contract_agent import prompts
from app.config import get_settings
from app.db.schema_docs import PROCUREMENT_SCHEMA_DDL
from app.llm import get_chat_model
from app.procurement import rules
from app.schemas import Role
from app.security.audit import get_audit_logger
from app.security.auth import filter_tools_for_role
from app.security.caller import (
    Caller,
    bind_caller_tools,
    caller_tool_args,
    reset_caller,
    set_caller,
)
from app.security.masking import mask_round_trip, restore

logger = logging.getLogger(__name__)

_EMPLOYEE_TAG_RE = re.compile(r"\[employee_id=([A-Za-z0-9_\-]+)\]")
# 条款起始: 第X条/章/节, 或 "一、二、" 中文序号, 或 "1." / "(1)" 数字编号。
_CLAUSE_SPLIT_RE = re.compile(
    r"(?=(?:第[一二三四五六七八九十百零〇\d]+[条章节])"
    r"|(?:[一二三四五六七八九十]+[、.])"
    r"|(?:[（(]?\d{1,2}[)）.、]\s))"
)
_LEVEL_TO_CN = {"low": "低", "medium": "中", "high": "高", "info": "低",
                "warning": "中", "critical": "高"}


def _clip(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出里稳健地抠出一个 JSON 对象(容忍 ``` 包裹与前后杂字)。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


def _parse_amount(value: Any) -> float:
    """把台账金额字段(可能含"6.5万元""65,000 元"等中文单位)解析为元(浮点); 解析不出返 0。"""
    text = str(value or "").strip()
    if not text:
        return 0.0
    num = re.sub(r"[^\d.]", "", text)
    if not num:
        return 0.0
    try:
        base = float(num)
    except ValueError:
        return 0.0
    if "亿" in text:
        return base * 1e8
    if "万" in text:
        return base * 1e4
    return base


_AMOUNT_TEXT_RE = re.compile(
    r"(?:合同金额|总金额|总价|价款|金额|合计)[^\d]{0,12}([\d,]+(?:\.\d+)?)\s*(万元|亿元|亿|万元|元)"
)


def _amount_from_text(text: str) -> float:
    """从合同**原文**确定性抽金额(元): 不依赖模型抽取, 避开脱敏后模型看不到数字的漏判。

    金额分级红线(SINGLE_SIGN_LIMIT)必须按真实金额判, 因此只从 original_text 取。
    """
    for match in _AMOUNT_TEXT_RE.finditer(text or ""):
        value = _parse_amount(f"{match.group(1)} {match.group(2)}")
        if value > 0:
            return value
    return 0.0


def _split_clauses(text: str, max_chars: int, max_chunks: int) -> list[str]:
    """按条款起始标记切块并聚合到 <= max_chars, 块数封顶 max_chunks。

    缓解"中间遗忘": 让每块语义自洽且长度可控; 无条款标记时退化为定长滑窗。
    """
    if not text:
        return []
    parts = [p for p in (seg.strip() for seg in _CLAUSE_SPLIT_RE.split(text)) if p]
    if not parts:
        parts = [text]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        while len(part) > max_chars:            # 单条超长(极少): 先冲缓冲再定长切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(part[:max_chars])
            part = part[max_chars:]
        if len(buf) + len(part) + 1 > max_chars and buf:
            chunks.append(buf)
            buf = part
        else:
            buf = f"{buf}\n{part}".strip() if buf else part
    if buf:
        chunks.append(buf)
    if len(chunks) > max_chunks:                # 触顶: 保留前段, 末块并入剩余尾巴(不丢内容)
        head, tail = chunks[: max_chunks - 1], "\n".join(chunks[max_chunks - 1:])
        head.append(tail)
        chunks = head
    return chunks


class ContractWorkflowState(TypedDict, total=False):
    """合同初审工作流的共享状态。"""

    user_text: str          # 用户原句(办理型/回退路径直接用)
    original_text: str      # 未脱敏原文(送规则引擎与定位)
    masked_text: str        # 送 LLM 的脱敏文本
    restore_map: dict[str, str]
    user_id: str
    role: Role
    trace_id: str
    is_review: bool
    ledger: dict[str, Any]
    amount: float
    clauses: list[dict[str, Any]]
    key_terms: dict[str, Any]
    rule_findings: list[dict[str, Any]]
    rule_risk_level: str
    rule_conclusion: str
    semantic_risks: list[dict[str, Any]]
    rag_reference: str
    merged_items: list[dict[str, Any]]
    model_risk_level: str
    final_opinion: str
    contract_no: str
    final: str


class ContractAgent:
    """合同初审 DAG 工作流 + 办理型 ReAct 回退, 角色感知。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=0)
        self._json_llm = get_chat_model(settings.llm_model, temperature=0, json_mode=True)
        self._tools: list[Any] | None = None
        self._tools_at = 0.0
        self._lookup: Any | None = None
        self._react_agents: dict[Role, Any] = {}
        self._graph: Any | None = None
        self._retriever: Any | None = None
        self._retriever_failed = False
        self._audit = get_audit_logger()

    def _ttl(self) -> float:
        return max(1.0, float(self._settings.mcp_tools_ttl))

    def _tool_by_name(self, name: str) -> Any | None:
        return next((t for t in (self._tools or []) if getattr(t, "name", "") == name), None)

    def _role_tools(self, role: Role) -> list[Any]:
        """办理型/回退 ReAct 工作者可见的工具集(角色硬控制 + 姓名解析 + caller 注入)。"""
        tools = filter_tools_for_role(role, "procurement", self._tools or [])
        tools = with_lookup_tool(tools, self._lookup)
        return bind_caller_tools(tools)

    async def _ensure_react_agent(self, role: Role) -> Any:
        if role in self._react_agents:
            return self._react_agents[role]
        agent = create_agent(
            self._llm,
            self._role_tools(role),
            system_prompt=prompts.LEGACY_SYSTEM_PROMPT.format(
                role_label=prompts.ROLE_LABELS[role],
                capabilities=prompts.ROLE_CAPABILITIES[role],
                schema=PROCUREMENT_SCHEMA_DDL,
            ),
        )
        self._react_agents[role] = agent
        return agent

    async def _ensure_tools(self) -> None:
        """按 TTL 惰性发现 procurement + hr MCP 工具; 清单变了就丢弃缓存的 agent/图。"""
        if self._tools is not None and time.monotonic() - self._tools_at <= self._ttl():
            return
        client = MultiServerMCPClient(
            {
                "procurement": {
                    "url": self._settings.procurement_mcp_url,
                    "transport": "streamable_http",
                },
                # 层 0(收回跨域凭证): 本进程不直连库, "姓名->工号"由数据属域(HR MCP)代做。
                "hr": {"url": self._settings.hr_mcp_url, "transport": "streamable_http"},
            }
        )
        self._tools = await client.get_tools(server_name="procurement")
        self._lookup = take_lookup_tool(await client.get_tools(server_name="hr"))
        self._tools_at = time.monotonic()
        self._react_agents.clear()
        self._graph = None

    # ------------------------------------------------------------------ RAG
    async def _retrieve_reference(self, query: str, principal_role: Role, user_id: str) -> str:
        """挂载法规/模板库做 few-shot(尽力而为, 任何不可用一律回空串, 绝不阻断)。

        注: contract-agent 容器按层 0 不持库/ES 凭据; 检索器初始化失败即标记不可用,
        本进程后续不再尝试。能连通时走与编排层同一份 HybridRetriever(自带 ACL 裁剪)。
        """
        if not self._settings.contract_rag_augment_enabled or self._retriever_failed:
            return ""
        try:
            if self._retriever is None:
                from app.rag.retriever import HybridRetriever

                retriever = HybridRetriever()
                await retriever.rebuild_bm25()
                self._retriever = retriever
            from app.security.acl import Principal

            chunks, _mode = await self._retriever.retrieve(
                query, top_n=4, principal=Principal(user_id=user_id or "", role=principal_role)
            )
            if not chunks:
                return ""
            return self._retriever.format_context(chunks)[:3000]
        except Exception as exc:  # noqa: BLE001  # 跨容器无凭据/服务不可达都属预期降级
            self._retriever_failed = True
            logger.info("合同审查 RAG 增强不可用, 跳过 enrich: %s", exc)
            return ""

    # ------------------------------------------------------------------ 节点
    async def _node_structure(self, state: ContractWorkflowState) -> dict[str, Any]:
        role = state["role"]
        prompt = prompts.STRUCTURE_PROMPT.format(max_chunk_chars=self._settings.contract_clause_chunk_chars)
        try:
            msg = await asyncio.wait_for(
                self._json_llm.ainvoke(
                    [("system", prompt), ("user", f"用户输入与可能的合同正文:\n{state['masked_text']}")]
                ),
                timeout=max(1.0, float(self._settings.contract_step_timeout)),
            )
            parsed = _parse_json(str(msg.content))
        except Exception as exc:  # noqa: BLE001  # 结构化失败降级为"按初审处理整篇单块", 不阻断
            logger.warning("structure 节点解析失败, 降级: %s", exc)
            parsed = None
        parsed = parsed or {}
        is_review = bool(parsed.get("is_review", True))
        ledger = parsed.get("ledger") if isinstance(parsed.get("ledger"), dict) else {}
        clauses = parsed.get("clauses") if isinstance(parsed.get("clauses"), list) else []
        key_terms = parsed.get("key_terms") if isinstance(parsed.get("key_terms"), dict) else {}
        self._audit.log(
            state["trace_id"], "contract_agent", "workflow_structured",
            {"is_review": is_review, "role": role.value, "clauses": len(clauses)},
        )
        return {"is_review": is_review, "ledger": ledger or {}, "clauses": clauses, "key_terms": key_terms or {}}

    async def _node_rule_precheck(self, state: ContractWorkflowState) -> dict[str, Any]:
        """对未脱敏原文跑进程内规则引擎(经 check_contract_clauses 工具, 服务端含供应商核验)。"""
        tool = self._tool_by_name("check_contract_clauses")
        ledger = state.get("ledger") or {}
        party_b = restore(str(ledger.get("party_b") or ""), state.get("restore_map") or {})
        # 金额以原文确定性抽取为准(脱敏后模型看不到数字); 抽不到再退回台账值。
        amount = _amount_from_text(state["original_text"]) or _parse_amount(
            restore(str(ledger.get("amount") or ""), state.get("restore_map") or {})
        )
        if not party_b:
            party_b = restore(
                str((state.get("key_terms") or {}).get("party_b") or ""), state.get("restore_map") or {}
            )
        rule_findings: list[dict[str, Any]] = []
        rule_risk_level = "低"
        rule_conclusion = ""
        try:
            raw = await tool.ainvoke({"content": state["original_text"], "amount": amount, "party_b": party_b})
            data = _coerce_dict(raw)
            if data and "error" not in data:
                rule_findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
                rule_risk_level = str(data.get("risk_level") or "低")
                rule_conclusion = str(data.get("conclusion") or "")
            elif data and data.get("error"):
                rule_conclusion = str(data["error"])
        except Exception as exc:  # noqa: BLE001  # 规则工具异常退到进程内规则兜底, 不阻断
            logger.warning("check_contract_clauses 调用失败, 进程内规则兜底: %s", exc)
            outcome = rules.precheck_contract(content=state["original_text"], amount=amount, party_b=party_b)
            rule_findings = [f.to_dict() for f in outcome.findings]
            rule_risk_level = outcome.risk_level
            rule_conclusion = outcome.conclusion
        self._audit.log(
            state["trace_id"], "contract_agent", "workflow_rule_precheck",
            {"risk_level": rule_risk_level, "findings": len(rule_findings)},
        )
        return {"rule_findings": rule_findings, "rule_risk_level": rule_risk_level, "rule_conclusion": rule_conclusion, "amount": amount}

    async def _node_chunk_review(self, state: ContractWorkflowState) -> dict[str, Any]:
        role = state["role"]
        query_bits = [str((state.get("ledger") or {}).get("title") or "")]
        query_bits += [str(c.get("heading") or c.get("no") or "") for c in (state.get("clauses") or [])[:6]]
        rag_reference = await self._retrieve_reference(
            " ".join(b for b in query_bits if b).strip() or "采购合同条款风险 违约责任 管辖 付款",
            role,
            state.get("user_id", ""),
        )
        chunks = _split_clauses(
            state["masked_text"],
            self._settings.contract_clause_chunk_chars,
            self._settings.contract_max_clause_chunks,
        )
        review_prompt = prompts.CLAUSE_REVIEW_PROMPT.format(
            role_label=prompts.ROLE_LABELS[role], rag_reference=rag_reference or "(无)"
        )
        sem = asyncio.Semaphore(max(1, self._settings.contract_chunk_review_concurrency))

        async def _review_chunk(chunk: str) -> list[dict[str, Any]]:
            async with sem:
                try:
                    msg = await asyncio.wait_for(
                        self._json_llm.ainvoke(
                            [("system", review_prompt), ("user", f"待审查条款分块原文:\n{chunk}")]
                        ),
                        timeout=max(1.0, float(self._settings.contract_step_timeout)),
                    )
                    parsed = _parse_json(str(msg.content)) or {}
                except Exception as exc:  # noqa: BLE001  # 单块失败只丢该块观察
                    logger.warning("clause chunk 审查失败(跳过该块): %s", exc)
                    return []
                out: list[dict[str, Any]] = []
                for r in parsed.get("risks") or []:
                    if not isinstance(r, dict):
                        continue
                    quote = str(r.get("quote") or "").strip()
                    # 强制溯源: 引用必须真在该块原文出现(去空白后包含判定), 命中不了即丢弃。
                    if not quote or _normalize(quote) not in _normalize(chunk):
                        continue
                    out.append({
                        "source": "semantic",
                        "clause_no": str(r.get("clause_no") or "").strip(),
                        "quote": quote,
                        "risk": str(r.get("risk") or "").strip(),
                        "level": _LEVEL_TO_CN.get(str(r.get("level") or "").lower(), "中"),
                        "suggestion": str(r.get("suggestion") or "").strip(),
                    })
                return out

        results = await asyncio.gather(*[_review_chunk(c) for c in chunks]) if chunks else []
        semantic_risks = [item for group in results for item in group]
        self._audit.log(
            state["trace_id"], "contract_agent", "workflow_chunk_review",
            {"chunks": len(chunks), "grounded_risks": len(semantic_risks), "rag_hit": bool(rag_reference)},
        )
        return {"semantic_risks": semantic_risks, "rag_reference": rag_reference}

    async def _node_aggregate(self, state: ContractWorkflowState) -> dict[str, Any]:
        merged = _merge_findings(state.get("rule_findings") or [], state.get("semantic_risks") or [])
        rule_level = state.get("rule_risk_level", "低")
        merged_items_text = _render_items_for_llm(merged)
        model_level = rule_level
        opinion = ""
        prompt = prompts.AGGREGATE_PROMPT.format(
            merged_items=merged_items_text or "(无已判定事项)", rule_risk_level=rule_level
        )
        try:
            msg = await asyncio.wait_for(self._json_llm.ainvoke([("system", prompt), ("user", "请给出结论。")]),
                                         timeout=max(1.0, float(self._settings.contract_step_timeout)))
            parsed = _parse_json(str(msg.content)) or {}
            cand = str(parsed.get("model_risk_level") or "").strip()
            opinion = str(parsed.get("opinion") or "").strip()
            # 只升不降: 模型等级不得低于规则红线。
            if cand in rules.RISK_ORDER and rules.RISK_ORDER[cand] >= rules.RISK_ORDER.get(rule_level, 0):
                model_level = cand
            else:
                model_level = rule_level
        except Exception as exc:  # noqa: BLE001  # 汇总失败退回规则结论
            logger.warning("aggregate 节点失败, 退回规则结论: %s", exc)
            model_level = rule_level
        if not opinion:
            opinion = state.get("rule_conclusion") or "已完成合同初审。"

        report_md = _render_report(state, merged, model_level, opinion)
        # 出口还原 PII 占位符(风险卡与意见面向用户)。
        rmap = state.get("restore_map") or {}
        report_md = restore(report_md, rmap)
        opinion = restore(opinion, rmap)

        merged_for_store = _restore_items(merged, rmap)
        contract_no = await self._persist_ledger(state, merged_for_store, model_level, opinion)
        final = report_md + _confirm_hint(contract_no, model_level)
        self._audit.log(
            state["trace_id"], "contract_agent", "workflow_aggregated",
            {"risk_level": model_level, "items": len(merged), "contract_no": contract_no},
        )
        return {"merged_items": merged, "model_risk_level": model_level,
                "final_opinion": opinion, "contract_no": contract_no, "final": final}

    async def _persist_ledger(self, state: ContractWorkflowState, merged: list[dict[str, Any]],
                              model_level: str, opinion: str) -> str:
        """以 PENDING_CONFIRM 落台账(登记 != 放行); 失败只退回"未落台账"的报告, 不阻断。"""
        tool = self._tool_by_name("submit_contract_review")
        if tool is None:
            return ""
        rmap = state.get("restore_map") or {}
        ledger = state.get("ledger") or {}
        amount = state.get("amount") or _amount_from_text(state["original_text"]) or _parse_amount(
            restore(str(ledger.get("amount") or ""), rmap)
        )
        args = {
            "title": restore(str(ledger.get("title") or "未命名合同"), rmap),
            "content": state["original_text"],
            "party_b": restore(str(ledger.get("party_b") or ""), rmap),
            "amount": amount,
            "party_a": restore(str(ledger.get("party_a") or ""), rmap),
            "category": restore(str(ledger.get("category") or "采购"), rmap),
            "sign_date": restore(str(ledger.get("sign_date") or ""), rmap),
            "effective_date": restore(str(ledger.get("effective_date") or ""), rmap),
            "expiry_date": restore(str(ledger.get("expiry_date") or ""), rmap),
            "pending_confirm": True,
            "risk_card": merged,
            **caller_tool_args(),
        }
        try:
            data = _coerce_dict(await tool.ainvoke(args))
            if data and not data.get("error"):
                return str(data.get("contract_no") or "")
            logger.info("合同落台账未成功(不阻断报告): %s", (data or {}).get("error"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("合同落台账异常(不阻断报告): %s", exc)
        return ""

    async def _node_operate(self, state: ContractWorkflowState) -> dict[str, Any]:
        """非初审诉求(办理/查询/统计): 走带全部本域工具的 ReAct, 与升级前一致。"""
        agent = await self._ensure_react_agent(state["role"])
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={state['user_id'] or 'anonymous'}; 当前角色 role={state['role'].value}。"
            "caller_* 字段由系统注入且会覆盖你填的值, 无需也不要在工具参数里传它们。"
        )
        # 办理型用未脱敏原句(工具要真实金额/供应商); set_caller 已在 invoke 完成。
        result = await agent.ainvoke({"messages": [("system", system_context), ("user", state["user_text"])]})
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                return {"final": str(msg.content)}
        return {"final": "采购合同智能体未能生成有效回复。"}

    def _route_after_structure(self, state: ContractWorkflowState) -> str:
        return "rule_precheck" if state.get("is_review") else "operate"

    def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        g = StateGraph(ContractWorkflowState)
        g.add_node("structure", self._node_structure)
        g.add_node("rule_precheck", self._node_rule_precheck)
        g.add_node("chunk_review", self._node_chunk_review)
        g.add_node("aggregate", self._node_aggregate)
        g.add_node("operate", self._node_operate)
        g.add_edge(START, "structure")
        g.add_conditional_edges("structure", self._route_after_structure, ["rule_precheck", "operate"])
        g.add_edge("rule_precheck", "chunk_review")
        g.add_edge("chunk_review", "aggregate")
        g.add_edge("aggregate", END)
        g.add_edge("operate", END)
        self._graph = g.compile()
        return self._graph

    # ------------------------------------------------------------------ 入口
    async def invoke(self, user_text: str, user_id: str, role: Role, trace_id: str = "unknown") -> str:
        """Run one delegated procurement/contract task under the given protocol-level identity."""
        await self._ensure_tools()
        token = set_caller(Caller(user_id=user_id or "", role=role.value, intent_text=user_text, trace_id=trace_id))
        try:
            if not self._settings.contract_workflow_enabled:
                return await self._legacy_invoke(user_text, user_id, role)
            masked_text, restore_map = (user_text, {})
            if self._settings.contract_pii_mask_enabled:
                masked_text, restore_map = mask_round_trip(user_text)
                self._audit.log(trace_id, "contract_agent", "pii_masked",
                                {"placeholders": len(restore_map)})
            graph = self._ensure_graph()
            state: ContractWorkflowState = {
                "user_text": user_text,
                "original_text": user_text,
                "masked_text": masked_text,
                "restore_map": restore_map,
                "user_id": user_id or "",
                "role": role,
                "trace_id": trace_id,
            }
            result = await graph.ainvoke(state)
            return result.get("final") or "采购合同智能体未能生成有效回复。"
        finally:
            reset_caller(token)

    async def _legacy_invoke(self, user_text: str, user_id: str, role: Role) -> str:
        """回滚路径: 工作流关闭时的单循环 ReAct(create_agent 全量单轮, 与升级前同构)。"""
        agent = await self._ensure_react_agent(role)
        system_context = (
            f"系统上下文(仅供调用工具时使用, 不要向用户复述): "
            f"当前登录操作者 employee_id={user_id or 'anonymous'}; 当前角色 role={role.value}。"
            "操作者身份仅代表登录态, 不等于业务目标用户: 若用户消息中指定了申请人(工号/姓名),"
            " 以消息指定的为准; 仅当代办\"我/本人\"的采购/送审且未指定他人时, 才默认使用操作者"
            " employee_id。caller_* 字段由系统注入且会覆盖你填的值, 无需也不要在工具参数里传它们。"
        )
        result = await agent.ainvoke({"messages": [("system", system_context), ("user", user_text)]})
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                return str(msg.content)
        return "采购合同智能体未能生成有效回复。"


# ---------------------------------------------------------------------- 纯函数(便于离线单测)
def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _restore_items(merged: list[dict[str, Any]], restore_map: dict[str, str]) -> list[dict[str, Any]]:
    """把风险卡各项的面向用户文本字段里的 PII 占位符还原(台账/HITL 展示要可读原文)。"""
    if not restore_map:
        return merged
    out: list[dict[str, Any]] = []
    for item in merged:
        new = dict(item)
        for key in ("quote", "risk", "detail", "suggestion", "item"):
            if isinstance(new.get(key), str):
                new[key] = restore(new[key], restore_map)
        out.append(new)
    return out


def _coerce_dict(raw: Any) -> dict[str, Any] | None:
    """把 MCP 工具返回(可能是内容块列表/JSON 串/dict)规整成 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        raw = "".join(str(b.get("text", "")) for b in raw if isinstance(b, dict)).strip()
    if isinstance(raw, str):
        return _parse_json(raw)
    return None


def _merge_findings(rule_findings: list[dict[str, Any]], semantic_risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并规则红线与已溯源语义风险: 语义项按(条款,引用)去重, 规则项优先保留。"""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for f in rule_findings:
        item = {
            "source": "rule",
            "item": str(f.get("item") or ""),
            "level": _LEVEL_TO_CN.get(str(f.get("level") or "").lower(), str(f.get("level") or "")),
            "raw_level": str(f.get("level") or ""),
            "detail": str(f.get("detail") or ""),
            "suggestion": str(f.get("suggestion") or ""),
            "clause_no": "",
            "quote": "",
        }
        merged.append(item)
    for r in semantic_risks:
        key = (_normalize(r.get("clause_no", "")), _normalize(r.get("quote", "")))
        if key in seen:
            continue
        seen.add(key)
        merged.append({**r, "source": "semantic", "item": r.get("risk") or "语义风险", "detail": r.get("risk") or ""})
    return merged


def _render_items_for_llm(merged: list[dict[str, Any]]) -> str:
    lines = []
    for i, m in enumerate(merged, 1):
        tag = "规则红线" if m.get("source") == "rule" else "语义风险"
        lines.append(f"[{i}]({tag}·{m.get('level', '')}) {m.get('item', '')}: {m.get('detail', '')}")
    return "\n".join(lines)


def _render_report(state: ContractWorkflowState, merged: list[dict[str, Any]],
                   risk_level: str, opinion: str) -> str:
    """程序化拼装结构化风险卡(不过 LLM, 防改写/编造): [风险等级]+[风险点]+[原文定位]+[建议]。"""
    ledger = state.get("ledger") or {}
    head = [
        f"## 合同初审结论",
        f"- 合同名称: {ledger.get('title') or '(未名)'}",
        f"- 综合风险等级: **{risk_level}**",
        "",
        "### 风险清单",
    ]
    if not merged:
        head.append("- 未命中规则红线, 也未发现需溯源的语义风险。")
    for i, m in enumerate(merged, 1):
        loc = m.get("quote") or m.get("clause_no") or "(规则项, 见全文)"
        suggestion = m.get("suggestion") or "-"
        head.append(
            f"{i}. [{m.get('level', '')}] {m.get('item', '')}\n"
            f"   - 依据: {m.get('detail', '') or '(见下)'}\n"
            f"   - 原文定位: {loc}\n"
            f"   - 修改建议: {suggestion}"
        )
    head.append("")
    head.append(f"### 初审意见\n{opinion}")
    return "\n".join(head)


def _confirm_hint(contract_no: str, risk_level: str) -> str:
    tail = (
        "\n\n> 初审为**初筛建议**, 已登记为**待确认**; 最终放行由法务/财务人工决定。"
        "管理角色可用 `确认 / 修改 / 驳回` 对该结论做 HITL 处置。"
    )
    if contract_no:
        return f"\n\n（台账合同号: {contract_no}，状态: PENDING_CONFIRM，风险: {risk_level}）{tail}"
    return f"\n\n（本次未落台账，可在确认合同要素后用 `submit_contract_review` 归档）{tail}"


class ContractAgentExecutor(AgentExecutor):
    """A2A AgentExecutor bridge: A2A task -> ContractAgent invocation."""

    def __init__(self) -> None:
        self._agent = ContractAgent()
        self._audit = get_audit_logger()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        trace_id = context.task_id or context.context_id or "unknown"

        # 身份只信协议级 metadata; 用户文本中的角色字样一律忽略, 防伪造。
        metadata: dict[str, Any] = getattr(context.message, "metadata", None) or {}
        user_id = str(metadata.get("user_id") or "")
        if not user_id:
            fallback = _EMPLOYEE_TAG_RE.search(user_input)
            user_id = fallback.group(1) if fallback else ""
        try:
            role = Role(str(metadata.get("role") or Role.EMPLOYEE.value))
        except ValueError:
            role = Role.EMPLOYEE

        self._audit.log(
            trace_id, "contract_agent", "a2a_task_received",
            {"input": _clip(user_input, 200), "user_id": user_id or "unknown", "role": role.value},
        )
        try:
            answer = await self._agent.invoke(user_input, user_id=user_id, role=role, trace_id=trace_id)
        except Exception as exc:  # noqa: BLE001
            answer = f"采购合同智能体处理失败: {exc}"
        self._audit.log(trace_id, "contract_agent", "a2a_task_completed", {"answer": answer})
        await event_queue.enqueue_event(new_agent_text_message(answer))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """取消未实现: 正在跑的工作流没有可中断点(委派靠 a2a_timeout 兑底)。"""
        raise NotImplementedError(
            "Contract_Agent 不支持 A2A cancel: 编排层不得依赖取消来回收资源"
        )
