"""数据洞察子系统: 零依赖 SVG 图表 + 周期报告装配 + 产物落盘。

三个文件各司其职, MCP server(app/mcp_servers/analytics_server.py)只做"工具外壳":
- charts.py  : 数据 -> SVG(bar/line/pie), 不引 matplotlib;
- reports.py : 固定口径指标 SQL -> 结构化指标 -> Markdown 正文;
- store.py   : 产物落 data/reports + report_artifacts 台账 + 相对 URL 寻址。
"""
