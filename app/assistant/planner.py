"""复合问法拆分器(多任务并行的第一环)。

一句话问多件事("查查我年假还剩几天, 明天北京天气怎么样")在单意图链路上必然
丢一半: 整句只产出一个 ``IntentResult``, 而 ``(TOOL_CALL, hr)`` 与
``(TOOL_CALL, web)`` 两组种子互相把对方的 margin 压下去, 判定沉到 LLM 层后
``INTENT_PROMPT`` 又只允许回答一个 intent/target。本模块只负责把这种句子拆成
**若干条可独立执行的子问题**。

刻意只拆问题、不出意图: 意图口径的单一事实源在 ``app/assistant/intent.py`` 的
三层漏斗(``_RULES`` / ``_SEEDS`` / ``layer`` 审计), 让拆分器顺带直出 intent 就等于
养出第二套会漂移的分类器。子问题的 intent/target 仍逐条走
``IntentRecognizer.classify``(见 ``graph.plan_tasks``)。

降级风格与意图漏斗一致: 任何异常/脏输出都返回空列表, 空列表 == 不拆, 链路原样
走今天的单意图路径 —— 拆分器永远不会成为对话链路的故障点。
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

# 触发拆分的廉价前置标记: 句读分隔符或并列/顺承连接词。三者都不含的句子几乎
# 不可能问了两件事, 直接省掉一次 LLM 调用(与意图漏斗第一层"能省则省"同思路)。
# 只作准入门, 不作拆分依据 —— 真正的拆分交给 LLM, 避免规则切句切坏语义。
_SPLIT_HINT = re.compile(
    r"[，。；、,;]"
    r"|(还有|另外|顺便|同时|以及|并且|再帮|再来|然后|接着|也查|也看|都查|分别)"
)


def clean_tasks(raw: str, max_tasks: int) -> tuple[list[str], int]:
    """解析拆分器输出的 JSON 并清洗成子问题列表(纯函数, 可离线单测)。

    脏 JSON / 非数组 / 空串 / 超长条 / 重复条一律丢弃; **有效条数 < 2 时返回空
    列表**, 语义是"用户其实只问了一件事", 调用方据此走原有单意图路径。

    返回 ``(保留的子问题, 因超出 max_tasks 被截断的条数)`` —— 截断条数要透传到
    回答末尾说明"另有 N 项本次未处理", 不能默默吞掉用户的一句话。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [], 0
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        return [], 0
    seen: set[str] = set()
    out: list[str] = []
    for item in tasks:
        if not isinstance(item, str):
            continue
        text = item.strip().strip(_QUOTE_CHARS).strip()
        if not text or len(text) > _MAX_TASK_LEN or text in seen:
            continue
        seen.add(text)
        out.append(text)
    if len(out) < 2:
        return [], 0
    limit = max(2, max_tasks)
    return out[:limit], max(0, len(out) - limit)


class TaskPlanner:
    """复合问法 -> 2~N 条独立子问题(一次 LLM 调用, 走 Prompt Cache)。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._llm = get_chat_model(
            settings.intent_model, temperature=_LLM_TEMPERATURE, json_mode=True
        )

    def looks_multi(self, message: str) -> bool:
        """零网络调用的准入判定: 总开关关闭/消息太短/无并列标记一律不拆。"""
        if not self._settings.multi_task_enabled:
            return False
        text = (message or "").strip()
        if len(text) < self._settings.multi_task_min_chars:
            return False
        return bool(_SPLIT_HINT.search(text))

    async def split(self, message: str, history: str = "") -> tuple[list[str], int]:
        """拆出子问题; 返回 ``(子问题列表, 被截断条数)``, 少于两条或任何失败都是 ([], 0)。"""
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
            return clean_tasks(raw, max_tasks)
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
