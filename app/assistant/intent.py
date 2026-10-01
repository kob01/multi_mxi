"""Intent recognition: a three-layer funnel ordered by cost / determinism.

    Layer 1 (rule)     -- regex/keyword fast-match for high-frequency fixed
                          commands (转人工/查订单号/签到 ...). Synchronous,
                          zero external call, highest confidence when matched.
    Layer 2 (embedding) -- bge-m3 semantic classifier. One query embedding is
                          compared (cosine) against a built-in seed set per
                          (intent, target); a group wins only when its Top-K
                          mean score clears the accept threshold AND leads the
                          runner-up by the margin. Skipped on any failure.
    Layer 3 (llm)      -- DeepSeek LLM (INTENT_PROMPT) as the catch-all for
                          long / ambiguous utterances that the cheap layers
                          can't confidently route.
    fallback           -- deterministic keyword heuristics, the terminal safety
                          net when the LLM itself fails, so routing never
                          crashes.

Each hit short-circuits the funnel; a layer returning ``None`` means "not
confident, sink to the next one". Every result is tagged with ``layer`` so the
``classify_intent`` node can audit which stage produced the decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time

from app.assistant.prompts import INTENT_PROMPT
from app.cache.prompt_cache import cached_llm_call
from app.config import get_settings
from app.llm import get_chat_model
from app.rag.embeddings import OllamaEmbedder
from app.schemas import IntentResult, IntentType

logger = logging.getLogger(__name__)

# 领域关键词: 供规则层判定业务域(target)与终端关键词兜底复用。
# 顺序即优先级(字典保持插入序): 先具体业务对象(finance/hr), 再报表/采购这类
# 可能与其他域重叠的域; analytics 排在 procurement 前, 使"采购报表"归数据分析。
_AGENT_KEYWORDS = {
    "finance": ("报销", "费用", "发票", "借款", "付款", "预算"),
    "hr": ("入职", "离职", "在职证明", "证明", "考勤", "请假", "年假申请", "工单"),
    "analytics": ("统计", "报表", "周报", "月报", "数据分析", "趋势", "占比", "汇总",
                  "图表", "经营", "看板", "洞察", "排名", "分布"),
    "procurement": ("采购", "合同", "供应商", "比价", "招标", "框架协议", "下单", "进货"),
    # 能力域(计划 D5): web=联网检索, docgen=文件生成(office/PDF/MD)。两者都映射
    # tool_call(见 _fallback), 放在最后是刻意的: 显式格式词/联网动词由 _RULES 先拦。
    "web": ("最新", "近期", "最近", "新闻", "网上", "联网", "热搜", "实时", "股价", "汇率", "天气"),
    "docgen": ("word", "docx", "excel", "xlsx", "ppt", "pptx", "pdf", "文档", "表格", "幻灯片", "导出", "markdown", "md"),
}
_TOOL_PATTERNS = re.compile(r"(FIN\d+|HR\d+|PO\d+|CT\d+|余额|进度查询|查询单号)")
_FIN_CODE = re.compile(r"FIN\d+")
_HR_CODE = re.compile(r"HR\d+")
# 采购单 PO#### / 合同 CT#### 单号: 命中即 tool_call 且归 procurement 域。
_PO_CODE = re.compile(r"PO\d+")
_CT_CODE = re.compile(r"CT\d+")
# 文件生成(可下载的 office/PDF/MD 文件)的规则: 必须带明确格式词/导出动词 —— 否则
# "生成周报"(analytics)也会被抢过来。规则层与 _detect_target 共用这一个正则, 避免两处漂移。
_FILE_GEN_RE = re.compile(
    r"(导出|输出|生成|做成|做|写|整)[^。！？]{0,8}(word|docx|excel|xlsx|ppt|pptx|pdf|幻灯片|文档|表格|markdown|\bmd\b)", re.I
)
# 出现以下相对时间词即视为"依赖当前时间"的问题, 需先取平台时间再作答。
# 仅用于 resolve_time 节点决定是否预取时钟, 不参与意图分类。
_TIME_CONTEXT_PATTERNS = re.compile(
    r"(今天|明天|昨天|后天|前天|现在|当前|目前|此刻|几号|几点|星期|周几|礼拜|"
    r"今年|去年|明年|本月|这个月|上个月|下个月|本季度|上季度|下季度|本年度|"
    r"月初|月底|年初|年末|截止|截至|还有几天|剩余几天|时效|过期|到期|"
    r"最新|最近|近期)"
)

# ---------------------------------------------------------------- 第一层: 规则
# 高频固定指令 -> 直接判定意图。列表按优先级有序匹配, 新增指令只需追加。
# 委派类(需专业系统多步办理)排在查询类前面, 避免"我要报销"被判为 tool_call。
_RULES: list[tuple[re.Pattern, IntentType]] = [
    # 文件生成(office/PDF/MD): 交付物是可下载文件, 规则与 _detect_target 共用 _FILE_GEN_RE。
    (_FILE_GEN_RE, IntentType.TOOL_CALL),
    # 联网检索(计划 D5): 规则只兜显式联网动词; "最新/新闻/天气"这类靠语义层+关键词兜底。
    (re.compile(r"(联网|上网|网上搜|搜索一下|搜一下|帮我搜|查一下网上|百度一下)"), IntentType.TOOL_CALL),
    (re.compile(r"(我要|帮我|申请|开始|发起|办理).{0,4}(报销|费用核销)"), IntentType.AGENT_DELEGATE),
    (re.compile(r"(开|办|申请).{0,4}(在职证明|收入证明|离职证明|证明)"), IntentType.AGENT_DELEGATE),
    (re.compile(r"(申请|我要|办理).{0,4}(离职|入职|转正)"), IntentType.AGENT_DELEGATE),
    # 数据分析: "生成/出/做一份周报/月报/报表" 这类成文成图诉求委派 Analyst_Agent。
    (re.compile(r"(生成|出|做|来|写|整).{0,6}(周报|月报|报表|分析报告|经营报告|图表)"), IntentType.AGENT_DELEGATE),
    # 采购/合同: 送审合同、发起采购委派 Contract_Agent(多步办理 + 初审)。
    (re.compile(r"(审|审查|初审|送审|合规|把关).{0,4}(合同|采购)"), IntentType.AGENT_DELEGATE),
    (re.compile(r"(申请|我要|发起|办理|提).{0,4}(采购|下单|进货)"), IntentType.AGENT_DELEGATE),
    (re.compile(r"FIN\d+|HR\d+|PO\d+|CT\d+"), IntentType.TOOL_CALL),
    (re.compile(r"(查询|查|看下|帮我查).{0,6}(单号|订单号|进度|到哪一步|余额|额度|预算|到账)"), IntentType.TOOL_CALL),
    (re.compile(r"签到|打卡"), IntentType.TOOL_CALL),
]

# ---------------------------------------------------------------- 第二层: 种子
# 每个 (意图, 业务域) 的代表话术, 用于 bge-m3 语义最近邻分类。随版本演进维护。
_SEEDS: dict[tuple[IntentType, str | None], list[str]] = {
    (IntentType.KNOWLEDGE_QA, None): [
        "年假有几天", "带薪年休假是怎么规定的", "差旅费报销标准是多少",
        "公司的考勤制度是怎样的", "加班怎么调休", "公积金缴纳比例是多少",
        "报销需要哪些发票", "公司的晋升机制是什么",
    ],
    (IntentType.TOOL_CALL, "finance"): [
        "查询 FIN5000 报销单", "我的报销款到账了吗", "查一下这张发票的报销进度",
        "研发部本季度预算还剩多少", "我的差旅费报了多少", "查一下借款到账情况",
    ],
    (IntentType.TOOL_CALL, "hr"): [
        "查我的年假余额", "帮我签到", "今天的打卡记录", "查一下我的考勤明细",
        "还有几天年假", "查询工单处理进度",
    ],
    (IntentType.AGENT_DELEGATE, "finance"): [
        "我要报销", "帮我提交报销申请", "这笔费用怎么核销", "我要开发票", "申请一笔借款",
    ],
    (IntentType.AGENT_DELEGATE, "hr"): [
        "帮我开在职证明", "我要申请离职", "办理入职手续", "开一份收入证明", "申请转正",
    ],
    (IntentType.TOOL_CALL, "analytics"): [
        "统计各部门本季度报销金额", "研发部今年报销趋势", "各类费用占比是多少",
        "看一下经营看板数据", "市场部月度费用汇总图表",
    ],
    (IntentType.AGENT_DELEGATE, "analytics"): [
        "生成本周经营周报", "帮我做一份月度分析报告", "出一份费用趋势图",
        "写个季度数据分析报告", "做个部门预算对比图表",
    ],
    (IntentType.TOOL_CALL, "procurement"): [
        "查一下 PO3000 采购单", "CT8000 合同初审到哪了", "我的采购单进度",
        "查在册供应商名单",
    ],
    (IntentType.AGENT_DELEGATE, "procurement"): [
        "我要发起一笔采购", "帮我审一下这份合同", "申请采购十台电脑",
        "这份采购合同合规吗", "提一个供应商准入",
    ],
    # 能力域(计划 D5): 交付物是"带来源的联网检索结果"与"可下载的文件"。
    (IntentType.TOOL_CALL, "web"): [
        "搜一下今天的科技新闻", "最新的人工智能进展有哪些", "现在美元兑人民币汇率是多少",
        "最近一周的行业动向", "查一下网上这家公司的背景资料",
    ],
    (IntentType.TOOL_CALL, "docgen"): [
        "把这份数据做成 Excel 表格", "生成一份季度总结的 Word 文档", "帮我导出一份 PDF 报告",
        "做一份项目汇报 PPT", "把报销明细导出成表格文件",
    ],
    (IntentType.CHITCHAT, None): [
        "你好", "在吗", "谢谢", "再见", "你是谁", "辛苦了",
    ],
}


def needs_current_time(message: str) -> bool:
    """Whether answering ``message`` requires the platform's current time."""
    return bool(_TIME_CONTEXT_PATTERNS.search(message))


def _detect_target(message: str) -> str | None:
    """Resolve business domain from codes first, then keywords."""
    if _FIN_CODE.search(message):
        return "finance"
    if _HR_CODE.search(message):
        return "hr"
    if _PO_CODE.search(message) or _CT_CODE.search(message):
        return "procurement"
    # 文件生成(可下载的 word/excel/ppt/pdf/md)优先于业务关键词: "把报销明细导出成 excel"
    # 的交付物是文件, 不该被"报销"抢成 finance 域。
    if _FILE_GEN_RE.search(message):
        return "docgen"
    for domain, kws in _AGENT_KEYWORDS.items():
        if any(k in message for k in kws):
            return domain
    return None


def _l2_normalize(vec: list[float]) -> list[float]:
    """Return a unit-length copy so cosine == dot product."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product of two equal-length vectors (cosine on unit vectors)."""
    return sum(x * y for x, y in zip(a, b))


class IntentRecognizer:
    """Three-layer intent funnel: rule -> bge-m3 embedding -> LLM -> keyword."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(
            settings.intent_model, temperature=0.2, json_mode=True
        )
        self._embedder = OllamaEmbedder()
        # (intent, target) -> list of normalized seed vectors (built lazily once).
        self._seed_index: dict[tuple[IntentType, str | None], list[list[float]]] | None = None
        self._embedding_ok = settings.intent_embedding_enabled
        # 种子向量只建一次, 但首建必须单飞: 没有锁时冷启动的第一批并发(或网关重启
        # 后的前 N 个请求)会各自发一次"全部种子文本"的 embedding 批量请求 —— 十几组
        # 种子×六条话术就是十几遍重复的同一批计算, 直接把 Ollama 压到报错。
        self._seeds_lock: asyncio.Lock | None = None
        # 建索引失败的退避时间戳(monotonic): 刻意做的负缓存 —— 否则失败后每个请求
        # 都重发全量 embedding(故障时那就是重试风暴)。
        self._seeds_failed_at = 0.0

    _SEEDS_RETRY_AFTER = 60.0

    def _in_retry_backoff(self) -> bool:
        """处于建索引失败的退避窗口内则 True(调用方直接下沉下一层)。"""
        return bool(self._seeds_failed_at) and (
            time.monotonic() - self._seeds_failed_at < self._SEEDS_RETRY_AFTER
        )

    # ---------------- 第一层: 规则快筛 ----------------

    def _rule_classify(self, message: str) -> IntentResult | None:
        """Deterministic high-frequency command match; short-circuits the funnel."""
        for pattern, intent in _RULES:
            if pattern.search(message):
                target = _detect_target(message)
                return IntentResult(
                    intent=intent,
                    target=target,
                    confidence=0.9,
                    reason=f"rule:{intent.value}:{target or '-'}",
                    layer="rule",
                )
        return None

    # ---------------- 第二层: bge-m3 语义分类 ----------------

    async def _ensure_seeds(self) -> None:
        """Build + cache normalized seed vectors once (best-effort, single-flight).

        建索引失败必须有负缓存退避: 原先只写成功路径, 失败时 ``_seed_index`` 仍为 None
        且 ``_embedding_ok`` 不会转 False, 于是**每一个后续请求都重新发一遍"全部种子"
        的批量 embedding** —— 单飞锁只能挡冷启动风暴, 挡不住故障态下的持续重试风暴
        (而后者恰好是注释里那个"把 Ollama 压到报错"的持续版本)。失败后
        ``_SEEDS_RETRY_AFTER`` 秒内不再重试建索引, 其间直接下沉 LLM 层。
        """
        if self._seed_index is not None or not self._embedding_ok:
            return
        if self._in_retry_backoff():
            return
        if self._seeds_lock is None:
            self._seeds_lock = asyncio.Lock()
        async with self._seeds_lock:
            # 双检: 等锁期间另一个请求已经把种子嵌好了(或刚建索引失败进了退避窗口)
            if self._seed_index is not None or not self._embedding_ok:
                return
            if self._in_retry_backoff():
                return
            labels = list(_SEEDS.keys())
            flat = [text for key in labels for text in _SEEDS[key]]
            label_of = [key for key in labels for _ in _SEEDS[key]]
            try:
                vectors = await self._embedder.embed(flat)
            except Exception as exc:  # noqa: BLE001 - 建索引失败只能降级, 不能卡住意图层
                self._seeds_failed_at = time.monotonic()
                logger.warning(
                    "意图种子向量建索引失败, %d 秒内不再重试(其间下沉 LLM): %s",
                    int(self._SEEDS_RETRY_AFTER), exc,
                )
                return
            index: dict[tuple[IntentType, str | None], list[list[float]]] = {}
            for key, vec in zip(label_of, vectors):
                index.setdefault(key, []).append(_l2_normalize(vec))
            self._seed_index = index
            self._seeds_failed_at = 0.0
            logger.info("intent seed index built: %d groups, %d vectors", len(index), len(flat))

    def _semantic_classify_sync(self, query_vec: list[float]) -> IntentResult | None:
        """Score all intent groups against a query vector and apply the accept rule."""
        assert self._seed_index is not None
        top_k = max(1, self._settings.intent_semantic_top_k)
        scored: list[tuple[float, IntentType, str | None]] = []
        for (intent, target), vecs in self._seed_index.items():
            sims = sorted((_dot(query_vec, v) for v in vecs), reverse=True)[:top_k]
            mean = sum(sims) / len(sims)
            scored.append((mean, intent, target))
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_intent, best_target = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        gap = best_score - second_score
        if best_score >= self._settings.intent_accept_threshold and gap >= self._settings.intent_margin:
            return IntentResult(
                intent=best_intent,
                target=best_target,
                confidence=round(best_score, 3),
                reason=f"embed:{best_intent.value}:{best_target or '-'}(top1={best_score:.3f},gap={gap:.3f})",
                layer="embedding",
            )
        return None

    async def _semantic_classify(self, query: str) -> IntentResult | None:
        """Embed the query and delegate scoring; sink to LLM on any failure."""
        if not self._embedding_ok:
            return None
        try:
            await self._ensure_seeds()
            if self._seed_index is None:
                return None
            query_vec = _l2_normalize(await self._embedder.embed_query(query))
            return self._semantic_classify_sync(query_vec)
        except Exception as exc:  # Ollama down / dim mismatch -> never block the chain
            logger.warning("intent embedding layer failed, sinking to LLM: %s", exc)
            return None

    # ---------------- 第三层: LLM 兜底 ----------------

    def _parse_llm(self, content: str) -> IntentResult:
        """Parse the JSON intent payload from the model response."""
        data = json.loads(str(content))
        intent = IntentType(data.get("intent", "knowledge_qa"))
        target = data.get("target")
        if target not in ("finance", "hr", "analytics", "procurement", "web", "docgen"):
            target = None
        return IntentResult(
            intent=intent,
            target=target,
            confidence=float(data.get("confidence", 0.5)),
            reason=f"llm:{data.get('reason', '')}",
            layer="llm",
        )

    async def _llm_classify(self, message: str, history: str) -> IntentResult | None:
        """LLM catch-all; returns None on failure so the keyword net engages.

        接入 Prompt Cache: 意图分类是"同一段 prompt -> 同一个 JSON 结果"的纯
        函数式调用, 不涉及权限/实时数据, 完全可以按 prompt 内容缓存。
        """
        prompt = INTENT_PROMPT.format(history=history or "(无)", message=message)

        async def _invoke() -> str:
            resp = await self._llm.ainvoke(prompt)
            return str(resp.content)

        try:
            content = await cached_llm_call(
                self._settings.intent_model, 0.2, prompt, _invoke
            )
            return self._parse_llm(content)
        except Exception as exc:
            logger.warning("intent LLM classification failed, using keyword fallback: %s", exc)
            return None

    # ---------------- 终端安全网: 关键词启发式 ----------------

    def _fallback(self, message: str) -> IntentResult:
        """Keyword-based routing when the model fails."""
        for target, kws in _AGENT_KEYWORDS.items():
            if any(k in message for k in kws):
                if target in ("web", "docgen"):
                    # 能力域没有对应的专业智能体可委派, 只能是 tool_call。
                    return IntentResult(intent=IntentType.TOOL_CALL, target=target, confidence=0.55,
                                        reason=f"keyword:{target}", layer="fallback")
                if _TOOL_PATTERNS.search(message):
                    return IntentResult(intent=IntentType.TOOL_CALL, target=target, confidence=0.55, reason="keyword:tool", layer="fallback")
                return IntentResult(intent=IntentType.AGENT_DELEGATE, target=target, confidence=0.55, reason="keyword:agent", layer="fallback")
        return IntentResult(intent=IntentType.KNOWLEDGE_QA, confidence=0.4, reason="keyword:default_kb", layer="fallback")

    # ---------------- 漏斗入口 ----------------

    async def classify(self, message: str, history: str) -> IntentResult:
        """Route one utterance through rule -> embedding -> LLM -> fallback."""
        result = self._rule_classify(message)
        if result is not None:
            return result
        result = await self._semantic_classify(message)
        if result is not None:
            return result
        result = await self._llm_classify(message, history)
        if result is not None:
            return result
        return self._fallback(message)
