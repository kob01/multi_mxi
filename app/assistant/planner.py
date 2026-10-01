"""复合问法判断与拆分器(多任务并行的第一环, 三层任务图架构)。

一句话问多件事("查查我年假还剩几天, 明天北京天气怎么样")在单意图链路上必然
丢一半: 整句只产出一个 ``IntentResult``, 而 ``(TOOL_CALL, hr)`` 与
``(TOOL_CALL, web)`` 两组种子互相把对方的 margin 压下去, 判定沉到 LLM 层后
``INTENT_PROMPT`` 又只允许回答一个 intent/target。本模块把这种句子拆成**一张可执
行的任务图**(若干条可独立执行的子任务)。

三层判断条件(自左向右逐层收紧, 越靠前越廉价):
- Layer1 ``triage``: 零网络快速规则门。"单域槽位叠加"(同主体多疑问词, 如"升旗是哪天,
  时间几点")直接判 ``single`` 不进 LLM; 显式并列词/跨域动词判 ``plan`` 进 Layer2;
  无分隔无连接词判 ``skip`` 走单意图。
- Layer2 ``split``: LLM 只输出结构化任务图(goal/tool_hint/readonly/depends_on), 不出意图。
- Layer3 ``validate_task_graph``: 代码校验器(准确性主要靠它) —— 解析容错、同工具同实体
  无依赖自动合并、depends_on 环检测、写操作降级、条数截断; 校验不通过一律返回空列表。

刻意只拆问题、不出意图: 意图口径的单一事实源在 ``app/assistant/intent.py`` 的三层漏斗
(``_RULES`` / ``_SEEDS`` / ``layer`` 审计), ``tool_hint``/``readonly`` 只作调度提示, 子任务
的 intent/target 仍逐条走 ``IntentRecognizer.classify``(见 ``graph.plan_tasks``)。

降级风格与意图漏斗一致: 任何异常/脏输出都返回空列表, 空列表 == 不拆, 链路原样走今天
的单意图路径 —— 拆分器永远不会成为对话链路的故障点。
"""

from __future__ import annotations

import json
import logging
import re

from app.assistant.prompts import MULTI_TASK_SPLIT_PROMPT
from app.cache.prompt_cache import cached_llm_call
from app.config import get_settings
from app.llm import get_chat_model

logger = logging.getLogger(__name__)

# 与意图 LLM 兜底层同一温度与模型(intent_model 是分类/判定专用档)。
_LLM_TEMPERATURE = 0.2
# 单条子问题的长度上限: 超过视为 LLM 把整段解释塞进了数组。
_MAX_TASK_LEN = 200
# 输出清洗要剥掉的引号字符(与 graph._clean_rewrite 同一套口径)。
_QUOTE_CHARS = "\"'`“”‘’「」『』《》"

# ---------- Layer1 快速规则门的标记 ----------
# 显式并列/顺承连接词: 命中即认定用户想把多件事放一起 -> 进 LLM 规划(plan)。
_PARALLEL_CONNECTORS = re.compile(
    r"(同时|分别|另外|还有|顺便|以及|并且|然后|接着|再帮|再来|再查|再看|也查|也看|都查)"
)
# 域动词: 从句里出现独立动作词说明它自带主语/诉求, 不是挂在主句上的槽位。
_DOMAIN_VERBS = re.compile(r"(查|看|帮我|办理|申请|提交|下单|发起|生成|对比|抓取|算)")
# 疑问词/属性槽位: 单域槽位叠加的识别核心(哪天+几点+多少钱+在哪里...)。
_INTERROGATIVES = re.compile(
    r"(哪天|哪月|几号|几点|什么时候|何时|多少|多少钱|哪里|哪儿|怎么|怎样|是否|什么|"
    r"价格|费用|时间|地点|地址|颜色|尺寸|规格|进度|金额|余额|几天|几个)"
)
# 句读分隔符: 切从句用。
_CLAUSE_SEP = re.compile(r"[，,、；;]")
# 槽位片段最大长度: 超过视为带了自己的主语, 不再是"时间几点"这类裸槽位。
_SLOT_MAX_LEN = 8


def _is_slot_fragment(clause: str) -> bool:
    """从句是否是"裸槽位"(同主体追问的一个属性, 如"时间几点""多少钱")。

    短、含疑问/属性词、且没有独立域动词或并列连接词 —— 三者同时满足才判为槽位,
    避免把"明天北京天气怎么样"这种自带主语的分句误判成槽位。
    """
    text = clause.strip()
    if not text or len(text) > _SLOT_MAX_LEN:
        return False
    if _DOMAIN_VERBS.search(text) or _PARALLEL_CONNECTORS.search(text):
        return False
    return bool(_INTERROGATIVES.search(text))


# ---------- Layer3 校验器: goal 归一化核心与相似度 ----------
# 剥掉标点后要再剥的疑问/虚词/动词, 只留实体核心("天安门下次升旗是哪天"->"天安门下次升旗")。
_CORE_STRIP = re.compile(
    r"(哪天|哪月|几号|几点|什么时候|何时|多少|多少钱|哪里|哪儿|怎么|怎样|是否|什么|"
    r"价格|费用|时间|地点|地址|颜色|尺寸|规格|进度|金额|余额|几天|几个|"
    r"请问|帮我|一下|查查|查一下|查询|查|看看|看|是|的|有|吗|呢|吧|了)"
)
_PUNCT = re.compile(r"[，,、；;：:。.！!？?\s“”\"']")


def _content_core(goal: str) -> str:
    """把子问题归一化成"实体核心": 剥标点 + 剥疑问/虚词/动词, 用于同实体判定。"""
    text = _PUNCT.sub("", goal or "")
    return _CORE_STRIP.sub("", text)


def _dice(a: str, b: str) -> float:
    """字符二元组的 Dice 系数(0~1); 任一侧为空返回 0。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    inter = len(ga & gb)
    return 2 * inter / (len(ga) + len(gb))


def _has_cycle_or_dangling(tasks: list[dict]) -> bool:
    """depends_on 是否构成非法图: 有环、自依赖或引用不存在的 id。"""
    ids = {t["id"] for t in tasks}
    for t in tasks:
        for dep in t["depends_on"]:
            if dep == t["id"] or dep not in ids:
                return True
    # Kahn 拓扑: 若无法出完全部节点则存在环。
    indeg = {t["id"]: len(t["depends_on"]) for t in tasks}
    children: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    for t in tasks:
        for dep in t["depends_on"]:
            children[dep].append(t["id"])
    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in children[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return seen != len(tasks)


def validate_task_graph(
    raw: str,
    max_tasks: int,
    *,
    merge_enabled: bool = True,
    overlap_threshold: float = 0.75,
) -> tuple[list[dict], int]:
    """把拆分器输出的任务图 JSON 清洗+校验成可执行子任务列表(纯函数, 可离线单测)。

    返回 ``(保留的任务, 因超出 max_tasks 被截断的条数)``。任务字段:
    ``{id, goal, tool_hint, readonly, depends_on, deferred}``。

    校验顺序: 解析容错 -> 逐条清洗 -> 同工具同实体无依赖合并 -> 环/悬空检测 ->
    截断 -> **有效条数 < 2 时返回空列表**(语义"用户其实只问了一件事", 走单意图)。
    任何一步抛异常一律返回 ``([], 0)``, 拆分器绝不成为故障点。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [], 0
    items = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], 0

    tasks: list[dict] = []
    seen_goals: set[str] = set()
    for i, item in enumerate(items):
        # 兼容旧形状: 裸字符串子问题 -> 当作只读、无 tool_hint 的单节点。
        if isinstance(item, str):
            goal = item.strip().strip(_QUOTE_CHARS).strip()
            tool_hint, readonly, deps = "", True, []
        elif isinstance(item, dict):
            goal = str(item.get("goal") or "").strip().strip(_QUOTE_CHARS).strip()
            tool_hint = str(item.get("tool_hint") or "").strip().lower()
            readonly = bool(item.get("readonly", True))
            raw_deps = item.get("depends_on")
            deps = [str(d).strip() for d in raw_deps if str(d).strip()] if isinstance(raw_deps, list) else []
        else:
            continue
        if not goal or len(goal) > _MAX_TASK_LEN or goal in seen_goals:
            continue
        seen_goals.add(goal)
        tid = f"t{len(tasks) + 1}"  # 一律重编号, 避免 LLM 给的 id 重复/缺失
        tasks.append(
            {
                "id": tid,
                "goal": goal,
                "tool_hint": tool_hint,
                "readonly": readonly,
                "depends_on": deps,
                "deferred": not readonly,  # 写操作 -> 串行且不自动执行(待确认)
            }
        )

    if not tasks:
        return [], 0

    # 先把 depends_on 里的旧 id(若有)映射到新重编号: LLM 用 t1/t2... 我们同序重编号,
    # 位置一致即可直接沿用; 若 LLM id 与位置错位则依赖会被环检测判非法并整体回退。
    if merge_enabled:
        tasks = _merge_same_entity(tasks, overlap_threshold)

    if _has_cycle_or_dangling(tasks):
        logger.info("任务图依赖非法(环/悬空), 本轮按单意图处理")
        return [], 0

    limit = max(2, max_tasks)
    dropped = max(0, len(tasks) - limit)
    tasks = tasks[:limit]
    if len(tasks) < 2:
        return [], 0
    return tasks, dropped


def _merge_same_entity(tasks: list[dict], overlap_threshold: float) -> list[dict]:
    """同 tool_hint + 同实体核心 + 互不依赖的两条子任务合并为一条(收敛 LLM 误拆)。"""
    kept: list[dict] = []
    for t in tasks:
        merged_into = None
        core = _content_core(t["goal"])
        for k in kept:
            if k["tool_hint"] != t["tool_hint"]:
                continue
            kcore = _content_core(k["goal"])
            same = (core and core == kcore) or _dice(core, kcore) >= overlap_threshold
            # 互不依赖才允许合并(有向依赖说明用户确实要两件事先后做)。
            independent = (
                k["id"] not in t["depends_on"] and t["id"] not in k["depends_on"]
            )
            if same and independent:
                merged_into = k
                break
        if merged_into is None:
            kept.append(t)
        else:
            # 被合并节点的下游依赖重定向到保留节点, 依赖并集保留。
            merged_into["depends_on"] = sorted(
                set(merged_into["depends_on"]) | set(t["depends_on"])
            )
            for other in kept:
                if t["id"] in other["depends_on"]:
                    other["depends_on"] = [
                        merged_into["id"] if d == t["id"] else d for d in other["depends_on"]
                    ]
    return kept


class TaskPlanner:
    """复合问法 -> 任务图(一次 LLM 调用, 走 Prompt Cache)。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(
            settings.intent_model, temperature=_LLM_TEMPERATURE, json_mode=True
        )

    def triage(self, message: str) -> str:
        """Layer1 零网络规则门: 返回 ``"single"`` / ``"plan"`` / ``"skip"``。

        - ``single``: 单域槽位叠加(同主体多疑问词), 直接走单意图, 不进 LLM 拆分;
        - ``plan``: 有显式并列词或跨域动词, 交给 Layer2 规划;
        - ``skip``: 总开关关闭 / 太短 / 无任何分隔与并列标记, 走单意图。
        """
        if not self._settings.multi_task_enabled:
            return "skip"
        text = (message or "").strip()
        if len(text) < self._settings.multi_task_min_chars:
            return "skip"
        # 显式并列连接词优先判 plan(不受槽位规则影响, 如"分别查年假和社保")。
        if _PARALLEL_CONNECTORS.search(text):
            return "plan"
        clauses = [c for c in _CLAUSE_SEP.split(text) if c.strip()]
        if len(clauses) < 2:
            # 无从句分隔: 单一诉求, 省一次 LLM 调用。
            return "skip"
        trailing = clauses[1:]
        # 所有从句都是裸槽位 -> 同主体追问多个属性 -> 单任务。
        if all(_is_slot_fragment(c) for c in trailing):
            return "single"
        return "plan"

    async def split(self, message: str, history: str = "") -> tuple[list[dict], int]:
        """拆出任务图; 返回 ``(任务列表, 被截断条数)``, 少于两条或任何失败都是 ([], 0)。"""
        max_tasks = max(2, self._settings.multi_task_max_subtasks)
        prompt = MULTI_TASK_SPLIT_PROMPT.format(
            max_tasks=max_tasks, history=history or "(无)", message=message
        )

        async def _invoke() -> str:
            # 命名 LLM run, 便于在 LangSmith trace 树里定位这次拆分调用。
            resp = await self._llm.ainvoke(
                prompt, config={"run_name": "plan_tasks", "tags": ["plan_tasks"]}
            )
            return str(resp.content)

        try:
            # Prompt Cache: 拆分是纯函数式调用(同 prompt -> 同 JSON), 不含权限
            # 与实时数据, 与意图 LLM 兜底层同等可缓存。
            raw = await cached_llm_call(
                self._settings.intent_model, _LLM_TEMPERATURE, prompt, _invoke
            )
            return validate_task_graph(
                raw,
                max_tasks,
                merge_enabled=self._settings.multi_task_merge_enabled,
                overlap_threshold=self._settings.multi_task_core_overlap_threshold,
            )
        except Exception as exc:  # noqa: BLE001 - 拆不开就按单意图办
            logger.warning("多任务拆分失败, 本轮按单意图处理: %s", exc)
            return [], 0


_planner: TaskPlanner | None = None


def get_planner() -> TaskPlanner:
    """进程级单例(构造期只建 LLM 客户端, 无 I/O)。"""
    global _planner
    if _planner is None:
        _planner = TaskPlanner()
    return _planner
