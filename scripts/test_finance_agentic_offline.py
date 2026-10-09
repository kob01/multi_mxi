"""Finance_Agent 第三代 Agentic 升级的离线单测: 不需要整栈, 不需要 LLM/DB/网络/A2A 下游。

跑法::

    uv run python -m scripts.test_finance_agentic_offline

覆盖可离线判定的部分(闭环的真实推理、跨域端到端办理在整栈里回归):
  1. JSON 抠取(``_parse_json``): 裸对象 / ``` 包裹 / 前后杂字 / 非法;
  2. 子任务规整(``_valid_step``): 非法 kind 归 read、非法 peer_domain 剥掉、空 intent 丢弃;
  3. 写操作确认门(``_has_confirmation`` + ``_gate_write_tool``): 原句无确认词拒写、
     命中确认词放行到真实写工具(硬控制, 不依赖提示词);
  4. peer 可委派域(``_allowed_peer_domains``): 与 AGENT_WHITELIST 同源, 员工不含 analytics,
     任何角色都不含 finance 自身(杜绝自委派环);
  5. 路由判定(``_route_after_executor`` / ``_route_after_reflector``): 未跑完进步骤、
     跑完转反思、replan 回执行、其余收口;
  6. Agent Card: 版本升到 3.0.0 且新增 agentic_planning / peer_collaboration 两技能;
  7. 提示词模板: PLANNER/STEP/REFLECTOR/LEGACY 按各自占位符 .format 不抛且无残留花括号;
  8. 配置项: finance_* 齐备且步数/轮数/超时为正。
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import StructuredTool

from app.agents.finance_agent import prompts
from app.agents.finance_agent.agent_card import build_agent_card
from app.agents.finance_agent.executor import (
    FinanceAgent,
    _allowed_peer_domains,
    _gate_write_tool,
    _has_confirmation,
    _parse_json,
    _valid_step,
)
from app.config import get_settings
from app.schemas import Role
from app.security.caller import Caller, reset_caller, set_caller

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def test_parse_json() -> None:
    check("裸 JSON", _parse_json('{"a": 1}') == {"a": 1})
    check("``` 包裹", _parse_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("前后杂字", _parse_json('结论如下: {"verdict": "finish"} 供参考') == {"verdict": "finish"})
    check("非法返回 None", _parse_json("不是 JSON") is None)
    check("空串返回 None", _parse_json("") is None)


def test_valid_step() -> None:
    s = _valid_step({"intent": "查预算", "kind": "ANALYZE", "peer_domain": "HR"})
    check("kind 归小写合法", s is not None and s["kind"] == "analyze", str(s))
    s2 = _valid_step({"intent": "x", "kind": "bogus", "peer_domain": "finance"})
    check("非法 kind 归 read", s2 is not None and s2["kind"] == "read", str(s2))
    check("finance 自委派被剥成空 peer", s2 is not None and s2["peer_domain"] == "", str(s2))
    check("空 intent 丢弃", _valid_step({"intent": "  ", "kind": "read"}) is None)
    check("非 dict 丢弃", _valid_step("nope") is None)


def test_confirmation_gate() -> None:
    check("无确认词", not _has_confirmation("帮我报销一张800元差旅费"))
    check("含确认词", _has_confirmation("确认提交这张报销单"))
    check("英文确认词", _has_confirmation("yes, please submit"))
    check("动作词'提交'不算确认(首轮不架空草稿门)", not _has_confirmation("帮我提交一张报销单"))
    check("泛化词'没问题/可以'不算确认", not _has_confirmation("这样报销没问题吧"))

    async def _fake_create(
        title: str = "", amount: float = 0.0, category: str = "", reason: str = "",
        user_id: str = "", caller_user_id: str = "", caller_role: str = "",
    ):
        return {"order_no": "FIN9999", "echo": title}

    raw = StructuredTool.from_function(
        coroutine=_fake_create, name="create_reimbursement", description="d"
    )
    gated = _gate_write_tool(raw)

    async def _run() -> tuple[object, object]:
        denied_token = set_caller(
            Caller(user_id="E1", role="employee", intent_text="帮我报销一张800元差旅费")
        )
        try:
            denied = await gated.ainvoke({"title": "出差高铁", "amount": 800.0, "category": "差旅费"})
        finally:
            reset_caller(denied_token)
        ok_token = set_caller(Caller(user_id="E1", role="employee", intent_text="确认提交"))
        try:
            allowed = await gated.ainvoke({"title": "出差高铁", "amount": 800.0, "category": "差旅费"})
        finally:
            reset_caller(ok_token)
        return denied, allowed

    denied, allowed = asyncio.run(_run())
    check(
        "无确认词被硬门拒写",
        isinstance(denied, dict) and denied.get("needs_confirmation") is True,
        str(denied),
    )
    check(
        "命中确认词放行到真实写工具",
        isinstance(allowed, dict) and allowed.get("order_no") == "FIN9999",
        str(allowed),
    )


def test_allowed_peer_domains() -> None:
    emp = _allowed_peer_domains(Role.EMPLOYEE)
    mgr = _allowed_peer_domains(Role.MANAGER)
    check("员工可委派 hr/procurement", set(emp) == {"hr", "procurement"}, str(emp))
    check("员工不可委派 analytics", "analytics" not in emp, str(emp))
    check("经理可委派含 analytics", "analytics" in mgr, str(mgr))
    check("任何角色都不含 finance 自身", "finance" not in emp and "finance" not in mgr)


def test_routes() -> None:
    # 路由判定不触碰 self, 传 None 作实例即可离线复算。
    st_todo = {"step_idx": 1, "plan": [{}, {}]}
    st_done = {"step_idx": 2, "plan": [{}, {}]}
    check("未跑完 -> executor", FinanceAgent._route_after_executor(None, st_todo) == "executor")
    check("跑完 -> reflector", FinanceAgent._route_after_executor(None, st_done) == "reflector")
    check("replan -> executor", FinanceAgent._route_after_reflector(None, {"verdict": "replan"}) == "executor")
    check("finish -> END", FinanceAgent._route_after_reflector(None, {"verdict": "finish"}) != "executor")
    check("ask_confirm -> 收口", FinanceAgent._route_after_reflector(None, {"verdict": "ask_confirm"}) != "executor")


def test_agent_card() -> None:
    card = build_agent_card()
    check("版本升到 3.0.0", card.version == "3.0.0", card.version)
    skill_ids = {s.id for s in card.skills}
    check("含 agentic_planning 技能", "agentic_planning" in skill_ids, str(skill_ids))
    check("含 peer_collaboration 技能", "peer_collaboration" in skill_ids, str(skill_ids))
    check("原有报销/预算技能仍在", {"create_reimbursement", "query_budget", "finance_text2sql"} <= skill_ids, str(skill_ids))


def test_prompts_format() -> None:
    caps = prompts.ROLE_CAPABILITIES[Role.FINANCE]
    label = prompts.ROLE_LABELS[Role.FINANCE]
    try:
        p = prompts.PLANNER_PROMPT.format(role_label=label, capabilities=caps, peer_domains="hr, analytics", max_steps=6)
        s = prompts.STEP_EXECUTOR_PROMPT.format(role_label=label, capabilities=caps, schema="DDL", peer_domains="hr")
        r = prompts.REFLECTOR_PROMPT.format(goal="g", observations="[1] o", max_replan_steps=6)
        lg = prompts.LEGACY_SYSTEM_PROMPT.format(role_label=label, capabilities=caps, schema="DDL")
    except KeyError as exc:  # 占位符漂移
        check("提示词全部可格式化", False, f"KeyError: {exc}")
        return
    placeholders = ("{role_label}", "{capabilities}", "{schema}", "{peer_domains}", "{max_steps}", "{goal}", "{observations}", "{max_replan_steps}")
    leftover = [ph for ph in placeholders for t in (p, s, r, lg) if ph in t]
    check(
        "提示词占位符均已消费且无残留未替换",
        not leftover,
        f"残留: {leftover}",
    )
    check("planner 保留了 JSON 花括号契约", '{"steps":' in p.replace(" ", ""), p[:0])


def test_config_fields() -> None:
    st = get_settings()
    check("finance_agentic_enabled 为布尔", isinstance(st.finance_agentic_enabled, bool))
    check("finance_max_plan_steps 为正", st.finance_max_plan_steps > 0)
    check("finance_max_replan_rounds 为正", st.finance_max_replan_rounds > 0)
    check("finance_peer_delegate_enabled 为布尔", isinstance(st.finance_peer_delegate_enabled, bool))
    check("finance_step_timeout 为正", st.finance_step_timeout > 0)


def main() -> int:
    test_parse_json()
    test_valid_step()
    test_confirmation_gate()
    test_allowed_peer_domains()
    test_routes()
    test_agent_card()
    test_prompts_format()
    test_config_fields()

    failed = [name for ok, name, _ in _results if not ok]
    print("\n" + ("全部通过" if not failed else f"失败 {len(failed)} 项: " + ", ".join(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
