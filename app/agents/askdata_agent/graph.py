"""问数状态机: PARSE → LINK → (CLARIFY?) → PLAN → VALIDATE → ROUTE → VERIFY → RENDER → FINALIZE。

为什么是确定性状态机而不是自由 ReAct(与 Analyst_Agent 的最大区别):
每一步的输入输出形状固定, 于是
- **可单测**: 每个节点都能拿一份 fixture 状态断言产出(见 scripts/test_askdata_offline.py);
- **可回放**: 中间产物(槽位/候选/DSL/SQL)全部落进 askdata_query_log, badcase 能定位到段;
- **可归因**: 失败发生在哪个节点是结构事实, 不用反过来猜模型为什么这么答。
自由 ReAct 只保留一个有界的入口: ``verify`` 判定结果异常后的自修复循环(默认 ≤2 轮)。

两个刻意的合并, 都为了少一套口径:
- ``ROUTE`` 内含执行: askdata MCP 的工具本身就是"编译 + 执行"一体的(编译产物不外发),
  再拆成两个节点只会让"哪一步失败"更难说清;
- ``RENDER`` 内含叙述: 图型由结果 shape 规则决定, LLM 只补一段人话结论。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph

from app.agents.askdata_agent import prompts
from app.config import get_settings
from app.llm import get_chat_model
from app.observability import otel
from app.security.caller import caller_tool_args
from app.semantic import timesem
from app.semantic.dsl import DslError, parse_dsl
from app.semantic.dsl import render_schema_hint
from app.semantic.resultcache import SEMANTIC_CACHE_MARKER
from app.semantic.timesem import TIME_CHOICES

logger = logging.getLogger(__name__)

PAYLOAD_FENCE = "askdata"
# 校验失败后的回炉上限改为配置驱动(方案要 ≤2; 以前写死 1 轮)。
_REPAIR_TOOLS = ("list_metrics", "link_terms", "validate_dsl", "exec_sql_sandbox")
# 预算熔断之后还要不要把已经算出的数交出去: 要(带"存疑"标注)。超预算不是数据有问题。
CONFIRM_PREFIX = "问数确认："

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


class AskDataState(TypedDict, total=False):
    """状态机流转的共享状态(每个键都属于某个阶段的产物, 便于逐段回放)。"""

    question: str
    prior_slots: dict[str, Any]
    prior_dsl: dict[str, Any]
    prior_top_metric: str
    user_id: str
    role: str
    trace_id: str
    session_id: str
    started_at: float
    slots: dict[str, Any]
    candidates: list[dict[str, Any]]
    enum_filters: dict[str, str]
    catalog_origin: str
    vector_channel: str
    semver: str
    examples: list[dict[str, Any]]
    cached_dsl: dict[str, Any]
    cache_similarity: float
    dsl: dict[str, Any]
    rewrite_count: int
    token_cost: int
    llm_calls: int
    error_hint: str
    validate_result: dict[str, Any]
    exec_result: dict[str, Any]
    route: str
    cache_hit: bool
    attempts: list[dict[str, Any]]
    verify: dict[str, Any]
    repair_text: str
    chart: dict[str, Any]
    narrative: str
    answer: str
    payload: dict[str, Any]
    verdict: str
    error_codes: list[str]
    notes: list[str]
    budget_reason: str
    ticket_id: str


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里稳健地抠出一个 JSON 对象(容忍 fenced block 与前后杂字)。"""
    if not text:
        return None
    cleaned = _JSON_FENCE_RE.sub("", text.strip())
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _usage_tokens(resp: Any) -> int:
    """从一条 LangChain 响应里取 token 总量(拿不到就 0, 不影响回答)。

    为什么要自己抠: 预算熔断(G3)与成本看板都吃这个数, 而不同提供商给的位置不一样
    (usage_metadata / response_metadata.token_usage / llm_metadata), 只读一个就会永远拿到 0。
    """
    for attr in ("usage_metadata", "response_metadata", "llm_output"):
        block = getattr(resp, attr, None)
        if not isinstance(block, dict):
            continue
        total = block.get("total_tokens")
        if isinstance(total, (int, float)):
            return int(total)
        prompt = block.get("input_tokens")
        if prompt is None:
            prompt = block.get("prompt_tokens")
        completion = block.get("output_tokens")
        if completion is None:
            completion = block.get("completion_tokens")
        if isinstance(prompt, (int, float)) or isinstance(completion, (int, float)):
            return int(prompt or 0) + int(completion or 0)
        for nested in ("usage", "token_usage", "llm_usage"):
            sub = block.get(nested)
            if isinstance(sub, dict):
                if isinstance(sub.get("total_tokens"), (int, float)):
                    return int(sub["total_tokens"])
                return int(sub.get("prompt_tokens") or 0) + int(sub.get("completion_tokens") or 0)
    return 0


def _elapsed_ms(state: AskDataState) -> int:
    return int((time.monotonic() - float(state.get("started_at") or time.monotonic())) * 1000)


def _budget_breach(state: AskDataState) -> str:
    """单轮预算熔断判据(方案 §3): 超 token 或超墙钟就不要再往下烧。"""
    settings = get_settings()
    max_tokens = int(settings.askdata_budget_max_tokens or 0)
    if max_tokens > 0 and int(state.get("token_cost") or 0) > max_tokens:
        return f"本轮已花 token {state.get('token_cost')} 超过单轮上限 {max_tokens}"
    max_ms = int(settings.askdata_budget_max_latency_ms or 0)
    if max_ms > 0 and _elapsed_ms(state) > max_ms:
        return f"本轮已耗时 {_elapsed_ms(state)}ms 超过单轮上限 {max_ms}ms"
    return ""


_TOPN_RE = re.compile(
    r"(最高的\s*(\d+)|最多的\s*(\d+)|最大的\s*(\d+)|最低的\s*(\d+)|最少的\s*(\d+)"
    r"|最小的\s*(\d+)|top\s*(\d+)|前\s*(\d+)(?!\s*个?\s*[月天日年周季]))", re.I,
    # 时间窗口的"前 3 个月/前 30 天"不是 topN: 这个否定预查必须**紧接在数字后面**
    # (拿 `\s*` 先吃掉空格再预查会被正则回溯绕过, 实测就是这样误把"前 3 个月"当成取前 3 行)。
)


_EXPLICIT_DATE_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日")


def _reconcile_time(dsl: dict[str, Any], slots: dict[str, Any],
                    question: str = "") -> tuple[dict[str, Any], str]:
    """相对区间词与显式日期同时出现时, 按**用户实际说了哪种**留一个。

    为什么不沿用"显式日期优先"的默认行为: 本轮实测到模型会交回
    `{"range": "this_month", "start": "2026-09-01", "end": "2026-09-30"}` —— 用户说的是
    "本月", 而当时已是 10 月: 提示词里写了可以用 start/end, 但没告诉模型今天是几号,
    它就只能自己编一个看着合理的月份(golden 里其实一条硬日期都没有, 不是抄示例)。
    默认行为下这变成一份"口径看着对、区间默错一个月"的空结果, 然后叙述会把错区间再说一遍。
    拿不定时(原话既没相对词也没日期)保留原有优先级, 交给校验与澄清门。

    返回 ``(新 DSL, 说明)``; 没改动时说明为空串。
    """
    time_block = dsl.get("time")
    if not isinstance(time_block, dict):
        return dsl, ""
    token = str(time_block.get("range") or "").strip()
    start, end = str(time_block.get("start") or ""), str(time_block.get("end") or "")
    if not (start or end) and token:
        return dsl, ""      # 只有区间词: 本来就是想要的形状
    if not (start or end):
        return dsl, ""
    phrase = str(((slots or {}).get("time") or {}).get("phrase") or "").strip()
    # 判"用户说了什么"只能看**原话**: 槽位里的 phrase 也是模型写的, 它会把它算出的日期
    # 当成"用户说的说法"填回来(本轮实测: 用户说"本月"而 slots.time.phrase="2026-09-01~2026-09-30"),
    # 拿它当依据就等于是拿模型的错自证模型的错。
    detected = timesem.detect_phrase(question or "")
    haystack = detected or phrase
    out = dict(dsl)
    new_time = dict(time_block)

    if not token:
        # 原话里就写着日期: 这是用户自己给的区间, 不动也不说明。
        # (先判这条再去猜区间词: 否则"2026-09-01 到 2026-10-01"这种句子里碰巧出现了
        # 词表子串时, 会把用户明写的日期换成一个相对区间 —— 那是另一个方向的错)
        if _EXPLICIT_DATE_RE.search(question or ""):
            return dsl, ""
        # 只有硬日期而用户说的其实是相对区间: 用区间词替掉自己算的日期
        # (这才是最阴的一种: 没有区间词可供对比, 对账逻辑一开始漏了这条分支,
        # 导致"本月"被静默算成上个月整月, 而且结果看上去完全自洽)
        for guess in (detected, phrase):
            if not guess:
                continue
            try:
                wanted = timesem.normalize_range(guess)
            except timesem.TimeError:
                wanted = ""
            if wanted and wanted != "custom":
                new_time["range"] = wanted
                new_time["start"] = ""
                new_time["end"] = ""
                out["time"] = new_time
                return out, (
                    f"你说的是「{guess}」, 本次按相对区间 {wanted} 算"
                    f"(模型自己换算的 {start or '?'}~{end or '?'} 已不再使用)"
                )
        return dsl, ""

    # 三条判据的**固定优先级**(原话 > 槽位 > 模型自算的日期):
    # 1. 原话里写着日期 -> 日期赢(去掉碰巧从日期串里猜到的区间词);
    # 2. 原话里有区间词 -> 区间词赢(删掉模型自己换算的日期);
    # 3. 两样都没证据 -> 不猜, 交给校验与澄清门。
    if _EXPLICIT_DATE_RE.search(question or ""):
        # 用户说的是日期: 删掉模型顺手补的区间词
        new_time["range"] = ""
        out["time"] = new_time
        return out, f"你说了具体日期({start}~{end}), 本次按该区间算(已去掉同时给出的区间词 {token})"
    if detected:
        # 用户说的是相对区间("本月/今年/近 30 天"): 删掉跟着的硬日期
        new_time["start"] = ""
        new_time["end"] = ""
        out["time"] = new_time
        return out, (
            f"你说的是「{detected}」, 本次按相对区间算; "
            f"模型同时给出的具体日期 {start or '?'}~{end or '?'} 已丢弃(它们不来自你的原话)"
        )
    if phrase and not _EXPLICIT_DATE_RE.search(phrase):
        # 原话里拿不到证据但槽位给了一个非日期的说法: 拿它试一次, 猜不出就不动
        try:
            timesem.normalize_range(phrase)
        except timesem.TimeError:
            return dsl, ""
        new_time["start"] = ""
        new_time["end"] = ""
        out["time"] = new_time
        return out, f"按你说的「{phrase}」算, 模型自己补的日期已丢弃"
    # 三条判据都没证据: 必须显式返回原样。漏掉这条时函数会隐式返回 None,
    # 而调用方按 `(dsl, note)` 解包 -> TypeError 把整轮问数直接抬死(测试抓到的真错)。
    return dsl, ""


def _drop_invented_filters(dsl: dict[str, Any], question: str,
                          candidates: list[dict[str, Any]],
                          enum_filters: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """丢掉用户根本没说的过滤条件(只允许三种来处: 原话 / 枚举反查 / 语义层命中的取值)。

    为什么必需: 本轮实测到问"本月报销总额是多少"时, DSL 里多了
    `filters: [d_expense_employee_name = 张伟]` —— 模型把"当前这个人的名字"当成"用户想看的范围"
    静静加进了查询。结果仍然自洽、仍然能执行, 但**口径已经被换了**: finance 角色本来能看全员,
    却收到一个只算一个人的总数。这类错不会触发任何校验器(它是合法维度+合法取值),
    只能靠"取值必须能在输入里找到来处"这道确定判据拦住。

    不误杀合法映射的关键: 枚举型候选(link 己经把"已打款"→PAID 这类映射算好了)与
    枚举反查结果都进允许集, 所以按码值写的过滤不会被误删。

    返回 ``(新 DSL, 被丢的过滤描述列表)``。
    """
    filters = dsl.get("filters")
    if not isinstance(filters, list) or not filters:
        return dsl, []
    haystack = _norm_text(question)
    allowed: set[str] = set()
    for item in candidates or []:
        if isinstance(item, dict) and str(item.get("kind") or "") == "enum":
            for token in (str(item.get("value") or ""), str(item.get("matched") or "")):
                if token:
                    allowed.add(_norm_text(token))
    for item in (enum_filters or {}).values() if isinstance(enum_filters, dict) else []:
        for token in ([str(item)] if isinstance(item, (str, int, float)) else
                      [str(getattr(entry, "value", entry)) for entry in (item or [])]):
            if token:
                allowed.add(_norm_text(token))

    kept: list[Any] = []
    dropped: list[str] = []
    for filt in filters:
        if not isinstance(filt, dict):
            kept.append(filt)
            continue
        op = str(filt.get("op") or "eq")
        if op in ("is_null", "not_null"):
            kept.append(filt)
            continue
        raw_value = filt.get("value")
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        probes = [_norm_text(str(v)) for v in values if str(v or "").strip()]
        if not probes or any(p in haystack or p in allowed for p in probes):
            kept.append(filt)
        else:
            dropped.append(f"{filt.get('dim')}={raw_value}")
    if not dropped:
        return dsl, []
    out = dict(dsl)
    out["filters"] = kept
    return out, dropped


def _apply_topn(dsl: dict[str, Any], slots: dict[str, Any], question: str = "") -> dict[str, Any]:
    """用户说了"最高的 N 个/前 N 名"但 DSL 没写 order_by/limit 时, 确定性补齐。

    为什么不能只靠 prompt: 这是"说了却没做到"的一类错, 模型在同一个句子上会时做时不做;
    而 N 与方向都在原话里, 用正则拿比拿重采样便宜且稳定。不猜 N: 原话里没数字就不动。
    为什么连**原话**也要扫: order_hint 是模型抽出来的槽位, 它不填并不代表用户没说
    (本轮实测 topN 类只有一半做对了补齐, 丢的就是没填 order_hint 的那批); 原话才是不会撒谎的来处。
    方向与 limit 都**以用户说的 N 为准**: 模型写了另一个数时覆盖它(本轮实测用户说 3 个部门、
    模型给 limit=5 → "不覆盖更严的值"这条旧规则等于把用户的规格让位给模型的猜测)。
    只覆盖 limit, 不动模型已给的 order_by(它可能按另一个指标排, 那是选择不是错)。
    """
    hint = " ".join([
        str(slots.get("order_hint") or ""),
        json.dumps(slots.get("metric_terms") or [], ensure_ascii=False),
        str((slots.get("time") or {}).get("phrase") or ""),
        question or "",
    ])
    match = _TOPN_RE.search(hint)
    if not match:
        return dsl
    number = next((group for group in match.groups()[1:] if group), "")
    try:
        size = int(number)
    except (TypeError, ValueError):
        return dsl
    if size <= 0 or size > 200:
        return dsl
    metrics = [item for item in (dsl.get("metrics") or []) if isinstance(item, dict)]
    if not metrics:
        return dsl
    lowered = hint.lower()
    direction = "asc" if ("最低" in lowered or "最少" in lowered or "最小" in lowered) else "desc"
    primary = str(metrics[0].get("id") or "").replace("-", "_")
    if not primary:
        return dsl
    out = dict(dsl)
    if not str(out.get("order_by") or "").strip():
        out["order_by"] = f"{primary} {direction}"
    if int(out.get("limit") or 0) != size:
        out["limit"] = size
        out["_topn_fixed"] = True
    return out


def _md_table(columns: list[str], labels: dict[str, str], rows: list[dict[str, Any]], limit: int = 20) -> str:
    """结果表(Markdown); 只列前 ``limit`` 行, 行数在正文里说明(不要把 200 行塞进对话)。"""
    if not columns or not rows:
        return ""
    head = "| " + " | ".join(labels.get(c, c) for c in columns) + " |"
    sep = "|" + "|".join(" --- " for _ in columns) + "|"
    lines = [head, sep]
    for row in rows[:limit]:
        cells = []
        for column in columns:
            value = row.get(column)
            if isinstance(value, float):
                cells.append(f"{value:,.2f}".rstrip("0").rstrip("."))
            elif value is None:
                cells.append("-")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


class AskDataPipeline:
    """一台跑在 askdata MCP server 之上的确定性问数流水线(按角色缓存图)。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(settings.llm_model, temperature=0)
        self._json_llm = get_chat_model(settings.llm_model, temperature=0, json_mode=True)
        self._tools: dict[str, Any] = {}
        self._tools_at = 0.0
        self._repair_agents: dict[str, Any] = {}

    # ---------------- 工具面 ----------------
    def _ttl(self) -> float:
        return max(1.0, float(self._settings.mcp_tools_ttl))

    async def _ensure_tools(self) -> dict[str, Any]:
        """连 askdata MCP server 拿工具(与 Analyst_Agent 同一 TTL 口径, 永不"一次发现定终身")。"""
        if self._tools and time.monotonic() - self._tools_at <= self._ttl():
            return self._tools
        client = MultiServerMCPClient(
            {"askdata": {"url": self._settings.askdata_mcp_url, "transport": "streamable_http"}}
        )
        tools = await client.get_tools()
        self._tools = {getattr(t, "name", ""): t for t in tools}
        self._tools_at = time.monotonic()
        self._repair_agents.clear()
        return self._tools

    async def _call(self, name: str, **args: Any) -> dict[str, Any]:
        """程序化调用一个 MCP 工具(状态机自己决定调什么, 不由模型挑工具)。

        caller_* 用 :func:`caller_tool_args` 现取现注: 这是全仓注入字段的唯一来源,
        在这里再走一遍比让模型填要可靠得多(它填了也会被覆盖, 但覆盖这件事不该依赖模型)。

        每一次工具调用一条 OTel span(方案 §7 要求每个模块可观测): 未启用时是哑上下文,
        拿不到 SDK 也不能影响调用。
        """
        tools = await self._ensure_tools()
        tool = tools.get(name)
        if tool is None:
            return {"error": f"askdata MCP server 未提供工具 {name}"}
        payload = {**args}
        payload.update(caller_tool_args())
        with otel.span(f"tool.{name}", {"tool": name}) as extra:
            started = time.monotonic()
            raw = await tool.ainvoke(payload)
            out = _as_dict_result(raw)
            extra["duration_ms"] = time_ms(started)
            extra["ok"] = not out.get("error")
            if out.get("error"):
                extra["error"] = str(out["error"])[:180]
            if out.get("candidates"):
                # 只带分数结构不带候选文本: 候选 label 是口径名(元数据)但包里的 caliber
                # 文本很长, 链路系统里不需要它(看留痕表就有)。
                extra["recall"] = [
                    round(float(c.get("score") or 0), 3) for c in out["candidates"][:5]
                ]
            if out.get("semver"):
                extra["semver"] = out["semver"]
            return out

    # ---------------- 节点 ----------------
    async def recall(self, state: AskDataState) -> dict[str, Any]:
        """语义缓存(V3): 先拿原话去 golden 里碰一碰同版已标注的问法。

        命中就**跳过两次 LLM 调用**(槽位抽取 + DSL 生成)但绝不跳过校验: 后面的
        link/validate/route/verify 全跑, 所以复用的只是"意图形状", 口径错、指标被停、
        权限不够仍旧会在这里被拦下。没命中就是多一次 pgvector 查询的代价。
        """
        settings = self._settings
        question = state.get("question", "") or ""
        if not settings.askdata_semantic_cache_enabled or question.startswith(CONFIRM_PREFIX):
            # 澄清点选那一轮的真意在"上一轮定稿 + 本轮选择"的合并里, 拿原文去碰缓存
            # 会复用成错的那一条(它匹配的是确认句式而不是用户真正想问的口径)。
            return {}
        out = await self._call("similar_intent", question=question,
                              trace_id=state.get("trace_id", ""))
        if not out.get("matched"):
            return {}
        dsl = out.get("dsl")
        if not isinstance(dsl, dict) or not dsl.get("metrics"):
            return {}
        return {
            "cached_dsl": dsl,
            "dsl": dsl,
            "cache_similarity": float(out.get("similarity") or 0),
            "semver": out.get("semver") or state.get("semver") or "",
            "notes": list(state.get("notes") or []) + [
                f"本轮{SEMANTIC_CACHE_MARKER}的查询意图(相似度 {float(out.get('similarity') or 0):.3f}, "
                f"用例 {out.get('case_id')}); 校验与取数仍按完整链路跑"
            ],
        }

    async def parse(self, state: AskDataState) -> dict[str, Any]:
        """① 意图理解: 抽槽位(带 confidence), 并用上一轮槽位与 DSL 做确定性继承。"""
        started = time.monotonic()
        prior = state.get("prior_slots") or {}
        prior_dsl = state.get("prior_dsl") or {}
        prompt = prompts.SLOT_PROMPT.format(
            today=prompts.today_anchor(),
            question=state.get("question", ""),
            prior_slots=json.dumps(prior, ensure_ascii=False) if prior else "(无)",
            # 上一轮的 DSL 比裸槽位信息量高: 它带的是最终选定的指标 id 与已确定的过滤条件,
            # "换成按部门看"这类指代只有拿到它才能正确改写而不是重新猜一遍。
            prior_dsl=json.dumps(_compact_dsl(prior_dsl), ensure_ascii=False) if prior_dsl else "(无)",
        )
        slots: dict[str, Any] = {}
        tokens = 0
        try:
            resp = await self._json_llm.ainvoke(prompt)
            slots = extract_json(str(resp.content)) or {}
            tokens = _usage_tokens(resp)
        except Exception as exc:  # noqa: BLE001 - 抽取失败退化成"什么都没抽到", 由澄清门接管
            logger.warning("[askdata] 槽位抽取失败(交给澄清门): %s", exc)
        merged = _inherit(slots, prior)
        merged = _prune_invented_terms(merged, state.get("question", ""))
        merged = _recover_time_phrase(merged, state.get("question", ""))
        recovered = str(merged.get("time_recovered") or "")
        dropped = list(merged.get("invented_dropped") or [])
        update: dict[str, Any] = {
            "slots": merged,
            "started_at": state.get("started_at") or time.monotonic(),
            "token_cost": int(state.get("token_cost") or 0) + tokens,
            "llm_calls": int(state.get("llm_calls") or 0) + 1,
        }
        # 两类信号都进 notes, 但**只能给一次** update["notes"]: LangGraph 只合并返回字典里的
        # 键, 写两次就是后者覆盖前者(时隐时现的丢注记最难查)。
        recovered_notes: list[str] = []
        if recovered:
            recovered_notes.append("槽位模型漏了时间说法, 已按原话里的区间词确定性补上: " + recovered)
        if dropped:
            # 剔词本身就是一个信号(模型在这句话上没把握), 进 notes 供归因回查。
            recovered_notes.append(
                "槽位里有 " + str(len(dropped)) + " 个说法不是原话, 已剔除: " + ", ".join(dropped[:4])
            )
        if recovered_notes:
            update["notes"] = list(state.get("notes") or []) + recovered_notes
        _track(state, f"槽位抽取完成({time_ms(started)}ms)")
        return update

    async def link(self, state: AskDataState) -> dict[str, Any]:
        """② 语义链接: 三路召回(词典/向量/枚举), 产出候选与分数。"""
        started = time.monotonic()
        slots = state.get("slots") or {}
        phrases = _phrase_of(slots)
        out = await self._call("link_terms", text=phrases or state.get("question", ""),
                              phrase_conf_json=_slot_confidence(slots))
        if not out.get("candidates") and (out.get("forbidden") or out.get("error")):
            forbidden = bool(out.get("forbidden"))
            reason = str(out.get("error") or "无法确定数据作用域")[:200]
            if forbidden:
                # 作用域解不出来不是"没认识你的说法": 写成权限拒, 否则用户与归因
                # 看到的都是一句误导的"语义层没这个指标"。
                answer = (
                    "这次取数被服务端拦下了: 无法确认可见数据范围。\n原因: " + reason
                    + "\n\n如果你是在本地/测试环境里看到这句, 请确认工号存在于员工主数据"
                    " 且已回填部门归属(而不是让模型换一个口径重算)。"
                )
                codes = ["E_NO_PERMISSION"]
                verdict = "denied"
            else:
                # 工具本身报错(不是拒): 不能冒充权限拒, 也不能当成"没这个口径"。
                answer = (
                    "问数服务当前不可用(语义层链接环节报错), 没能给出数字。\n报错: " + reason
                    + "\n\n这不是「你的问题没法回答」: 服务恢复后再问一次即可。"
                )
                codes = ["E_SERVICE"]
                verdict = "fail"
            return {
                "verdict": verdict, "error_codes": codes, "candidates": [], "answer": answer,
                "notes": list(state.get("notes") or []) + [reason],
            }
        result: dict[str, Any] = {
            "candidates": out.get("candidates") or [],
            "enum_filters": out.get("enum_filters") or {},
            "catalog_origin": out.get("catalog_origin") or "unknown",
            "vector_channel": out.get("vector_channel") or "unknown",
            "semver": out.get("semver") or state.get("semver") or "",
            "notes": list(state.get("notes") or []),
        }
        if out.get("error"):
            # 部分回 degrade(如枚举反查失败但候选仍在): 记进 notes 供归因, 不中断本轮。
            result["notes"].append(f"语义链接降级: {str(out['error'])[:160]}")
        if not result["candidates"]:
            # 短语一个都没命中时再拿原话试一次(中文连续串里维度说法常被拆开)。
            retry = await self._call("link_terms", text=state.get("question", ""),
                                    phrase_conf_json=_slot_confidence(slots))
            result["candidates"] = retry.get("candidates") or []
            result["semver"] = retry.get("semver") or result["semver"]
        else:
            # 更进一步: **有候选但最高分不够**也要拿原话重试一次。
            # 本轮实跑抖出的真问题: 模型会把"一般纳税人供应商数"截成"供应商数"并把限定词
            # 丢进 filter_terms, 拼出来的短语里没有连续口径名 -> 精确路必然落空,
            # 向量路只给 0.50 分 -> 本该一句就能答的问题变成反问。原话里它们是连续的。
            top = max([float(item.get("score") or 0)
                       for item in result["candidates"] if item.get("kind") == "metric"] or [0.0])
            if top < float(self._settings.askdata_clarify_confidence_threshold):
                question = state.get("question", "") or ""
                if _norm_text(question) != _norm_text(phrases):  # 既在 1244 行定义(归一+折叠空白)
                    retry = await self._call("link_terms", text=question,
                                            phrase_conf_json=_slot_confidence(slots))
                    merged = _merge_candidates(result["candidates"],
                                              retry.get("candidates") or [])
                    if merged != result["candidates"]:
                        result["candidates"] = merged
                        result["semver"] = retry.get("semver") or result["semver"]
                        result["notes"] = result["notes"] + [
                            "短语未能定住口径, 本轮改用原话重链接了一次(按最高分合并两轮候选)"]
        logger.debug("[askdata] link 完成 %dms, 候选 %d",
                     time_ms(started), len(result["candidates"]))
        return result

    async def clarify_gate(self, state: AskDataState) -> dict[str, Any]:
        """②-决定: 置信不足或口径冲突就发起**选项式**澄清; 否则直通。

        判定用的是"分数分布"而不是"最高分够不够": 前两名分差过小意味着系统自己
        也没把握, 这时硬选一个就是猜。禁止开放式反问 —— 选项必须来自候选集。
        """
        settings = self._settings
        slots = state.get("slots") or {}
        candidates = state.get("candidates") or []
        metrics = [c for c in candidates if c.get("kind") == "metric"]
        if state.get("cached_dsl"):
            # 意图是从已标注问法复用来的: 再反问一次"你要哪个口径"就没意义了
            # (那条问法本身已经隐含了完整选择), 但校验门仍旧照常把关。
            return {}
        threshold = float(settings.askdata_clarify_confidence_threshold)
        # 前两名分差阈值也配置化(方案要 0.10): 它直接决定澄清触发率, 应该是一个可调的口径
        # 而不是一个埋在代码里的数(调它不需要改代码, 但需要跑一轮评测看代价)。
        gap = float(settings.askdata_clarify_gap or 0.10)
        reasons: list[str] = []
        if not metrics:
            reasons.append("没能把你的说法对应到任何已登记的指标")
        else:
            best = float(metrics[0].get("score") or 0)
            # 只拿"在争同一个说法"的候选比分差: 否则一个指标因别名得 1.00、另一个因
            # 命中了句子里另一段得 0.95, 会被当成"口径分不清"而喷一句无意义反问。
            rival = _closest_rival(metrics)
            if best < threshold:
                reasons.append(f"指标匹配置信 {best:.2f} 低于阈值 {threshold:.2f}")
            if rival is not None and best - rival < gap:
                reasons.append(f"前两个口径太接近({best:.2f} vs {rival:.2f}), 需要你确认要哪一个")
        if not _has_time(slots) and _time_required(metrics):
            reasons.append("没有给出统计区间")

        if not reasons:
            return {}
        if not settings.askdata_clarify_enabled:
            # 开关关掉 = 允许"带着假设回答", 但必须把假设写进口径, 而不是让用户自己猜。
            # 这里**返回** notes 而不是只改 state: LangGraph 只合并返回字典里的键,
            # 原地改入参不会进通道, 那条假设说明就会在下一节点静默消失。
            note = "澄清已关闭: " + "; ".join(reasons) + "(按最接近口径作答)"
            return {"notes": list(state.get("notes") or []) + [note]}
        payload = _clarify_payload(state, reasons, metrics)
        return {"payload": payload, "verdict": "clarify", "answer": _clarify_text(payload)}

    async def plan(self, state: AskDataState) -> dict[str, Any]:
        """③ DSL 生成: 只产出受约束 JSON, 本地先过语法级校验(L1)。"""
        started = time.monotonic()
        cached = state.get("cached_dsl") or {}
        if cached:
            # 语义缓存命中: 不再花一次 LLM 重抽同一个意图。但 topN 的 N 仍以**本轮原话**为准
            # (复用的是口径形状, 用户今年说 3 个部门就不能拿昔年的 5 个答复他)。
            filled = _apply_topn(cached, state.get("prior_slots") or {}, state.get("question", ""))
            filled.pop("_topn_fixed", None)
            filled, time_note = _reconcile_time(filled, state.get("prior_slots") or {},
                                                 state.get("question", ""))
            filled, dropped_filters = _drop_invented_filters(
                filled, state.get("question", ""), state.get("candidates") or [],
                state.get("enum_filters"),
            )
            reused_notes = [n for n in (time_note,)
                             if n] + (["复用的意图里带了用户没说的过滤条件, 本轮已丢弃"]
                                       if dropped_filters else [])
            _track(state, f"DSL 复用缓存({time_ms(started)}ms)")
            return {"dsl": filled, "error_hint": "",
                    "notes": list(state.get("notes") or []) + reused_notes}
        breach = _budget_breach(state)
        if breach:
            return {"budget_reason": breach, "verdict": "", "error_hint": ""}
        slots = state.get("slots") or {}
        candidates = state.get("candidates") or []
        examples = await self._few_shot(state)
        prompt = prompts.dsl_prompt(
            question=state.get("question", ""),
            slots=json.dumps(slots, ensure_ascii=False),
            candidates=json.dumps(candidates[:10], ensure_ascii=False),
            schema_hint=render_schema_hint(),
            enum_filters=json.dumps(state.get("enum_filters") or {}, ensure_ascii=False),
            error_hint=state.get("error_hint", ""),
            examples=json.dumps(examples, ensure_ascii=False) if examples else "(无)",
            prior_dsl=json.dumps(_compact_dsl(state.get("prior_dsl") or {}), ensure_ascii=False)
            if state.get("prior_dsl") else "(无)",
        )
        tokens = 0
        try:
            resp = await self._json_llm.ainvoke(prompt)
            tokens = _usage_tokens(resp)
            raw = extract_json(str(resp.content))
            if raw is None:
                raise DslError("模型没有产出合法 JSON 对象")
            spec = parse_dsl(raw)
        except DslError as exc:
            return {"error_hint": str(exc), "verdict": "",
                    "token_cost": int(state.get("token_cost") or 0) + tokens,
                    "llm_calls": int(state.get("llm_calls") or 0) + 1,
                    "notes": list(state.get("notes") or [])}
        _track(state, f"DSL 生成完成({time_ms(started)}ms)")
        question = state.get("question", "")
        filled = _apply_topn(spec.to_payload(), slots, question)
        filled.pop("_topn_fixed", None)  # 内部标记不能混进 DSL(校验器只认登记过的字段)
        filled, time_note = _reconcile_time(filled, slots, question)
        filled, dropped_filters = _drop_invented_filters(
            filled, question, candidates, state.get("enum_filters"),
        )
        extra_notes = ([time_note] if time_note else []) + (
            ["本轮丢弃了用户没说的过滤条件: " + ", ".join(dropped_filters)
             + "(只按你说了的范围算)"] if dropped_filters else []
        ) + (
            ["topN 的 N 以用户原话为准(本轮已按原话对齐 order_by 与 limit)"]
            if filled != spec.to_payload() else []
        )
        return {
            "dsl": filled, "error_hint": "", "examples": examples,
            "token_cost": int(state.get("token_cost") or 0) + tokens,
            "llm_calls": int(state.get("llm_calls") or 0) + 1,
            "notes": list(state.get("notes") or []) + extra_notes,
        }

    async def _few_shot(self, state: AskDataState) -> list[dict[str, Any]]:
        """从 golden 集拿几条相似问法(dynamic few-shot, 方案 §4①)。

        拿不到(golden 空/工具不存在)就返回空列表: 没有例子只是回到静态 schema hint,
        不能把已经能回答的问题卡在这一步。
        """
        limit = int(self._settings.askdata_few_shot_cases or 0)
        if limit <= 0:
            return []
        out = await self._call("similar_cases", question=state.get("question", ""), limit=limit)
        examples = out.get("examples") or []
        return [e for e in examples if isinstance(e, dict)][:limit]

    async def validate(self, state: AskDataState) -> dict[str, Any]:
        """③-校验器(独立模块, 不信模型自检): 语义级 + 权限级。"""
        dsl = state.get("dsl") or {}
        if not dsl:
            return {"validate_result": {"ok": False, "errors": [
                {"code": "E_SCHEMA", "message": state.get("error_hint") or "DSL 未生成", "field": ""}
            ], "notices": []}}
        # 时间对账放在这里而不是只放在 plan 里: 本节点是**三条路径的汇合点**
        # (DSL 生成 / 语义缓存复用 / 回炉重写都得经过它), 只在 plan 里补一次的话,
        # 重写环节重新交出的 DSL 会绕过对账(本轮实测: 修完仍拿到"本月=9月整月")。
        # 对账本身幂等: 已经只剩区间词时进去就是空操作。
        reconciled, time_note = _reconcile_time(dsl, state.get("slots") or {},
                                               state.get("question", ""))
        notes = list(state.get("notes") or [])
        if time_note:
            notes = notes + [time_note]
        out = await self._call(
            "validate_dsl", dsl_json=reconciled, trace_id=state.get("trace_id", ""),
        )
        notices = out.get("notices") or []
        for notice in notices:
            notes.append(f"[{notice.get('code')}] {notice.get('message')}")
        return {"validate_result": out, "notes": notes, "dsl": reconciled,
                "semver": out.get("semver") or state.get("semver") or ""}

    async def route(self, state: AskDataState) -> dict[str, Any]:
        """④ 执行路由 + 取数: 指标 API → DSL 编译器 → 沙箱 SQL(逐级降级, 记录每级失败原因)。

        同环比/占比这类算术都在引擎侧(编译器出的 SQL)算, 模型不参与算术 —— 这是
        问数准确率里最容易被低估的一块: 让模型做两位小数的除法就是错答的来源。
        """
        settings = self._settings
        breach = _budget_breach(state)
        if breach:
            return {"budget_reason": breach, "verdict": "fail",
                    "error_codes": list(state.get("error_codes") or []) + ["E_BUDGET"],
                    "exec_result": {}}
        dsl = state.get("dsl") or {}
        order = [p.strip() for p in (settings.askdata_route_order or "").split(",") if p.strip()]
        attempts: list[dict[str, Any]] = []
        for level in order:
            if level == "sandbox" and not settings.askdata_sandbox_enabled:
                continue
            if level == "metric_api":
                out = await self._call("query_metric_api", dsl_json=dsl,
                                       nl_question=state.get("question", ""),
                                       trace_id=state.get("trace_id", ""))
            elif level == "compiler":
                out = await self._call("compile_dsl", dsl_json=dsl,
                                       nl_question=state.get("question", ""),
                                       trace_id=state.get("trace_id", ""))
            elif level == "sandbox":
                out = await self._sandbox(state)
            else:
                continue
            attempts.append({"level": level, "ok": not out.get("error"),
                             "error": str(out.get("error") or "")[:200]})
            if not out.get("error"):
                out.setdefault("route", level)
                return {"exec_result": out, "route": level, "attempts": attempts,
                        "semver": out.get("semver") or state.get("semver") or "",
                        "cache_hit": bool(out.get("from_cache"))}
        return {"exec_result": {}, "attempts": attempts,
                "verdict": "fail",
                "error_codes": _codes_of(attempts),
                "answer": _route_failed_text(attempts, state)}

    async def _sandbox(self, state: AskDataState) -> dict[str, Any]:
        """最后一级: 模型直出 SQL, 只在只读沙箱里跑(治理视图 + 行数 + 扫描量 + JOIN/笛卡尔护栏 + RLS)。

        发给模型的是**视图清单而不是基表清单**: 可查面与提示词必须是同一件事。
        给基表清单会让模型按基表列名写 SQL, 然后在 AST 表白名单那一轮被拒 —— 那种
        "提示词说可以, 防线说不可以"的矛盾会表现成这一级永远答不出来。
        """
        from app.db.schema_docs import ANALYTICS_SANDBOX_VIEW_DDL

        question = state.get("question", "")
        dsl = json.dumps(state.get("dsl") or {}, ensure_ascii=False)
        prompt = (
            "只写一条 PostgreSQL 只读 SELECT(或 WITH...SELECT), 只输出 SQL 本身, 不要解释。\n"
            f"要回答的问题: {question}\n已确定的查询意图: {dsl}\n"
            f"可查视图与字段:\n{ANALYTICS_SANDBOX_VIEW_DDL}\n"
            "规则: 只查上面列出的视图; 不要自己写 tenant_id/dept_id 条件(能读到哪些行由"
            "服务端行级安全决定); 时间用 created_at 左闭右开; 需要部门就 JOIN v_analytics_employees "
            "并带上 ON 关联条件; 不要注释、不要分号、不要写第二条语句。"
        )
        try:
            resp = await self._llm.ainvoke(prompt)
            sql = str(resp.content).strip()
        except Exception as exc:  # noqa: BLE001 - 沙箱生成失败就当作这一级不可用
            return {"error": f"沙箱 SQL 生成失败: {exc}"}
        sql = _JSON_FENCE_RE.sub("", sql).strip().strip(";").strip()
        if not sql.upper().startswith(("SELECT", "WITH")):
            return {"error": "模型没有产出 SELECT, 沙箱这一级放弃"}
        return await self._call("exec_sql_sandbox", sql=sql,
                                nl_question=question, trace_id=state.get("trace_id", ""))

    async def verify(self, state: AskDataState) -> dict[str, Any]:
        """结果校验(确定性规则, 独立于模型): 列缺失/主指标全 NULL/量级异常/行数越界。

        两类判定必须分开, 否则会把好答案拦下来:
        - **空结果集不是失败**: "这个范围内就是没数据"是合法答案(当成异常拉起自修复只会
          烧两轮 token 并给出一个看起来更漂亮的错答案);
        - **同比列全 NULL 也不是失败**: 对比期没数时 `NULLIF` 就是会给 NULL, 那是
          "没得比"而不是"算错了"。所以只校用户在 DSL 里点名要的**主指标列**。
        """
        result = state.get("exec_result") or {}
        columns = list(result.get("columns") or [])
        rows = list(result.get("rows") or [])
        dsl = state.get("dsl") or {}
        primary = {str(item.get("id") or "") for item in (dsl.get("metrics") or []) if item.get("id")}
        problems: list[str] = []
        notes: list[str] = []
        if not rows:
            notes.append("结果为空集(该范围内没数据)")
        metric_cols = [c for c in columns if str(c).startswith("m_")]
        checked = [c for c in metric_cols if c in primary] or metric_cols
        for column in checked:
            values = [row.get(column) for row in rows]
            if values and all(v is None for v in values):
                problems.append(f"主指标列 {column} 全是 NULL(口径可能落空)")
                continue
            numeric = [abs(float(v)) for v in values if isinstance(v, (int, float))]
            if any(v >= 1e12 for v in numeric):
                problems.append(f"指标列 {column} 出现 1e12 量级值, 疑似单位或口径错")
        for column in [c for c in metric_cols if c not in checked]:
            values = [row.get(column) for row in rows]
            if values and all(v is None for v in values):
                notes.append(f"对比列 {column} 全为空(对比期没数据, 不是算错)")
        if primary - set(columns):
            problems.append(f"缺少主指标列: {sorted(primary - set(columns))}")
        cap = int(self._settings.askdata_max_rows or 100)
        if len(rows) > cap:
            problems.append(f"返回 {len(rows)} 行超过单次上限 {cap}")
        # G6: 量级基线(z-score) + 单位一致性。这两个只写进 notes 并进 payload.suspect,
        # **不改 problems**: 基线来自问数自己的留痕(自参照), 拿它拉起自修复只会重算
        # 同一个数并众烧两轮 token; 而"数据存疑"这句话本来就应该让用户看到而不是替用户决定。
        suspect = await self._sanity_checks(state, columns, rows, checked)
        notes.extend(suspect.get("notes") or [])
        out: dict[str, Any] = {"verify": {"ok": not problems, "problems": problems, "notes": notes,
                                         "empty": not rows,
                                         "suspect": bool(suspect.get("suspect")),
                                         "baseline": suspect.get("baseline") or {}}}
        if problems and not self._settings.askdata_repair_enabled:
            # 自修复关掉时必须把 verdict 写成 fail: 否则 finalize 会拿"有结果"当 ok,
            # 一个没被任何人复核过的异常结果就以"成功"落留痕并进看板分子。
            out["verdict"] = "fail"
            out["error_codes"] = list(state.get("error_codes") or []) + ["E_VERIFY"]
            out["notes"] = list(state.get("notes") or []) + problems
        return out

    async def _sanity_checks(
        self, state: AskDataState, columns: list[str], rows: list[dict[str, Any]],
        checked: list[str],
    ) -> dict[str, Any]:
        """单位一致 + 历史基线偏离(z-score), 产出给人看的注记与 suspect 标。

        z-score 用的是稳健尺度(MAD*1.4826, 由 MCP 的 metric_baseline 算): 同一个口径
        反复被问同一个数时 MAD=0, 拿普通 std 会把任何一点差异都判成无限大异常。
        """
        settings = self._settings
        notes: list[str] = []
        baseline_info: dict[str, Any] = {}
        suspect = False
        if not rows or not checked:
            return {"notes": notes, "suspect": False, "baseline": {}}
        column = checked[0]
        values = [row.get(column) for row in rows if isinstance(row.get(column), (int, float))]
        if not values:
            return {"notes": notes, "suspect": False, "baseline": {}}
        value = float(values[0])
        metric_id = str(column[2:] if column.startswith("m_") else column)
        unit = _unit_of(state, metric_id)
        # 单位一致性: 百分比超出 [0, 100] 与"天/单/人"为负都是口径错位的形状错。
        if unit == "%" and abs(value) > 100:
            notes.append(f"百分比指标得到 {value}, 超出 0~100: 单位或分母可能不对")
            suspect = True
        elif unit in ("天", "单", "人", "人次") and value < 0:
            notes.append(f"{unit}单位的指标得到负数 {value}: 口径或软删排除可能有问题")
            suspect = True
        threshold = float(settings.askdata_baseline_zscore or 0)
        if threshold <= 0:
            return {"notes": notes, "suspect": suspect, "baseline": {}}
        shape = ",".join(sorted({str(d) for d in (state.get("dsl") or {}).get("dims") or []}))
        out = await self._call("metric_baseline", metric_id=metric_id, shape=shape)
        samples = int(out.get("samples") or 0)
        if samples < 5:
            return {"notes": notes, "suspect": suspect, "baseline": {"samples": samples}}
        median = float(out.get("median") or 0)
        scale = float(out.get("scale") or 0)
        baseline_info = {"samples": samples, "median": median, "scale": scale,
                         "scoped": out.get("scoped", "")}
        if scale <= 0:
            return {"notes": notes, "suspect": suspect, "baseline": baseline_info}
        z = abs(abs(value) - median) / scale
        if z >= threshold:
            notes.append(
                f"本次 {metric_id}={value:g} 与最近 {samples} 次的中位数 {median:g} 偏离 "
                f"{z:.1f} 个尺度(阈值 {threshold:g}): 可能是口径变了、也可能是业务真的跳了"
            )
            suspect = True
        return {"notes": notes, "suspect": suspect, "baseline": baseline_info}

    async def repair(self, state: AskDataState) -> dict[str, Any]:
        """受限自修复: 只读工具上的有界 ReAct(默认 ≤2 轮 + 墙钟上限), 失败就如实降级。"""
        settings = self._settings
        role = state.get("role", "")
        tools = await self._ensure_tools()
        subset = [tools[name] for name in _REPAIR_TOOLS if name in tools]
        if not subset:
            return {"verdict": "degraded", "repair_text": ""}
        agent = self._repair_agents.get(role)
        if agent is None:
            from app.security.auth import filter_tools_for_role
            from app.schemas import Role
            from app.security.caller import bind_caller_tools

            try:
                parsed = Role(role)
            except ValueError:
                parsed = Role.EMPLOYEE
            agent = create_agent(
                self._llm,
                bind_caller_tools(filter_tools_for_role(parsed, "askdata", subset)),
                system_prompt=prompts.REPAIR_PROMPT.format(
                    question=state.get("question", ""),
                    dsl=json.dumps(state.get("dsl") or {}, ensure_ascii=False),
                    verdict=json.dumps((state.get("verify") or {}), ensure_ascii=False),
                    candidates=json.dumps((state.get("candidates") or [])[:6], ensure_ascii=False),
                ),
            )
            self._repair_agents[role] = agent
        try:
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [("user", state.get("question", ""))]}),
                timeout=float(settings.askdata_repair_timeout),
            )
        except (TimeoutError, Exception) as exc:  # noqa: BLE001 - 自修复失败只降级, 不炸整轮
            logger.warning("[askdata] 自修复失败(降级为如实说明): %s", exc)
            return {"verdict": "degraded", "repair_text": ""}
        text = ""
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                text = str(msg.content)
                break
        return {"repair_text": text, "verdict": "degraded" if text else "fail"}

    async def render(self, state: AskDataState) -> dict[str, Any]:
        """⑤ 可视化与叙述: 图型由结果 shape 规则决定, LLM 只补一段结论。"""
        result = state.get("exec_result") or {}
        columns = list(result.get("columns") or [])
        rows = list(result.get("rows") or [])
        labels = result.get("column_labels") or {}
        chart: dict[str, Any] = {}
        shape = _chart_shape(state, columns, rows, labels)
        if shape["kind"] != "none" and rows:
            out = await self._call(
                "render_chart", title=f"{state.get('question', '')[:28]}", chart_type=shape["kind"],
                categories=shape["cats"], series=shape["series"], values=shape["values"],
            )
            if out.get("url"):
                chart = {"url": out["url"], "png_url": out.get("png_url", ""),
                         "chart_type": out.get("chart_type", shape["kind"]),
                         "name": out.get("name", ""),
                         "series_names": [s.get("name") for s in (shape["series"] or []) if s]}
        narrative, tokens = await self._narrate(state, columns, labels, rows)
        return {"chart": chart, "narrative": narrative,
                "token_cost": int(state.get("token_cost") or 0) + tokens,
                "llm_calls": int(state.get("llm_calls") or 0) + 1}

    async def _narrate(self, state: AskDataState, columns: list[str],
                       labels: dict[str, str], rows: list[dict[str, Any]]) -> tuple[str, int]:
        result = state.get("exec_result") or {}
        table = _md_table(columns, labels, rows)
        caliber = " ; ".join(result.get("caliber_notes") or []) or "(见下方口径列)"
        verify = state.get("verify") or {}
        suspect_note = ""
        if verify.get("suspect"):
            suspect_note = "\n开头加一句“数据存疑”提醒(理由: " + "; ".join(
                str(n) for n in (verify.get("notes") or [])[-2:]) + "), 不要把存疑的数当定论报。"
        fallback = (
            f"统计区间: {(result.get('columns') and state.get('dsl', {}).get('time') or {}).get('range', '-')}; "
            f"可见范围: {result.get('scope_note', '-')}"
        )
        try:
            resp = await self._llm.ainvoke(prompts.NARRATOR_PROMPT.format(
                question=state.get("question", ""),
                range_label=json.dumps((state.get("validate_result") or {}).get("resolved_range") or {},
                                       ensure_ascii=False),
                scope_note=result.get("scope_note", "-"),
                caliber=caliber,
                columns=", ".join(labels.get(c, c) for c in columns) or "-",
                rowcount=len(rows),
                truncated="(仅展示前 20 行)" if len(rows) > 20 else "",
                table=table or "(无数据行)",
                suspect=suspect_note,
            ))
            return (str(resp.content).strip() or fallback), _usage_tokens(resp)
        except Exception as exc:  # noqa: BLE001 - 叙述只是加分项, 表已经在答案里
            logger.warning("[askdata] 叙述生成失败(只出表格): %s", exc)
            return fallback, 0

    async def finalize(self, state: AskDataState) -> dict[str, Any]:
        """⑥ 组装答案与结构化载荷, 并把这一轮全链路写进留痕(看板与归因的原料)。"""
        payload = state.get("payload")
        if payload and payload.get("kind") == "clarify":
            query_id = await self._log(state, payload=None)
            payload["query_id"] = query_id
            payload["semver"] = state.get("semver", "")
            return {"payload": payload, "answer": _clarify_text(payload),
                    "verdict": "clarify"}
        result = state.get("exec_result") or {}
        verdict = state.get("verdict") or ("ok" if result else "fail")
        answer = state.get("answer") or ""
        if not answer and not result and verdict != "ok":
            # 只在"一个数都没算出来"时出拒答模板; 有结果但校验存疑时仍然把数交出去
            # (带警告), 因为"拦下好答案"比"给出带警告的答案"对用户更差。
            answer = _refuse_text(state)
        if not answer:
            answer = await _compose_answer(self, state, result, verdict)
        body = {
            "kind": "result" if verdict == "ok" else ("degraded" if verdict == "degraded" else "refuse"),
            "question": state.get("question", ""),
            "route": state.get("route", ""),
            "slots": state.get("slots") or {},
            "dsl": state.get("dsl") or {},
            # 展示用的区间**由定稿 DSL 现算**, 不转抄 validate 的回执:
            # 本轮实测到一个难查的错: 留痕里的 DSL 时间是对的(this_month),
            # 而给用户看的 range 块却是 custom 2026-09-01~10-01 —— 同一个数与它自己的
            # 区间标签不一致。转抄中间层回执就会这样: 取数用的是一份, 展示写的是另一份。
            # 现在只从定稿 DSL 推, 两者不可能再分叉。
            "range": _display_range(state.get("dsl") or {},
                                    (state.get("validate_result") or {}).get("resolved_range") or {}),
            "columns": list(result.get("columns") or []),
            "column_labels": result.get("column_labels") or {},
            "rows": (result.get("rows") or [])[:50],
            "rowcount": int(result.get("rowcount") or 0),
            "sql": result.get("sql", ""),
            "caliber_notes": list(result.get("caliber_notes") or []) + list(state.get("notes") or []),
            "scope_note": result.get("scope_note", "") or state.get("validate_result", {}).get("scope_note", ""),
            "chart": state.get("chart") or {},
            "verdict": verdict,
            "error_codes": list(state.get("error_codes") or []),
            "attempts": state.get("attempts") or [],
            "catalog_origin": state.get("catalog_origin", ""),
            "vector_channel": state.get("vector_channel", ""),
            "semver": state.get("semver", ""),
            "cache_hit": bool(state.get("cache_hit")),
            "policies": list(result.get("policies") or []),
            "verify": (state.get("verify") or {}).get("suspect", False),
            "token_cost": int(state.get("token_cost") or 0),
            "llm_calls": int(state.get("llm_calls") or 0),
            "latency_ms": _elapsed_ms(state),
            "trace_id": state.get("trace_id", ""),
            # 候选口径也随结果一同下发: 反馈面板里"我要改口径"要能列出可选口径,
            # 否则用户只能自由打字, 那条反馈就没法归到语义层对象上。
            "metrics": [
                {"id": c.get("id"), "label": c.get("label"), "caliber": c.get("caliber", ""),
                 "score": c.get("score"), "time_aware": c.get("time_aware"), "unit": c.get("unit", "")}
                for c in (state.get("candidates") or [])
                if c.get("kind") == "metric"
            ][:6],
        }
        # 预算熔断/引擎熔断了还是没出数: 开一张人工工单(方案 §4④ FALLBACK 的最后一级)。
        if not result and (state.get("budget_reason") or _needs_ticket(state)):
            body["ticket"] = await self._open_ticket(state, body)
        query_id = await self._log(state, payload=body)
        body["query_id"] = query_id
        return {"payload": body, "answer": answer, "verdict": verdict,
                "error_codes": body["error_codes"]}

    async def _open_ticket(self, state: AskDataState, body: dict[str, Any]) -> dict[str, Any]:
        """本轮答不出来时开单(失败不影响回答, 只是少了个后续可追的出口)。"""
        reason = "budget_exceeded" if state.get("budget_reason") else _ticket_reason(state)
        out = await self._call(
            "create_ticket",
            question=state.get("question", ""), reason=reason,
            trace_id=state.get("trace_id", ""), dsl_json=body.get("dsl") or {},
            semver=body.get("semver", ""),
        )
        if not out.get("ticket_id"):
            logger.warning("[askdata] 工单没开成(本轮仍按原样回答): %s", out.get("error") or out)
            return {"status": "degraded", "reason": reason}
        return {"status": "ok", "ticket_id": out["ticket_id"], "reason": reason}

    async def _log(self, state: AskDataState, *, payload: dict[str, Any] | None) -> str:
        """写留痕(含被澄清拦住的那一轮); 写不出来就返回空串并让载荷自己说明降级。"""
        result = state.get("exec_result") or {}
        route = state.get("route") or (
            "clarify" if state.get("verdict") == "clarify" else str((payload or {}).get("route") or "")
        )
        notes = list(state.get("notes") or [])
        if state.get("budget_reason"):
            notes.append("预算熔断: " + state["budget_reason"])
        if state.get("verdict") == "clarify":
            notes.append(str((payload or state.get("payload") or {}).get("reason") or ""))
        chosen, top = _clarify_choice(state)
        out = await self._call(
            "log_query",
            trace_id=state.get("trace_id", ""),
            question=state.get("question", ""),
            slots_json=state.get("slots") or {},
            candidates_json=(state.get("candidates") or [])[:10],
            dsl_json=state.get("dsl") or {},
            route=route,
            final_sql=result.get("sql", ""),
            rowcount=int(result.get("rowcount") or 0),
            latency_ms=_elapsed_ms(state),
            verdict=state.get("verdict") or "ok",
            error_codes=list(state.get("error_codes") or []),
            notes=" | ".join(n for n in notes if n)[:2000],
            session_id=state.get("session_id", ""),
            semver=state.get("semver", ""),
            catalog_origin=state.get("catalog_origin", ""),
            token_cost=int(state.get("token_cost") or 0),
            llm_calls=int(state.get("llm_calls") or 0),
            retry_count=int(state.get("rewrite_count") or 0),
            metric_summary=_metric_summary(state, result),
            clarify_pick=chosen,
            clarify_top=top,
        )
        return str(out.get("query_id") or "")

    # ---------------- 图 ----------------
    def build(self):
        g = StateGraph(AskDataState)
        g.add_node("recall", self.recall)
        g.add_node("parse", self.parse)
        g.add_node("link", self.link)
        g.add_node("clarify_gate", self.clarify_gate)
        g.add_node("plan", self.plan)
        g.add_node("validate", self.validate)
        g.add_node("route", self.route)
        g.add_node("verify", self.verify)
        g.add_node("repair", self.repair)
        g.add_node("render", self.render)
        g.add_node("finalize", self.finalize)
        g.add_node("rewrite", _rewrite_node)

        g.add_edge(START, "recall")
        # 语义缓存命中时跳过槽位抽取(不必再让模型听一遍同一句话), 但 link/validate 不跳。
        g.add_conditional_edges("recall", _after_recall,
                                {"link": "link", "parse": "parse"})
        g.add_edge("parse", "link")
        g.add_conditional_edges("link", _after_link,
                                {"denied": "finalize", "clarify_gate": "clarify_gate"})
        g.add_conditional_edges("clarify_gate", _after_clarify,
                                {"clarify": "finalize", "proceed": "plan"})
        g.add_conditional_edges("plan", _after_plan,
                                {"validate": "validate", "rewrite": "rewrite", "finalize": "finalize"})
        g.add_conditional_edges("validate", _after_validate,
                                {"route": "route", "rewrite": "rewrite", "finalize": "finalize"})
        g.add_edge("rewrite", "plan")
        g.add_conditional_edges("route", _after_route,
                                {"verify": "verify", "finalize": "finalize"})
        g.add_conditional_edges("verify", _after_verify,
                                {"render": "render", "repair": "repair", "finalize": "finalize"})
        g.add_edge("repair", "finalize")
        g.add_edge("render", "finalize")
        g.add_edge("finalize", END)
        return g.compile()

    async def run(self, state: AskDataState) -> AskDataState:
        """跑一轮状态机, 并把整轮的归因要用的属性写进一条 OTel span。"""
        graph = self.build()
        with otel.span("turn", {
            "session_id": state.get("session_id", ""),
            "trace_id": state.get("trace_id", ""),
            "role": state.get("role", ""),
        }) as extra:
            result = await graph.ainvoke(state)
            slots = result.get("slots") or {}
            conf = slots.get("confidence") or {}
            candidates = result.get("candidates") or []
            extra["conf"] = {k: conf[k] for k in list(conf)[:4] if isinstance(conf[k], (int, float))}
            extra["recall"] = [round(float(c.get("score") or 0), 3) for c in candidates[:5]]
            extra["route"] = result.get("route", "")
            extra["verdict"] = result.get("verdict", "")
            extra["retries"] = int(result.get("rewrite_count") or 0)
            extra["tokens"] = int(result.get("token_cost") or 0)
            extra["llm_calls"] = int(result.get("llm_calls") or 0)
            extra["semver"] = result.get("semver", "")
            extra["catalog_origin"] = result.get("catalog_origin", "")
            extra["error_codes"] = list(result.get("error_codes") or [])[:8]
            extra["cache_hit"] = bool(result.get("cache_hit"))
            return result


# ---------------------------------------------------------------------------
# 条件路由(每个判据都是结构事实, 不看答案文本猜)
# ---------------------------------------------------------------------------
def _merge_candidates(primary: list[dict[str, Any]],
                      extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """两轮链接的候选合并: 同一目标只留高分的那个, 按分数降序。

    拿"替换"而不是合并会把第一轮已经抽好的维度/枚举候选丢掉(原话里未必连着出现维度说法)。
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for item in list(primary) + list(extra):
        key = (str(item.get("kind") or ""), str(item.get("id") or item.get("target_id") or ""))
        current = best.get(key)
        if current is None or float(item.get("score") or 0) > float(current.get("score") or 0):
            best[key] = item
    return sorted(best.values(), key=lambda item: -float(item.get("score") or 0))


def _after_recall(state: AskDataState) -> str:
    """语义缓存命中就直连 link(已经拿到 DSL 了, 再抽一次槽位是白烧 token)。"""
    return "link" if state.get("cached_dsl") else "parse"


def _after_link(state: AskDataState) -> str:
    """链接阶段就被服务端拒(作用域)或服务本身报错时直接出结论, 不再往下猜。"""
    return "clarify_gate" if not state.get("verdict") else "denied"


def _after_clarify(state: AskDataState) -> str:
    if state.get("verdict") == "clarify":
        return "clarify"
    return "proceed"


def _rewrite_node(state: AskDataState) -> dict[str, Any]:
    """回炉计数节点: 把"重写了几个"这件事单独管, 而不是让条件边自己递增。

    这件事看起来小, 但它是终止保证的唯一所在: 条件边直接绕回 plan 而没人递增计数,
    一个永远修不掉的 DSL 就会在 plan/validate 之间无限振荡直到堆死整个容器。
    """
    count = int(state.get("rewrite_count") or 0) + 1
    errors = (state.get("validate_result") or {}).get("errors") or []
    hint = state.get("error_hint") or "; ".join(
        f"[{e.get('code')}] {e.get('message')}" for e in errors[:3]
    )
    return {"rewrite_count": count, "error_hint": hint}


def _after_plan(state: AskDataState) -> str:
    if state.get("budget_reason"):
        return "finalize"  # 超预算不再往下烧: 直接把已经拿到的东西交出去
    if state.get("dsl"):
        return "validate"
    # 与 _after_validate 同一个判据(严格小于): 否则"语法失败也能回炉一次"会变成两次重写。
    if int(state.get("rewrite_count") or 0) < _max_rewrites() and state.get("error_hint"):
        return "rewrite"
    return "finalize"


def _after_validate(state: AskDataState) -> str:
    check = state.get("validate_result") or {}
    if check.get("ok"):
        return "route"
    if int(state.get("rewrite_count") or 0) < _max_rewrites():
        return "rewrite"
    return "finalize"


def _max_rewrites() -> int:
    """校验失败后的回炉上限(配置驱动): 每个错误码都带可执行拒因, 两次还不行就是真修不了。"""
    return max(1, int(get_settings().askdata_validate_max_rewrites or 1))


def _after_route(state: AskDataState) -> str:
    return "verify" if state.get("exec_result") else "finalize"


def _after_verify(state: AskDataState) -> str:
    check = state.get("verify") or {}
    if check.get("ok"):
        return "render"
    settings = get_settings()
    if settings.askdata_repair_enabled and not state.get("repair_text"):
        return "repair"
    return "finalize"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _inherit(slots: dict[str, Any], prior: dict[str, Any]) -> dict[str, Any]:
    """多轮省略/指代的确定性继承: 只继承"本轮完全没说"的时间与指标说法。

    只继承这两样是因为它们的省略最常见且后果可控; 维度/过滤条件不继承 —— 那会把
    上一轮的筛选悄悄带进这一次的数, 是最难被发现的一类错。
    """
    merged = dict(slots or {})
    if not (merged.get("time") or {}).get("phrase") and (prior.get("time") or {}).get("phrase"):
        merged["time"] = {**(merged.get("time") or {}),
                          "phrase": prior["time"]["phrase"], "inherited": True}
    if not merged.get("metric_terms") and prior.get("metric_terms"):
        merged["metric_terms"] = list(prior["metric_terms"])
        merged.setdefault("confidence", {})["metric"] = 0.6
    merged["prior_inherited"] = bool(
        (merged.get("time") or {}).get("inherited") or merged.get("metric_terms")
        and not (slots or {}).get("metric_terms")
    )
    return merged


def _has_time(slots: dict[str, Any]) -> bool:
    time_slot = slots.get("time") or {}
    return bool(time_slot.get("phrase") or time_slot.get("start") or time_slot.get("end"))


def _recover_time_phrase(slots: dict[str, Any], question: str) -> dict[str, Any]:
    """模型漏抽时间但原话里明写着区间词时, 确定性把它补回来。

    为什么不算"猜时间": 只允许填入原话里存在的词(词表来自 timesem 的同义表),
    不存在的仍然为空并交给澄清门。不补的代价很实在: "各部门今年差旅费金额" 会被反问
    一句"请确认统计区间", 而这轮本来根本不需要问。
    """
    from app.semantic import timesem

    if _has_time(slots):
        return slots
    phrase = timesem.detect_phrase(question or "")
    if not phrase:
        return slots
    out = dict(slots)
    out["time"] = {**(out.get("time") or {}), "phrase": phrase,
                   "granularity": (out.get("time") or {}).get("granularity") or "none"}
    out["time_recovered"] = phrase
    return out


def _phrase_overlap(left: str, right: str) -> bool:
    """两个候选命中的说法是否"同一段话的两种解释"(互相包含就算)。"""
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if not a or not b:
        # 拿不到 matched(向量通道没这个字段)时保守当成在争同一个说法: 宁可多问。
        return True
    return a in b or b in a


def _closest_rival(metrics: list[dict[str, Any]]) -> float | None:
    """与首选口径"在争同一个说法"的候选里的最高分(没这样的对手返 None)。"""
    if len(metrics) < 2:
        return None
    top = metrics[0]
    best: float | None = None
    for item in metrics[1:]:
        if not _phrase_overlap(str(top.get("matched") or ""), str(item.get("matched") or "")):
            continue
        score = float(item.get("score") or 0)
        if best is None or score > best:
            best = score
    return best


def _candidates_are_snapshots(metrics: list[dict[str, Any]]) -> bool:
    """候选口径全是不看时间的快照型指标(年度预算消耗率、在职人数等)。

    对这类指标要时间区间没意义(库里根本没有时间列), 问了就是白问一轮。
    拿不到 ``time_aware`` 时按"需要时间"处理(保守: 宁可多问一句)。
    """
    if not metrics:
        return False
    return all(m.get("time_aware") is False for m in metrics)


def _time_required(metrics: list[dict[str, Any]]) -> bool:
    """本轮是否真的还欠一个统计区间 —— 按**会被选中的那个口径**判, 不按全部候选判。

    为什么不是"全部候选都是快照才免": 本轮实测 6 条错在这上面 ——
    问"年假余额合计是多少"时 top1 是年度快照指标(库里没有时间列), 但候选里还混着
    "年假天数"(按审批时间统计)。拿全体取"与"得出的结论是"缺统计区间", 而对用户来说
    这个追问根本无法回答(他选的口径就没有区间可言)。
    真选错了怎么办: 模型若最后挑了一个需要时间的口径, 校验器的 E_TIME_INVALID 会确定性
    拦下并回炉 —— 那条路比"对快照指标喷一句没意义的追问"便宜。
    """
    if not metrics:
        return True
    top = metrics[0]
    if top.get("time_aware") is False:
        return False
    return not _candidates_are_snapshots(metrics)


def _prune_invented_terms(slots: dict[str, Any], question: str) -> dict[str, Any]:
    """丢掉不是从本轮原话里截出来的说法(模型拿自己的词表替代了用户的话)。

    为什么必需: 槽位抽取一旦把"每一单外卖的金额中位数"这类它不会的词翻成
    "报销总额/年假余额合计", 下游链接就会拿到两个 1.00 的**精确命中**看似很有把握,
    实际是在问一个用户根本没说的口径。提示词里的"不要编造"拦不住这件事(实测会编),
    所以这里用确定性判据卡一道: 说法必须是归一化后原话的子串。

    继承来的说法不要求在本轮原话里(它就是从上一轮带来的); 完全被剔光时保留
    问题本身作为说法, 交给链接/澄清去判能不能对应上口径。
    """
    haystack = _norm_text(question)
    out = dict(slots or {})
    dropped: list[str] = []

    def _keep(items: Any, *, dict_key: str | None = None) -> list[Any]:
        kept: list[Any] = []
        for item in items or []:
            phrase = str(item.get(dict_key) if dict_key else item or "").strip()
            if not phrase:
                continue
            needle = _norm_text(phrase)
            if needle and needle in haystack:
                kept.append(item)
            else:
                dropped.append(phrase)
        return kept

    metric_terms = _keep(out.get("metric_terms"))
    dim_terms = _keep(out.get("dim_terms"))
    filter_terms = _keep(out.get("filter_terms"), dict_key="phrase")
    time_phrase = str(((out.get("time") or {}).get("phrase")) or "").strip()
    if time_phrase and _norm_text(time_phrase) not in haystack and not (out.get("time") or {}).get("inherited"):
        dropped.append(time_phrase)
        out["time"] = {k: v for k, v in (out.get("time") or {}).items() if k != "phrase"}
    out["metric_terms"] = metric_terms or ([question.strip()] if question else [])
    out["dim_terms"] = dim_terms
    out["filter_terms"] = filter_terms
    if dropped:
        out["invented_dropped"] = dropped[:8]
    return out


def _norm_text(value: str) -> str:
    from app.docs.normalize import normalize_text

    return re.sub(r"\s+", " ", normalize_text(value or "")).strip().lower()


def _phrase_of(slots: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.extend(str(p) for p in slots.get("metric_terms") or [])
    parts.extend(str(p) for p in slots.get("dim_terms") or [])
    for item in slots.get("filter_terms") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("phrase") or ""))
    time_slot = slots.get("time") or {}
    parts.append(str(time_slot.get("phrase") or ""))
    compare = slots.get("compare") or {}
    parts.append(str(compare.get("phrase") or ""))
    return " ".join(p for p in parts if p).strip()


def _time_phrase_to_token(phrase: str) -> str:
    try:
        from app.semantic.timesem import normalize_range

        return normalize_range(phrase)
    except Exception:  # noqa: BLE001 - 说法归不到区间词就走澄清
        return ""


def _clarify_payload(state: AskDataState, reasons: list[str], metrics: list[dict[str, Any]]) -> dict[str, Any]:
    settings = get_settings()
    limit = max(2, int(settings.askdata_clarify_max_options))
    slots = state.get("slots") or {}
    dims = [c for c in state.get("candidates") or [] if c.get("kind") == "dim"]
    return {
        "kind": "clarify",
        "question": state.get("question", ""),
        "reason": "; ".join(reasons),
        "lead": prompts.CLARIFY_LEAD,
        "metrics": [
            {"id": m.get("id"), "label": m.get("label"), "caliber": m.get("caliber"),
             "score": m.get("score")}
            for m in metrics[:limit]
        ],
        "times": [{"token": token, "label": label} for token, label in TIME_CHOICES],
        "has_time": _has_time(slots),
        "dims": [{"id": d.get("id"), "label": d.get("label")} for d in dims[:limit]],
        "allow_none": True,
        "slots": slots,
    }


def _clarify_text(payload: dict[str, Any]) -> str:
    """澄清正文: 与面板同源的一份**人读**文本(面板渲染不了时用户仍能看到选项)。"""
    lines = [payload.get("lead", prompts.CLARIFY_LEAD), f"原因: {payload.get('reason', '')}", ""]
    metrics = payload.get("metrics") or []
    if metrics:
        lines.append("请确认要看的指标口径:")
        for i, item in enumerate(metrics, 1):
            lines.append(f"{i}) {item.get('label')} —— {item.get('caliber') or '(口径见语义层)'}")
    if not payload.get("has_time"):
        lines.append("")
        lines.append("请确认统计区间:")
        for i, item in enumerate(payload.get("times") or [], 1):
            lines.append(f"{i}) {item.get('label')}")
    lines.append("")
    lines.append(
        "直接回复序号或说法即可; 如果都不是, 说「都不是」我会告诉你现在能用哪些口径。"
    )
    return "\n".join(lines)


def _refuse_text(state: AskDataState) -> str:
    """拒答正文: 把校验器的确定拒因翻译给人看, 并列出现在能用什么。

    这里刻意不"换个口径先答个差不多"的: 问数里错的数比没数危险得多。
    """
    check = state.get("validate_result") or {}
    errors = check.get("errors") or []
    reason = "; ".join(f"{e.get('message')}" for e in errors[:3]) or state.get("error_hint") or ""
    if not reason:
        reason = "没能把你的问题对应到一个已登记且可执行的口径"
    hint = ""
    metrics = (state.get("candidates") or [])[:3]
    if metrics:
        hint = "、".join(str(m.get("label") or m.get("id")) for m in metrics)
    else:
        hint = "报销总额/采购申请金额/高风险合同数 等经营指标(说“有哪些口径”可以列全)"
    return prompts.REFUSE_TEXT.format(reason=reason, hint=hint)


def _codes_of(attempts: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for attempt in attempts:
        for code in re.findall(r"E_[A-Z_]+", str(attempt.get("error") or "")):
            if code not in codes:
                codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# 多轮上下文/量级/图表的小函数(全部确定性, 不进 prompt 也不交给模型判断)
# ---------------------------------------------------------------------------
def _compact_dsl(dsl: dict[str, Any]) -> dict[str, Any]:
    """上一轮 DSL 的紧凑摘要(只带口径 id/维度/时间/过滤, 不带空字段)。

    为什么不让模型看完整 DSL: 它会连着 `limit`/`order_by` 这些本轮应该重新听用户的话
    一起拄过来; 摘要只保留"已经确认的口径与已确定的限定"。
    """
    if not dsl:
        return {}
    out: dict[str, Any] = {}
    metrics = [str((m or {}).get("id")) for m in (dsl.get("metrics") or []) if isinstance(m, dict) and m.get("id")]
    if metrics:
        out["metrics"] = metrics
    if dsl.get("dims"):
        out["dims"] = list(dsl["dims"])
    if dsl.get("time"):
        out["time"] = dsl["time"]
    if dsl.get("filters"):
        out["filters"] = [
            {"dim": f.get("dim"), "op": f.get("op"), "value": f.get("value")}
            for f in dsl["filters"] if isinstance(f, dict)
        ]
    if dsl.get("compare") and dsl["compare"] != "none":
        out["compare"] = dsl["compare"]
    return out


def _slot_confidence(slots: dict[str, Any]) -> dict[str, Any]:
    """槽位里的"说法 -> 置信度"(喂给链接当先验)。

    同一个数值给 metric/dim/filter 的说法共用: 模型只给到槽位级置信度, 拆不到每个词。
    """
    conf = slots.get("confidence") or {}
    out: dict[str, Any] = {}
    for key, bucket in (("metric", "metric_terms"), ("dim", "dim_terms"), ("filter", "filter_terms")):
        value = conf.get(key)
        if value is None:
            continue
        for item in slots.get(bucket) or []:
            phrase = str((item or {}).get("phrase") if isinstance(item, dict) else item or "").strip()
            if phrase:
                out[phrase] = value
    time_phrase = str(((slots.get("time") or {}).get("phrase")) or "").strip()
    if time_phrase and conf.get("time") is not None:
        out[time_phrase] = conf["time"]
    return out


def _unit_of(state: AskDataState, metric_id: str) -> str:
    """从候选里拿这个口径的单位(候选是语义层给的, 不靠模型写)。"""
    for item in state.get("candidates") or []:
        if item.get("kind") == "metric" and str(item.get("id")) == metric_id:
            return str(item.get("unit") or "")
    for item in (state.get("payload") or {}).get("metrics") or []:
        if str(item.get("id")) == metric_id:
            return str(item.get("unit") or "")
    return ""


def _metric_summary(state: AskDataState, result: dict[str, Any]) -> dict[str, Any]:
    """本轮主指标量级(给历史基线用): 每个指标列取第一行的值。

    只存一个代表值而不是整张结果表: 基线只需要量级, 而存整张表等于把行级权限的边界
    从数据库搬到留痕表里重做一遍。
    """
    rows = list(result.get("rows") or [])
    if not rows:
        return {}
    metrics: dict[str, float] = {}
    for column in list(result.get("columns") or []):
        if not str(column).startswith("m_"):
            continue
        # 对比列(m_*_prev / m_*_mom_pct)不参与基线: 它是"上期那个数"与"变化率",
        # 拿它们当主指标量级去算 z-score 会自己跟自己循环比较。
        if str(column).endswith(("_prev", "_mom_pct", "_yoy_pct")):
            continue
        for row in rows:
            value = row.get(column)
            if isinstance(value, (int, float)):
                metrics[str(column)] = float(value)
                break
    if not metrics:
        return {}
    dims = sorted({str(d) for d in (state.get("dsl") or {}).get("dims") or []})
    return {"shape": ",".join(dims), "metrics": metrics, "rowcount": len(rows)}


def _clarify_choice(state: AskDataState) -> tuple[str, str]:
    """本轮若是澄清点选的回复: 返 ``(用户选的口径, 上一轮最高分口径)``。

    两者相等就是"本可以不问"(误澄清率的分子)。靠自然语言里带的 id 判而不是靠"上一轮
    是不是澄清了": 后者会把"用户自己重新打了一遍相同口径"也算成误澄清。
    """
    question = state.get("question", "") or ""
    if not question.startswith(CONFIRM_PREFIX):
        return "", ""
    chosen = ""
    for item in (state.get("dsl") or {}).get("metrics") or []:
        if isinstance(item, dict) and item.get("id"):
            chosen = str(item["id"])
            break
    return chosen, str(state.get("prior_top_metric") or "")


def _needs_ticket(state: AskDataState) -> bool:
    """哪些情形式"该由人看"而不是"让用户再试一次说法"。"""
    codes = set(state.get("error_codes") or [])
    return bool({"E_ENGINE", "E_ENGINE_BREAKER", "E_SERVICE"} & codes)


def _ticket_reason(state: AskDataState) -> str:
    codes = set(state.get("error_codes") or [])
    if {"E_ENGINE", "E_ENGINE_BREAKER", "E_SERVICE"} & codes:
        return "engine_error"
    if "E_UNKNOWN_METRIC" in codes or "E_NO_PERMISSION" in codes:
        return "no_metric"
    return "user_request"


def _chart_shape(
    state: AskDataState, columns: list[str], rows: list[dict[str, Any]], labels: dict[str, str],
) -> dict[str, Any]:
    """结果 shape -> 图型与数据系列(规则在 prompts.chart_hint, 这里只做装配)。

    三个分支都是确定性的: 时间序列走折线; 同环比走**分组柱(当期/上期两系列)**; 两个维度
    走"按第二个维度分组的柱图"(即堆叠效果的可读版, 不需要新渲染器)。
    """
    empty = {"kind": "none", "cats": [], "series": None, "values": None}
    if not rows or not columns:
        return empty
    dim_cols = [c for c in columns if not str(c).startswith("m_")]
    metric_cols = [c for c in columns if str(c).startswith("m_")]
    if not metric_cols:
        return empty
    primary = metric_cols[0]
    has_compare = any(str(c).endswith(("_prev", "_mom_pct", "_yoy_pct")) for c in columns)
    kind = prompts.chart_hint(
        len(dim_cols), "period" in columns, len(rows),
        has_compare=has_compare, metric_count=len(metric_cols),
    )
    if kind == "none":
        return empty
    head = rows[:20]
    cats = [str(r.get(dim_cols[0], "")) if dim_cols else str(r.get("period", "-")) for r in head]
    if len(dim_cols) >= 2 and kind not in ("line",):
        # 二维: 第一个维度做类目, 第二个维度做系列(热力图最多 8 行, 柱图取前 4 个系列)。
        first, second = dim_cols[0], dim_cols[1]
        max_groups = 8 if kind == "heatmap" else 4
        groups: list[str] = []
        for row in head:
            name = str(row.get(second, ""))
            if name not in groups:
                groups.append(name)
        series = []
        for name in groups[:max_groups]:
            data = [row.get(primary) for row in head if str(row.get(second, "")) == name]
            expected = [str(row.get(second, "")) == name for row in head].count(True)
            series.append({"name": f"{name}", "data": (data + [None] * (expected - len(data)))[:20]})
        # 类目取第一个维度的出现顺序(去重), 与系列长度对齐。
        ordered: list[str] = []
        for row in head:
            value = str(row.get(first, ""))
            if value not in ordered:
                ordered.append(value)
        cats = ordered[:20]
        return {"kind": "heatmap" if kind == "heatmap" else "bar",
                "cats": cats, "series": series, "values": None}
    if has_compare and kind != "line":
        series = [
            {"name": labels.get(primary, "当期"), "data": [r.get(primary) for r in head]},
            {"name": labels.get(f"{primary}_prev", "上期"), "data": [r.get(f"{primary}_prev") for r in head]},
        ]
        return {"kind": "bar", "cats": cats, "series": series, "values": None}
    if kind == "pie":
        return {"kind": "pie", "cats": cats, "series": None,
                "values": [r.get(primary) for r in head]}
    series = [{"name": labels.get(c, c), "data": [r.get(c) for r in head]} for c in metric_cols[:2]]
    if kind == "line" and len(metric_cols) < 2:
        return {"kind": "line", "cats": cats, "series": None,
                "values": [r.get(primary) for r in head]}
    return {"kind": kind, "cats": cats, "series": series, "values": None}


def _display_range(dsl: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """给人看的统计区间: 只由定稿 DSL 的时间块现算。

    为什么不只信上游回执: 回执可能是另一份 DSL(回炉前的、或槽位里的模型自算日期)算出来的,
    那会让"表里的数"与"开头那句统计区间"讲的是两件不同的事 —— 用户看不出来, 只会拿着
    错区间的结论去汇报。拿不到合法 DSL 时间时退回原回执(不阻断本轮)。
    """
    time_block = dsl.get("time")
    if not isinstance(time_block, dict) or not (time_block.get("range") or time_block.get("start")):
        return fallback
    compare = dsl.get("compare")
    compare_type = str(compare.get("type") or "none") if isinstance(compare, dict) else str(compare or "none")
    try:
        resolved = timesem.resolve(time_block, compare=compare_type)
    except Exception as exc:  # noqa: BLE001 - 展示层不该把已算好的数抖掉
        logger.debug("[askdata] 展示区间现算失败, 退回回执: %s", str(exc)[:120])
        return fallback
    payload = resolved.to_payload()
    return payload


def _route_failed_text(attempts: list[dict[str, Any]], state: AskDataState) -> str:
    lines = [
        "这个问题现在算不出来: 语义层的三条通道都没能给出可执行的口径。",
        "",
        f"- 指标 API / 编译器 / 沙箱逐条结果: "
        + "; ".join(f"{a.get('level')}={'失败' if not a.get('ok') else '未通过'}" for a in attempts),
    ]
    for attempt in attempts:
        if attempt.get("error"):
            lines.append(f"- {attempt.get('level')}: {attempt['error']}")
    candidates = (state.get("candidates") or [])[:3]
    if candidates:
        lines.append("")
        lines.append("最接近的已登记口径: "
                     + "; ".join(f"{c.get('label')}({c.get('caliber') or ''})" for c in candidates))
    lines.append("")
    lines.append("如果这就是你要的口径, 换个说法再问一次; 如果不是, 这个口径需要先在语义层登记。")
    return "\n".join(lines)


async def _compose_answer(
    pipeline: "AskDataPipeline", state: AskDataState, result: dict[str, Any], verdict: str
) -> str:
    """正文 = 叙述 + 结果表 + 图表链接 + 口径与范围说明。"""
    columns = list(result.get("columns") or [])
    rows = list(result.get("rows") or [])
    labels = result.get("column_labels") or {}
    parts: list[str] = []
    if verdict == "degraded" and state.get("repair_text"):
        parts.append(state["repair_text"])
    elif verdict in ("fail", "degraded"):
        problems = (state.get("verify") or {}).get("problems") or []
        if problems:
            parts.append("⚠️ 结果校验存疑，以下数字请人工复核：" + "; ".join(problems[:3]))
    else:
        if state.get("narrative"):
            parts.append(state["narrative"])
    table = _md_table(columns, labels, rows)
    if table:
        parts.append(table)
        if len(rows) > 20:
            parts.append(f"(共 {len(rows)} 行, 以上仅展示前 20 行)")
    chart = state.get("chart") or {}
    if chart.get("url"):
        parts.append(f"![图表]({chart['url']})")
        parts.append(f"[查看图表]({chart['url']})")
    notes = result.get("caliber_notes") or []
    tail = ["", f"> 数据范围: {result.get('scope_note', '-')}"]
    if notes:
        tail.append("> 统计口径: " + "; ".join(notes))
    if state.get("catalog_origin") and state["catalog_origin"] != "db":
        tail.append(f"> 注意: 本轮口径来自内置兜底集(catalog={state['catalog_origin']}), 未经语义层治理版本确认。")
    parts.append("\n".join(tail))
    return "\n\n".join(p for p in parts if p).strip()


def _as_dict_result(raw: Any) -> dict[str, Any]:
    """把 MCP 工具返回(content blocks / 结构化 list / dict / JSON 字符串)统一成一个 dict。

    形状不统一是真的: langchain-mcp-adapters 对 `dict` 工具会给 `[{"type":"text","text":...}]`,
    对 `list[dict]` 工具又会直接给列表。不能拿"第一项是 dict"就当结果体 ——
    那会拿到 `{"type": "text", "text": "..."}` 这种块, 看上去是一个空结果而不是报错
    (实际踩过: 表现为"语义层没这个指标"的误导拒因)。
    """
    from app.assistant.mcp_client import flatten_mcp_result

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw and all(isinstance(b, dict) for b in raw):
        if not any("type" in b or "text" in b for b in raw):
            # 直接的结构化行列表(analytics 域 run_sql 那种形状)
            return raw[0] if len(raw) == 1 else {"rows": raw, "rowcount": len(raw)}
    text = flatten_mcp_result(raw)
    if not text:
        return {"error": "MCP 工具返回了空结果"}
    try:
        parsed = json.loads(text)
    except ValueError:
        # FastMCP 把工具内异常以纯文本返回来("Error executing tool xxx: ...")。
        # 不判它就会变成"一个没有候选的合法结果", 上层因此报"语义层没这个指标"
        # 而不是"工具本身炸了" —— 这两件事必须分开(踩过)。
        if text.lstrip().lower().startswith(("error", "traceback", "validation error")):
            return {"error": text[:400]}
        return {"text": text}
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0] if len(parsed) == 1 else {"rows": parsed, "rowcount": len(parsed)}
    return {"value": parsed}


def _track(state: AskDataState, text: str) -> None:
    logger.debug("[askdata] %s", text)


def time_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
