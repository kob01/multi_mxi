"""跨域基础解析能力的接入层(进程内不直连数据库)。

层 0(收回跨域凭证)改造前: 本模块用 ``@tool`` 在**每个入口进程内**开 SQLAlchemy
Session 直接查 ``hr_employees`` —— 于是网关之外的每个专业智能体容器都必须持有一份
能读写全部业务表的数据库凭据(且那个账号是表属主)。"我的进程凭什么连着别人的库"
正是从这里敞开的。

现在实现搬到数据属域(HR MCP server 的 ``lookup_employee_by_name``), 本模块只剩接入
职责: 从 MCP 发现结果里取出该工具, 并安全地并入各角色的工具清单。

降级口径: 发现不到该工具(hr-mcp 未起或版本不含它)时返回 None, 调用方据此**少注入
一个工具**而不是让整轮对话失败 —— 用户给工号时业务照样能办。
"""

from __future__ import annotations

from typing import Any

LOOKUP_TOOL_NAME = "lookup_employee_by_name"


def take_lookup_tool(tools: list[Any]) -> Any | None:
    """从一批已发现的 MCP 工具里取"姓名->工号"解析工具(未提供时返回 None)。"""
    return next((t for t in tools if getattr(t, "name", "") == LOOKUP_TOOL_NAME), None)


def with_lookup_tool(tools: list[Any], lookup: Any | None) -> list[Any]:
    """把解析工具并入工具清单。

    清单里已有同名工具时原样返回: 重名工具会让 ``create_agent`` 直接报错, 而 hr 域
    的智能体本来就在 hr server 的工具清单里见过它一次。
    """
    if lookup is None or any(getattr(t, "name", "") == LOOKUP_TOOL_NAME for t in tools):
        return list(tools)
    return [*tools, lookup]
