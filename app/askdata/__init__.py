"""智能问数(AskData_Agent)的留痕存储、归因路由与自服务 REST。

与问数执行面的分工: askdata MCP server 负责"算数"(语义层 + 编译 + 取数), 本包负责
"记住并解释结果" —— 全链路留痕、看板指标、反馈事件与 badcase 归因、评测集管理。
留痕 DAO 是同步实现(与 FastMCP 工具共用一份), 在异步网关里走 threadpool。
"""
