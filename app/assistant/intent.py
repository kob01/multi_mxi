"""Intent recognition powered by the configured intent model.

Falls back to keyword heuristics when the model output cannot be parsed,
so routing never crashes on bad LLM output.
"""

from __future__ import annotations

import json
import re

from app.assistant.prompts import INTENT_PROMPT
from app.config import get_settings
from app.llm import get_chat_model
from app.schemas import IntentResult, IntentType

_AGENT_KEYWORDS = {
    "finance": ("报销", "费用", "发票", "借款", "付款", "预算"),
    "hr": ("入职", "离职", "在职证明", "证明", "考勤", "请假", "年假申请", "工单"),
}
_TOOL_PATTERNS = re.compile(r"(FIN\d+|HR\d+|余额|进度查询|查询单号)")
# 出现以下相对时间词即视为"依赖当前时间"的问题, 需先取平台时间再作答。
# 仅用于 resolve_time 节点决定是否预取时钟, 不参与意图分类。
_TIME_CONTEXT_PATTERNS = re.compile(
    r"(今天|明天|昨天|后天|前天|现在|当前|目前|此刻|几号|几点|星期|周几|礼拜|"
    r"今年|去年|明年|本月|这个月|上个月|下个月|本季度|上季度|下季度|本年度|"
    r"月初|月底|年初|年末|截止|截至|还有几天|剩余几天|时效|过期|到期)"
)


def needs_current_time(message: str) -> bool:
    """Whether answering ``message`` requires the platform's current time."""
    return bool(_TIME_CONTEXT_PATTERNS.search(message))


class IntentRecognizer:
    """LLM-based intent classifier with deterministic fallback."""

    def __init__(self) -> None:
        settings = get_settings()
        self._llm = get_chat_model(
            settings.intent_model, temperature=0.2, json_mode=True
        )

    def _fallback(self, message: str) -> IntentResult:
        """Keyword-based routing when the model fails."""
        for target, kws in _AGENT_KEYWORDS.items():
            if any(k in message for k in kws):
                if _TOOL_PATTERNS.search(message):
                    return IntentResult(intent=IntentType.TOOL_CALL, target=target, confidence=0.55, reason="keyword:tool")
                return IntentResult(intent=IntentType.AGENT_DELEGATE, target=target, confidence=0.55, reason="keyword:agent")
        return IntentResult(intent=IntentType.KNOWLEDGE_QA, confidence=0.4, reason="keyword:default_kb")

    async def classify(self, message: str, history: str) -> IntentResult:
        """Classify the latest user utterance."""
        prompt = INTENT_PROMPT.format(history=history or "(无)", message=message)
        try:
            resp = await self._llm.ainvoke(prompt)
            data = json.loads(str(resp.content))
            intent = IntentType(data.get("intent", "knowledge_qa"))
            target = data.get("target")
            if target not in ("finance", "hr"):
                target = None
            return IntentResult(
                intent=intent,
                target=target,
                confidence=float(data.get("confidence", 0.5)),
                reason=str(data.get("reason", "")),
            )
        except Exception:
            return self._fallback(message)
