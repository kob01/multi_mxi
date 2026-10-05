"""多智能体并发委派的离线单测: 不需要整栈, 不需要 LLM/DB/网络/A2A 下游。

跑法::

    uv run python -m scripts.test_multi_agent_offline

覆盖可离线判定的部分:
  1. 入参清洗(``ChatRequest.agent_targets`` 校验器): 去空白/转小写/去重保序, 缺省为空;
  2. 点选清洗(``normalize_agent_targets``): 非法域剥离 / 超上限截尾 / 上限配成 0 也至少
     执行一个 / 全非法时不执行任何委派;
  3. 分节合并(``_merge_agent_answers``): 一节一个智能体 + 部分失败 + 全失败 + 权限被拒 +
     非法与超限说明 + 超长截断;
  4. 响应组装(``_build_response``): route=multi_agent 合法且 metadata.agents 成形;
  5. 图装配: 新节点已串上, 复合问法拆分的三个节点(plan_tasks/multi_execute/
     merge_results)确实已从图上消失, 条件路由判定可离线复算;
  6. 域注册表一致性: ``AGENT_PROFILES`` 与 ``AGENT_URLS`` 键集合相同, 且每个域在
     ``AGENT_WHITELIST`` 里都存在 ``{domain}_agent`` 条目(否则 /api/agents 永远摆不出来);
  7. 总开关口径(``_entry_agent_targets``): 开关关闭即忽略点选, 不需要重启就能回退;
  8. 配置项: multi_agent_* 齐备且并发/超时/上限/截断长度为正, 拆分残留键已清零。

真实并发是否省时、逐个智能体的端到端回归在 scripts/test_multi_agent.py 走整栈验证。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from app.assistant.a2a_client import AGENT_PROFILES, AGENT_URLS
from app.assistant.graph import AssistantOrchestrator
from app.config import get_settings
from app.schemas import ChatRequest, IntentResult, IntentType, Role
from app.security.auth import AGENT_WHITELIST

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def _req(**kw) -> ChatRequest:
    base = {"session_id": "s1", "user_id": "E10005", "role": Role.EMPLOYEE,
            "department": "研发部", "message": "帮我看看我的年假和报销"}
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


def _res(
    index: int, domain: str, answer: str = "结果正文", *, ok: bool = True,
    status: str = "ok", error: str = "", agent: str = "",
) -> dict:
    return {
        "index": index, "domain": domain, "agent": agent or f"{domain}_agent",
        "route": "a2a_agent", "target": domain, "answer": answer,
        "ok": ok, "status": status, "error": error, "elapsed_ms": 1200,
    }


def test_request_normalization() -> None:
    req = _req(agent_targets=[" HR ", "hr", "Finance", "", "bogus"])
    check("点选列表去空白/转小写/去重且保序", req.agent_targets == ["hr", "finance", "bogus"], str(req.agent_targets))
    check("未点选时缺省为空列表", _req().agent_targets == [], str(_req().agent_targets))
    check("去重不区分大小写来源", _req(agent_targets=["HR", "hr"]).agent_targets == ["hr"])


def test_normalize_targets() -> None:
    norm = AssistantOrchestrator.normalize_agent_targets
    kept, invalid, dropped = norm(["hr", "finance", "bogus"], 3)
    check(
        "非法域被剥离但仍回给用户(在 invalid 里)",
        kept == ["hr", "finance"] and invalid == ["bogus"] and dropped == [],
        f"{kept} {invalid} {dropped}",
    )
    kept, invalid, dropped = norm(["hr", "finance", "analytics", "procurement"], 3)
    check(
        "超上限只截尾部, 前面的点选不动",
        kept == ["hr", "finance", "analytics"] and dropped == ["procurement"] and invalid == [],
        f"{kept} {dropped}",
    )
    kept, _, dropped = norm(["hr", "finance"], 0)
    check("上限配成 0 也至少执行一个(不至于点了没反应)", kept == ["hr"] and dropped == ["finance"], f"{kept} {dropped}")
    kept, invalid, _ = norm(["bogus1", "bogus2"], 3)
    check("全是非法域时不执行任何委派", kept == [] and invalid == ["bogus1", "bogus2"], f"{kept} {invalid}")


def test_agent_display() -> None:
    display = AssistantOrchestrator._agent_display
    check("已注册域取卡片可读名", display("hr") == "HR_Agent", display("hr"))
    check("未注册域退回裸域名不抛异常", display("bogus") == "bogus", display("bogus"))


def test_merge_answers() -> None:
    merge = AssistantOrchestrator._merge_agent_answers
    merged = merge(
        [_res(0, "hr", "年假剩余 5 天。", agent="HR_Agent"),
         _res(1, "finance", "报销单 FIN5000 已通过。", agent="Finance_Agent")],
        max_chars=4000,
    )
    check("每个智能体各自成节", "## 1. HR_Agent（hr）" in merged and "## 2. Finance_Agent（finance）" in merged, merged)
    check("两节都保留自己的答复", "年假剩余 5 天。" in merged and "FIN5000" in merged, merged)
    check("分节不丢任一项", merged.count("## ") == 2, merged)

    partial = merge([
        _res(0, "hr", "年假剩余 5 天。"),
        _res(1, "finance", "", ok=False, status="timeout", error="超过 150s 未返回"),
    ])
    check(
        "部分失败只降级那一节",
        "年假剩余 5 天。" in partial and "未完成: 超过 150s 未返回" in partial
        and "有 1 个智能体未能给出结果" in partial,
        partial,
    )

    denied = merge([
        _res(0, "hr", "年假剩余 5 天。"),
        _res(1, "analytics", "", ok=False, status="denied", error="权限不足: 角色 employee 无权访问 analytics_agent"),
    ])
    check(
        "被拒节写成未执行而非未完成",
        "未执行: 权限不足" in denied and "无权访问" in denied and "有 1 个智能体当前角色无权访问" in denied,
        denied,
    )

    all_failed = merge([
        _res(0, "hr", "", ok=False, status="error", error="ConnectionError: 下游无响应"),
        _res(1, "finance", "", ok=False, status="denied", error="权限不足"),
    ])
    check("全失败给统一说明且带原因", "都没能给出结果" in all_failed and "下游无响应" in all_failed, all_failed)
    check("空结果不抛异常且有文案", isinstance(merge([]), str) and bool(merge([])))

    notes = merge([
        _res(0, "hr", "ok"),
        _res(1, "bogus", "", ok=False, status="invalid", error="未注册的专业智能体域"),
        _res(2, "procurement", "", ok=False, status="dropped", error="超出单次可点选上限(3)"),
    ])
    check(
        "非法域与超限各自在末尾汇总一句",
        "有 1 项不是已注册的专业智能体域" in notes and "有 1 项超出单次可点选上限" in notes,
        notes,
    )

    truncated = merge([_res(0, "hr", "长" * 5000)], max_chars=100)
    check("超长答复按节截断并提示单独点选", "本节内容已截断" in truncated and "单独点选该智能体" in truncated, truncated[:200])

    single = merge([_res(0, "hr", "年假剩余 5 天。")])
    check("只点一个智能体时不加多个智能体引导语", "你点了" not in single and "## 1." in single, single)


def test_build_response() -> None:
    # _build_response 不依赖实例状态, 用 __new__ 取裸对象避免构造 LLM/记忆等客户端。
    orch = AssistantOrchestrator.__new__(AssistantOrchestrator)
    req = _req(agent_targets=["hr", "finance"])
    final = {
        "answer": "## 1. HR_Agent（hr）\n\nA\n\n## 2. Finance_Agent（finance）\n\nB",
        "intent": IntentResult(
            intent=IntentType.AGENT_DELEGATE, target="hr", confidence=1.0,
            reason="用户显式点选 2 个专业智能体并发委派", layer="explicit",
        ),
        "route": "multi_agent",
        "target": "hr",
        "message_id": 7,
        "artifacts": [],
        "docs_meta": [],
        "agent_results": [
            _res(0, "hr", "A", agent="HR_Agent"),
            _res(1, "finance", "", ok=False, status="timeout", error="超过 150s 未返回", agent="Finance_Agent"),
        ],
    }
    resp = AssistantOrchestrator._build_response(orch, req, final, "trace-1")  # type: ignore[arg-type]
    check("route=multi_agent 通过响应模型校验", resp.route == "multi_agent", str(resp.route))
    agents = resp.metadata.get("agents") or []
    check(
        "metadata.agents 逐项带 index/domain/agent/status/ok",
        len(agents) == 2 and agents[0]["ok"] is True and agents[1]["ok"] is False
        and agents[0]["domain"] == "hr" and agents[0]["agent"] == "HR_Agent"
        and agents[1]["status"] == "timeout",
        str(agents),
    )
    check("单意图轮次不携 metadata.agents", "agents" not in AssistantOrchestrator._build_response(  # type: ignore[arg-type]
        orch, _req(), {**final, "route": "a2a_agent", "agent_results": []}, "trace-2").metadata)


def test_graph_wiring() -> None:
    orch = AssistantOrchestrator.__new__(AssistantOrchestrator)
    graph = AssistantOrchestrator._build_graph(orch, None)  # type: ignore[arg-type]
    nodes = set(graph.get_graph().nodes)
    check("图已装配节点 multi_agent_execute", "multi_agent_execute" in nodes, str(sorted(nodes)))
    for gone in ("plan_tasks", "multi_execute", "merge_results"):
        check(f"复合问法拆分节点 {gone} 已从图上消失", gone not in nodes, str(sorted(nodes)))
    for node in ("classify_intent", "persist_memory", "agent_delegate"):
        check(f"原有节点 {node} 仍在", node in nodes, str(sorted(nodes)))
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    check("multi_agent_execute 汇入 persist_memory", ("multi_agent_execute", "persist_memory") in edges, str(sorted(edges))[:240])

    # 条件路由判定: 点选优先于意图分类, 未点选时行为与改动前一致。
    route = AssistantOrchestrator._route_after_rewrite
    check("点选了智能体走并发委派", route({"agent_targets": ["hr"]}) == "multi_agent_execute")
    check("未点选仍走意图分类", route({"agent_targets": []}) == "classify_intent")
    check(
        "单意图分派四路由不变",
        AssistantOrchestrator._route_by_intent(  # type: ignore[arg-type]
            {"intent": IntentResult(intent=IntentType.AGENT_DELEGATE, target="hr")}
        )
        == "agent_delegate",
    )


def test_domain_registry_coherence() -> None:
    check("AGENT_PROFILES 与 AGENT_URLS 键集合一致", set(AGENT_PROFILES) == set(AGENT_URLS),
          f"profiles={sorted(AGENT_PROFILES)} urls={sorted(AGENT_URLS)}")
    missing = [d for d in AGENT_PROFILES if f"{d}_agent" not in set().union(*AGENT_WHITELIST.values())]
    check("每个域在白名单里都有对应智能体名(否则 /api/agents 摆不出来)", not missing, str(missing))
    employee = {a for a in AGENT_WHITELIST[Role.EMPLOYEE]}
    check(
        "普通员工看不到 analytics(敏感能力默认拒)",
        "analytics_agent" not in employee and {"hr_agent", "finance_agent"} <= employee,
        str(sorted(employee)),
    )
    check(
        "多智能体上限不超过已注册域数(否则永远凑不满)",
        get_settings().multi_agent_max_targets <= len(AGENT_PROFILES),
        f"max={get_settings().multi_agent_max_targets} domains={len(AGENT_PROFILES)}",
    )


def test_settings_defaults() -> None:
    settings = get_settings()
    check(
        "多智能体配置项已进 Settings",
        all(
            hasattr(settings, name)
            for name in (
                "multi_agent_enabled", "multi_agent_max_targets", "multi_agent_parallelism",
                "multi_agent_timeout", "multi_agent_answer_chars",
            )
        ),
    )
    check(
        "并发上限与超时为正数(0 会把委派全卡在信号量上)",
        settings.multi_agent_parallelism > 0 and settings.multi_agent_timeout > 0
        and settings.multi_agent_answer_chars > 0,
        f"parallelism={settings.multi_agent_parallelism} timeout={settings.multi_agent_timeout}",
    )
    check(
        "外层超时应大于内层 a2a_timeout(让可读降级文案先返回)",
        settings.multi_agent_timeout > settings.a2a_timeout,
        f"multi_agent_timeout={settings.multi_agent_timeout} a2a_timeout={settings.a2a_timeout}",
    )
    check(
        "复合问法拆分的配置项已彻底移除",
        not any(
            hasattr(settings, name)
            for name in (
                "multi_task_enabled", "multi_task_min_chars", "multi_task_max_subtasks",
                "multi_task_parallelism", "multi_task_subtask_timeout",
                "multi_task_merge_enabled", "multi_task_core_overlap_threshold",
            )
        ),
    )


def test_kill_switch() -> None:
    """开关与入参在两处读(入图前洗空 + 路由只看 state), 这里定住它们不会不一致。"""
    orch = AssistantOrchestrator.__new__(AssistantOrchestrator)
    req = _req(agent_targets=["hr", "finance"])
    orch._settings = SimpleNamespace(multi_agent_enabled=True)  # type: ignore[attr-defined]
    kept = AssistantOrchestrator._entry_agent_targets(orch, req)  # type: ignore[arg-type]
    check("开关开启时点选原样进图", kept == ["hr", "finance"], str(kept))
    orch._settings = SimpleNamespace(multi_agent_enabled=False)  # type: ignore[attr-defined]
    off = AssistantOrchestrator._entry_agent_targets(orch, req)  # type: ignore[arg-type]
    check("开关关闭时点选被洗空", off == [], str(off))
    check(
        "洗空后条件路由回到单意图分类",
        AssistantOrchestrator._route_after_rewrite({"agent_targets": off}) == "classify_intent",  # type: ignore[arg-type]
    )


def main() -> int:
    test_request_normalization()
    test_normalize_targets()
    test_agent_display()
    test_merge_answers()
    test_build_response()
    test_graph_wiring()
    test_domain_registry_coherence()
    test_kill_switch()
    test_settings_defaults()
    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
