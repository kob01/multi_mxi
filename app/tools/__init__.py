"""进程内工具能力包(计划 D1): 非 MCP 的跨域基础能力统一在这里注册。

与 ``app/agents/common_tools.py`` 的定位一致("跨域基础能力"), 区别在于 common_tools
是**无条件**注入所有 ReAct 循环的公共件(姓名->工号), 本包按**能力域(target)**取用:
``tool_execute`` 判 ``intent.target in CAPABILITY_TOOLS`` 走进程内工具集, 跳过 MCP
连接池与 MCP 权限矩阵(能力域不是业务域 server, 不进 ``MCP_WHITELIST``)。

键名即意图层的 ``target`` 取值, 与意图/提示词/Prompt Cache 三处保持同一字符串:
- ``web``    -> 联网检索与抓取(search_web / fetch_url)
- ``docgen`` -> Word/Excel/PPT/PDF/Markdown 文件生成(generate_*, 支持内嵌图片, 返回可下载链接);
  并额外并入联网检索(search_web / fetch_url) —— "调研 X 并生成报告导出 PDF"这类
  复合任务被判为 docgen 后, 若本域只有 generate_* 就无网可搜, ReAct 只能凭模型记忆
  编内容再落盘(答非所问且看似秒回)。补入只读检索工具后, 同一轮可先检索再据实生成。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.tools.docgen import (
    generate_docx,
    generate_image,
    generate_md,
    generate_pdf,
    generate_pptx,
    generate_xlsx,
)
from app.tools.web import fetch_url, search_web

# 能力域 -> 工具集(单一事实来源; graph.tool_execute 与测试都以这里为准)。
# docgen 域并入 web 只读检索工具: 支撑"先调研后成文"的复合任务(见模块 docstring)。
# 检索工具名命中只读前缀白名单, 在 docgen 域同样按 server="docgen" 缓存, 不与 web 域串 key。
CAPABILITY_TOOLS: dict[str, list[BaseTool]] = {
    "web": [search_web, fetch_url],
    "docgen": [
        generate_docx, generate_xlsx, generate_pptx, generate_pdf,
        generate_md, generate_image, search_web, fetch_url,
    ],
}

__all__ = [
    "CAPABILITY_TOOLS", "fetch_url", "search_web",
    "generate_docx", "generate_xlsx", "generate_pptx", "generate_pdf",
    "generate_md", "generate_image",
]
