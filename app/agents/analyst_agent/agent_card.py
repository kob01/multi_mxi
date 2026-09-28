"""Analyst_Agent A2A Agent Card definition.

卡片 name 用 "Analyst_Agent"(对外可读), 而 Assistant 侧委派用的 domain 键是
"analytics"(与 analytics MCP server 同名), 二者通过 a2a_client.AGENT_URLS 关联,
故本卡片通告的地址来自 settings.analyst_agent_url。
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from app.config import get_settings


def build_agent_card() -> AgentCard:
    """Build the Analyst_Agent card describing its A2A endpoint & skills."""
    settings = get_settings()
    return AgentCard(
        name="Analyst_Agent",
        description=(
            "数据洞察专业智能体: 跨 HR/财务/采购业务域做自然语言统计查询(Text2SQL)、"
            "生成图表与周期经营报告(周报/月报), 所有查询均为只读, 不改动业务数据。"
        ),
        url=f"{settings.analyst_agent_url}/",
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        skills=[
            AgentSkill(
                id="analytics_text2sql",
                name="跨域数据查询 (Text2SQL)",
                description="把自然语言统计问题转成只读 SQL, 跨报销/预算/工单/请假/采购/合同查询并汇总",
                tags=["analytics", "text2sql", "query"],
                examples=["各部门本月报销总额", "研发部今年费用趋势"],
            ),
            AgentSkill(
                id="render_chart",
                name="图表生成",
                description="把查询结果画成柱状/折线/饼图(SVG), 返回可展示的图表链接",
                tags=["analytics", "chart"],
                examples=["把各类费用占比画成饼图", "做个部门费用对比柱状图"],
            ),
            AgentSkill(
                id="weekly_report",
                name="周期经营报告",
                description="生成周报/月报: 固定口径指标 + 图表 + 模型结论, 输出可打开的 Markdown 报告",
                tags=["analytics", "report"],
                examples=["生成本周经营周报", "出一份本月费用分析报告"],
            ),
            AgentSkill(
                id="metrics_snapshot",
                name="经营指标快照",
                description="按周期取固定口径的经营指标概览(费用/预算/工单/采购/合同)",
                tags=["analytics", "metrics", "overview"],
                examples=["这个月整体经营情况怎么样", "看下本季度关键指标"],
            ),
        ],
    )
