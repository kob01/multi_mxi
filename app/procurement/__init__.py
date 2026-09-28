"""采购与合同初审子系统: 确定性规则引擎(app/procurement/rules.py)。

分工是本子系统的设计核心(app/mcp_servers/procurement_server.py 只做工具外壳,
Contract_Agent 只做编排):
- rules.py : 必备条款/高风险表述/金额分级/预算余额 —— 进程内确定性判定, 零 token;
- 条款抽取与语义风险补充交由 Contract_Agent 的 ReAct 循环完成(get_contract_text 取
  原文 -> 模型阅读 -> save_contract_opinion 回写), 不在 MCP 内再开一次 LLM 调用。

"规则保底 + 模型加分"的顺序不能反: 模型可以升级风险等级, 不能降级规则已判定的红线。
"""
