"""AskData_Agent A2A Agent Card 定义。

卡片 name 用 "AskData_Agent"(对外可读), Assistant 侧委派用的 domain 键是
"askdata"(与 askdata MCP server 同名), 二者经 a2a_client.AGENT_URLS 关联,
本卡片通告的地址来自 settings.askdata_agent_url。
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from app.config import get_settings


def build_agent_card() -> AgentCard:
    """构造 AskData_Agent 卡片(能力边界要说清「按口径问数」与「自由 SQL 分析」的差别)。"""
    settings = get_settings()
    return AgentCard(
        name="AskData_Agent",
        description=(
            "智能问数专业智能体: 自然语言问数走 NL2DSL + 语义层 —— 只按已登记的指标口径与"
            "维度取数(同环比/占比由引擎算, 不由模型算), 支持多维下钻与图表交付; "
            "说法含糊或口径冲突时给选项式澄清而不是猜。只读, 能读到哪些行由服务端行级安全决定。"
        ),
        url=f"{settings.askdata_agent_url}/",
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        skills=[
            AgentSkill(
                id="askdata_metric_query",
                name="按口径问数(NL2DSL + 语义层)",
                description=(
                    "把「报销花了多少/高风险合同有几份」这类说法映射到语义层登记的指标, "
                    "由服务端编译受控 SQL 取数; 每个数字都带口径说明, 未登记的口径直接拒"
                ),
                tags=["askdata", "nl2dsl", "semantic-layer", "query"],
                examples=["本月报销总额是多少", "今年已批准的采购金额有多少"],
            ),
            AgentSkill(
                id="askdata_drilldown",
                name="多维下钻与过滤",
                description=(
                    "按部门/类别/状态/供应商等已登记维度下钻, 支持枚举值过滤"
                    "(如「只看研发部」、「已打款的」); 维度与指标不兼容会被校验器拦下"
                ),
                tags=["askdata", "drilldown", "dimension"],
                examples=["各部门本月报销总额", "按费用类别看今年上半年的差旅花费"],
            ),
            AgentSkill(
                id="askdata_compare",
                name="环比/同比对比",
                description=(
                    "声明式环比(mom)与同比(yoy): 对比区间与变化率都由引擎算并随结果返回, "
                    "模型不做算术; 全部历史区间不支持对比会要求先给时间范围"
                ),
                tags=["askdata", "mom", "yoy", "compare"],
                examples=["各部门本月报销总额并做环比", "合同金额今年同比去年怎么样"],
            ),
            AgentSkill(
                id="askdata_chart",
                name="结果图表与结论",
                description=(
                    "按结果形状规则选图型(时序→折线、占比→饼、对比→柱), 图落盘回 URL; "
                    "模型只写结论叙述, 不参与选图也不重新计算"
                ),
                tags=["askdata", "chart", "narrative"],
                examples=["把各部门报销金额画成对比柱状图"],
            ),
            AgentSkill(
                id="askdata_clarify",
                name="选项式澄清",
                description=(
                    "指标口径多义、置信不足或缺时间范围时, 返回可点选的候选口径与时间段"
                    "让用户确认, 不做开放式反问也不自行猜测; 澄清与反馈会进入 badcase 归因"
                ),
                tags=["askdata", "clarify", "feedback"],
                examples=["费用有多少", "看一下预算情况"],
            ),
        ],
    )
