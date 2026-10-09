"""Contract_Agent A2A Agent Card definition.

卡片 name 用 "Contract_Agent"(对外可读), 委派 domain 键为 "procurement"(与
procurement MCP server 同名), 通过 a2a_client.AGENT_URLS 关联到 contract_agent_url。
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from app.config import get_settings


def build_agent_card() -> AgentCard:
    """Build the Contract_Agent card describing its A2A endpoint & skills."""
    settings = get_settings()
    return AgentCard(
        name="Contract_Agent",
        description=(
            "采购与合同初审专业智能体: 负责采购申请单的发起与合规初审(比价/供应商准入/预算),"
            "以及采购/服务合同的条款初审与风险清单; 通过 MCP 协议调用采购系统完成实际操作。"
        ),
        url=f"{settings.contract_agent_url}/",
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        skills=[
            AgentSkill(
                id="create_purchase_order",
                name="采购申请发起",
                description="收集采购事项/金额/类别/供应商/比价份数后创建采购申请单并进入初审",
                tags=["procurement", "purchase"],
                examples=["我要采购十台笔记本电脑", "申请一笔咨询服务采购"],
            ),
            AgentSkill(
                id="purchase_precheck",
                name="采购合规初审",
                description="对采购单执行比价份数、供应商准入、部门预算余额三条硬规则并给出结论",
                tags=["procurement", "compliance", "budget"],
                examples=["这张采购单合规吗", "PO3000 预算够不够"],
            ),
            AgentSkill(
                id="contract_review",
                name="合同条款初审",
                description=(
                    "对合同做 DAG 工作流初审: 先跑确定性规则红线(必备条款缺失/高风险表述/"
                    "供应商与账号一致/金额与违约金比例), 再按条款分块审查语义风险并强制"
                    "原文溯源(带条款编号与引用), 敏感值出口脱敏还原"
                ),
                tags=["procurement", "contract", "risk"],
                examples=["帮我审一下这份采购合同", "这份合同有什么风险"],
            ),
            AgentSkill(
                id="hitl_confirm",
                name="合同初审人工确认(HITL)",
                description=(
                    "对处于待确认(PENDING_CONFIRM)的合同初审做确认/修改/驳回闭环"
                    "(仅管理角色可用, 登记不等于放行)"
                ),
                tags=["procurement", "contract", "hitl", "approval"],
                examples=["确认这份合同的初审结论", "驳回这份高风险合同"],
            ),
            AgentSkill(
                id="supplier_check",
                name="供应商与预算查询",
                description="查询在册供应商资质/风险状态, 以及部门年度预算余额",
                tags=["procurement", "supplier", "budget"],
                examples=["这家供应商能合作吗", "研发部预算还剩多少"],
            ),
            AgentSkill(
                id="procurement_text2sql",
                name="采购数据统计查询 (Text2SQL)",
                description="自然语言生成只读 SQL, 统计采购单/合同/供应商数据(仅管理角色可用)",
                tags=["procurement", "text2sql", "analytics"],
                examples=["本季度采购金额是多少", "各部门采购笔数分布"],
            ),
        ],
    )
