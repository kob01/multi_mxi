"""多任务并行的离线单测: 不需要整栈, 不需要 LLM/DB/网络。

跑法::

    uv run python -m scripts.test_multi_task_offline

覆盖计划"测试与验收"里可离线判定的部分:
  1. 拆分器输出清洗(clean_tasks): 脏 JSON / 非数组 / 单条 / 重复 / 超长 / 截断计数;
  2. 拆分准入(looks_multi): 总开关、长度下限、句读与连接词标记;
  3. 并行集口径(_is_parallel_safe): 只读通道进并行, 业务域 tool_call 与委派一律串行;
  4. 分节合并(_merge_task_answers): 一节一件事 + 部分失败 + 全失败 + 截断提示;
  5. 响应组装(_build_response): route=multi_task 合法且 metadata.subtasks 成形;
  6. 图装配: plan_tasks / multi_execute / merge_results 三节点与边能编译通过。

真实拆分效果、并行是否真的省时、端到端回归在 scripts/test_multi_task.py 走整栈验证。
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from app.assistant.graph import AssistantOrchestrator
from app.assistant.planner import TaskPlanner, clean_tasks
from app.config import get_settings
from app.schemas import ChatRequest, IntentResult, IntentType, Role

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def _payload(*tasks: str) -> str:
    return json.dumps({"tasks": list(tasks)}, ensure_ascii=False)


def test_clean_tasks() -> None:
    kept, dropped = clean_tasks(_payload("查我的年假还剩几天", "明天北京的天气怎么样"), 3)
    check("两条独立诉求拆成 2 条", kept == ["查我的年假还剩几天", "明天北京的天气怎么样"] and dropped == 0, str(kept))
    check("脏 JSON 回退不拆", clean_tasks("这不是 JSON", 3) == ([], 0))
    check("非数组载荷回退不拆", clean_tasks('{"tasks": "查年假"}', 3) == ([], 0))
    check("只有一件事按不拆处理", clean_tasks(_payload("差旅费报销标准是多少"), 3) == ([], 0))
    kept, _ = clean_tasks(_payload("查年假", "查年假", "查天气"), 3)
    check("重复条去重后仍是两条", kept == ["查年假", "查天气"], str(kept))
    kept, _ = clean_tasks(_payload("查年假", "  ", "「查天气」", "x" * 300), 3)
    check("空条与超长条被丢弃且剥掉包裹引号", kept == ["查年假", "查天气"], str(kept))
    kept, dropped = clean_tasks(_payload("一", "二", "三", "四", "五"), 3)
    check("超出上限截断并回报未处理条数", kept == ["一", "二", "三"] and dropped == 2, f"{kept} dropped={dropped}")


def test_looks_multi() -> None:
    def gate(enabled: bool, min_chars: int) -> TaskPlanner:
        # looks_multi 只读这两个配置项, 用 SimpleNamespace 绕开 LLM 客户端构造。
        planner = TaskPlanner.__new__(TaskPlanner)
        planner._settings = SimpleNamespace(  # type: ignore[attr-defined]
            multi_task_enabled=enabled, multi_task_min_chars=min_chars
        )
        return planner

    on = gate(True, 8)
    check("关闭总开关即不拆", gate(False, 8).looks_multi("查查我年假还剩几天，明天北京天气怎么样") is False)
    check("短消息不拆(省一次 LLM 调用)", on.looks_multi("查年假") is False)
    check("逗号分隔的复合问法命中", on.looks_multi("查查我年假还剩几天，明天北京天气怎么样") is True)
    check("连接词'顺便'命中", on.looks_multi("帮我查下年假余额顺便看看明天北京天气") is True)
    check("无分隔无连接词的单一诉求不拆", on.looks_multi("我的公积金缴纳比例是多少") is False)


def _intent(kind: IntentType, target: str | None = None) -> IntentResult:
    return IntentResult(intent=kind, target=target, confidence=0.8, reason="test", layer="rule")


def test_parallel_partition() -> None:
    safe = AssistantOrchestrator._is_parallel_safe
    check("知识库检索可并行", safe(_intent(IntentType.KNOWLEDGE_QA)) is True)
    check("web 能力域可并行", safe(_intent(IntentType.TOOL_CALL, "web")) is True)
    check("docgen 能力域可并行", safe(_intent(IntentType.TOOL_CALL, "docgen")) is True)
    check("业务域 tool_call 不并行(可能写库)", safe(_intent(IntentType.TOOL_CALL, "hr")) is False)
    check("finance tool_call 不并行", safe(_intent(IntentType.TOOL_CALL, "finance")) is False)
    check("A2A 委派不并行", safe(_intent(IntentType.AGENT_DELEGATE, "hr")) is False)
    check("闲聊不并行", safe(_intent(IntentType.CHITCHAT)) is False)
    route = AssistantOrchestrator._subtask_route
    check(
        "意图到路由的映射稳定",
        route(_intent(IntentType.TOOL_CALL, "hr")) == "mcp_tool"
        and route(_intent(IntentType.KNOWLEDGE_QA)) == "assistant_kb"
        and route(_intent(IntentType.AGENT_DELEGATE, "finance")) == "a2a_agent",
    )
    check(
        "分节标签带业务域名",
        AssistantOrchestrator._subtask_label(_intent(IntentType.TOOL_CALL, "hr")) == "MCP工具·hr"
        and AssistantOrchestrator._subtask_label(_intent(IntentType.KNOWLEDGE_QA)) == "知识库",
    )


def _outcome(index: int, query: str, answer: str, *, ok: bool = True, error: str = "", kind: IntentType = IntentType.TOOL_CALL, target: str | None = "hr") -> dict:
    return {
        "index": index, "query": query, "intent": _intent(kind, target), "route": "mcp_tool",
        "target": target, "answer": answer, "docs_meta": [], "artifacts": [],
        "ok": ok, "error": error, "elapsed_ms": 10,
    }


def test_merge_answers() -> None:
    merge = AssistantOrchestrator._merge_task_answers
    merged = merge([
        _outcome(0, "查我的年假还剩几天", "剩余 5 天。"),
        _outcome(1, "明天北京的天气怎么样", "晴, 18~27℃。", target="web"),
    ])
    check("每件事各自成节", "## 1. 查我的年假还剩几天" in merged and "## 2. 明天北京的天气怎么样" in merged, merged)
    check("两节都保留自己的答复", "剩余 5 天。" in merged and "18~27℃" in merged, merged)
    check("节标题带路由标签", "(MCP工具·hr)" in merged, merged)
    check("分节不丢任一件事", merged.count("## ") == 2, merged)

    partial = merge([_outcome(0, "查年假", "剩余 5 天。"), _outcome(1, "查天气", "", ok=False, error="工具调用超时", target="web")])
    check("部分失败只降级该节", "剩余 5 天。" in partial and "该项未完成: 工具调用超时" in partial, partial)

    all_failed = merge([_outcome(0, "查年假", "", ok=False, error="下游无响应"), _outcome(1, "查天气", "", ok=False, error="下游无响应")])
    check("全失败给统一说明且带原因", "都没能完成" in all_failed and "下游无响应" in all_failed, all_failed)
    check("空结果不抛异常", isinstance(merge([]), str) and bool(merge([])))
    check("截断项在末尾说明", "另有 2 项" in merge([_outcome(0, "甲", "a"), _outcome(1, "乙", "b")], dropped=2))


def test_build_response() -> None:
    # _build_response 不依赖实例状态, 用 __new__ 取裸对象避免构造 LLM/记忆等客户端。
    orch = AssistantOrchestrator.__new__(AssistantOrchestrator)
    req = ChatRequest(session_id="s1", user_id="E10005", role=Role.EMPLOYEE, department="研发部", message="复合问法")
    final = {
        "answer": "## 1. 甲\n\nA\n\n## 2. 乙\n\nB",
        "intent": _intent(IntentType.TOOL_CALL, "hr"),
        "route": "multi_task",
        "target": "hr",
        "message_id": 7,
        "artifacts": [],
        "docs_meta": [],
        "task_results": [
            _outcome(0, "查我的年假还剩几天", "剩余 5 天。"),
            {**_outcome(1, "明天北京的天气怎么样", "", ok=False, error="未完成", target="web"), "route": "mcp_tool"},
        ],
        "subtask_dropped": 1,
    }
    resp = AssistantOrchestrator._build_response(orch, req, final, "trace-1")  # type: ignore[arg-type]
    check("route=multi_task 通过响应模型校验", resp.route == "multi_task", resp.route)
    subs = resp.metadata.get("subtasks") or []
    check(
        "metadata.subtasks 逐项带 index/query/route/ok",
        len(subs) == 2 and subs[0]["ok"] is True and subs[1]["ok"] is False
        and subs[1]["route"] == "mcp_tool" and subs[0]["query"] == "查我的年假还剩几天",
        str(subs),
    )
    check("未处理条数透出到 metadata", resp.metadata.get("subtask_dropped") == 1, str(resp.metadata))


def test_graph_wiring() -> None:
    orch = AssistantOrchestrator.__new__(AssistantOrchestrator)
    graph = AssistantOrchestrator._build_graph(orch, None)  # type: ignore[arg-type]
    nodes = set(graph.get_graph().nodes)
    for node in ("plan_tasks", "classify_intent", "multi_execute", "merge_results", "persist_memory"):
        check(f"图已装配节点 {node}", node in nodes, str(sorted(nodes)))
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    check(
        "rewrite -> plan_tasks -> classify 已串联",
        ("rewrite_query", "plan_tasks") in edges and ("plan_tasks", "classify_intent") in edges,
        str(sorted(edges))[:200],
    )
    check("merge_results 汇入 persist_memory", ("merge_results", "persist_memory") in edges)

    # 路由判定: 多子任务优先于单意图分派。
    check(
        "多子任务路由到 multi_execute",
        AssistantOrchestrator._route_by_intent(  # type: ignore[arg-type]
            {"subtasks": [{"intent": _intent(IntentType.TOOL_CALL, "hr")}, {"intent": _intent(IntentType.TOOL_CALL, "web")}],
             "intent": _intent(IntentType.TOOL_CALL, "hr")}
        )
        == "multi_execute",
    )
    check(
        "单任务仍走原有四路由",
        AssistantOrchestrator._route_by_intent(  # type: ignore[arg-type]
            {"subtasks": [], "intent": _intent(IntentType.AGENT_DELEGATE, "hr")}
        )
        == "agent_delegate",
    )


def test_settings_defaults() -> None:
    settings = get_settings()
    check(
        "多任务配置项已进 Settings",
        all(
            hasattr(settings, name)
            for name in (
                "multi_task_enabled", "multi_task_min_chars", "multi_task_max_subtasks",
                "multi_task_parallelism", "multi_task_subtask_timeout",
            )
        ),
    )
    check(
        "并发上限与超时为正数(0 会让子任务全卡在信号量上)",
        settings.multi_task_parallelism > 0 and settings.multi_task_subtask_timeout > 0,
        f"parallelism={settings.multi_task_parallelism} timeout={settings.multi_task_subtask_timeout}",
    )
    check(
        "拆分上限 >= 2(否则多任务永不生效)",
        settings.multi_task_max_subtasks >= 2, str(settings.multi_task_max_subtasks),
    )


def main() -> int:
    test_clean_tasks()
    test_looks_multi()
    test_parallel_partition()
    test_merge_answers()
    test_build_response()
    test_graph_wiring()
    test_settings_defaults()
    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
