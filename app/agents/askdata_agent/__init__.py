"""AskData_Agent(智能问数专业智能体): 语义层 + NL2DSL + 确定性状态机。

与其余四个专业智能体的最大区别在于**它内部不是 ReAct 自由循环**: 主干是一条
每步形状固定的状态机(见 :mod:`app.agents.askdata_agent.graph`), 只有结果校验
失败时才会拉起一个有界的只读自修复循环。

本包不 import 语义层的 DB 半区(``app.semantic.registry`` / ``link`` / ``compiler``):
容器按层 0 不持数据库凭据, 需要那些能力时都走 askdata MCP 工具。
"""
