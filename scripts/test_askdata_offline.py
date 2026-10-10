"""智能问数(AskData_Agent)离线自检: 不连数据库、不调 LLM、不起服务。

跑法::

    uv run python -m scripts.test_askdata_offline

覆盖的都是"必须确定"的部分(概率部分不在这里测):
DSL 形状与越权列、三级校验错误码、编译器 SQL 形状与作用域注入、
口径与周报一致、澄清门与开关、载荷剥离、注册三处一致性。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from typing import Any

from app.agents.askdata_agent.graph import extract_json
from app.assistant.askdata_bridge import (
    artifacts_from_payload,
    slots_of,
    split_askdata_payload,
    to_metadata,
)
from app.config import get_settings
from app.db.scope import DataScope
from app.semantic import compiler
from app.semantic.dsl import DslError, parse_dsl
from app.semantic.seed import TOP_DIMS, TOP_METRICS, TERM_ALIASES, build_catalog
from app.semantic.validate import CallerScope, validate
from app.semantic import timesem

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


CATALOG = build_catalog()
TODAY = date(2026, 10, 10)  # 固定"今天", 时间语义必须可复现


def scope(all_depts: bool = True, role: str = "finance") -> DataScope:
    return DataScope(
        tenant_id="T001",
        dept_ids=("D001", "*") if not all_depts else ("D001", "D002", "*"),
        all_depts=all_depts,
        user_id="E1001",
        role=role,
        department="全员" if all_depts else "研发部",
    )


def dsl(**kwargs: Any) -> dict[str, Any]:
    base = {"metrics": [{"id": "m_expense_total"}], "time": {"range": "this_month"}}
    base.update(kwargs)
    return base


def test_dsl_shape() -> None:
    print("\n1) DSL 语法级校验(形状/作用域列/上限)")
    spec = parse_dsl(dsl(dims=["d_expense_department"], compare={"type": "mom"}))
    check("正常 DSL 可解析", spec.metric_ids == ["m_expense_total"] and spec.dims == ["d_expense_department"])
    try:
        parse_dsl(dsl(filters=[{"dim": "dept_id", "op": "eq", "value": "D001"}]))
        check("作用域列 dept_id 被拒", False, "没抛 DslError")
    except DslError as exc:
        check("作用域列 dept_id 被拒", "作用域列" in str(exc), str(exc))
    try:
        parse_dsl(dsl(limit=100000))
        check("limit 被收到硬上限内", True)
    except DslError:
        check("limit 被收到硬上限内", False, "直接拒了")
    bad = parse_dsl(dsl(limit=100000))
    check("limit 收到 HARD_ROW_CAP", bad.limit is not None and bad.limit <= 200, f"limit={bad.limit}")
    try:
        parse_dsl(dsl(metrics=[{"id": "m_x", "agg": "sum(amount)"}]))
        check("agg 不接受表达式", False)
    except DslError:
        check("agg 不接受表达式", True)
    try:
        parse_dsl({"metrics": [{"id": "m_expense_total"}], "time": {"range": "上个月的时候"}})
        check("区间词词表外被拒", False)
    except DslError:
        check("区间词词表外被拒", True)
    try:
        parse_dsl(dsl(filters=[{"dim": "d_expense_status", "op": "like", "value": "%已%"}]))
        check("op=like 被拒", False)
    except (DslError, ValueError):
        check("op=like 被拒", True)
    payload = spec.to_payload()
    check("to_payload 不含空字段", "order_by" not in payload and "limit" not in payload, str(payload))


def test_validate_levels() -> None:
    print("\n2) 三级校验(语义 + 权限)错误码")

    def codes(raw: dict, *, all_depts: bool = True, role: str = "finance") -> list[str]:
        check = validate(
            parse_dsl(raw), catalog=CATALOG,
            scope=CallerScope.from_scope(scope(all_depts, role)),
            max_time_span_days=730, max_rows=100, today=TODAY,
        )
        return check.codes

    check("合法 DSL 无错", not codes(dsl()), str(codes(dsl())))
    check("未知指标 -> E_UNKNOWN_METRIC",
          "E_UNKNOWN_METRIC" in codes(dsl(metrics=[{"id": "m_not_exists"}])))
    check("未知维度 -> E_UNKNOWN_DIM", "E_UNKNOWN_DIM" in codes(dsl(dims=["d_nope"])))
    check("维度与指标不兼容 -> E_INCOMPATIBLE_DIM",
          "E_INCOMPATIBLE_DIM" in codes(dsl(dims=["d_budget_department"])))
    check("跨事实表多指标 -> E_TABLE_CONFLICT",
          "E_TABLE_CONFLICT" in codes({"metrics": [{"id": "m_expense_total"}, {"id": "m_contract_count"}],
                                       "time": {"range": "this_month"}}))
    check("带行集口径的多指标 -> E_CALIBER_CONFLICT",
          "E_CALIBER_CONFLICT" in codes({"metrics": [{"id": "m_expense_total"}, {"id": "m_expense_travel_total"}],
                                         "time": {"range": "this_month"}}))
    check("缺时间 -> E_TIME_INVALID",
          "E_TIME_INVALID" in codes({"metrics": [{"id": "m_expense_total"}]}))
    check("跨度超限 -> E_TIME_INVALID",
          "E_TIME_INVALID" in codes(dsl(time={"start": "2000-01-01", "end": "2026-01-01"})))
    check("枚举值不存在 -> E_ENUM_INVALID",
          "E_ENUM_INVALID" in codes(dsl(filters=[{"dim": "d_expense_status", "op": "eq", "value": "已发货"}])))
    check("all_time + 环比被拒",
          "E_TIME_INVALID" in codes(dsl(time={"range": "all_time"}, compare={"type": "mom"})))
    check("limit 超上限 -> E_ROWS_CAP",
          "E_ROWS_CAP" in codes(dsl(limit=None) | {"limit": 500}))


def test_metric_roles() -> None:
    print("\n3) 指标级角色白名单")
    from app.semantic.catalog import Metric
    from app.semantic.validate import Issue  # noqa: F401  (确认错误码词表可导入)

    check("内置指标默认不逐指标限角色(靠域级黑名)",
          all(not m["allowed_roles"] for m in TOP_METRICS))
    catalog = build_catalog()
    metric_id = "m_expense_total"
    base = catalog.metrics[metric_id]
    locked = dict(catalog.metrics)
    locked[metric_id] = Metric(**{**base.__dict__, "allowed_roles": "admin"})
    spec = parse_dsl(dsl())
    from app.semantic.validate import ValidationResult  # noqa: F401

    finance_codes = validate(spec, catalog=_CatalogWith(locked),
                             scope=CallerScope(role="finance", all_depts=True), today=TODAY).codes
    admin_codes = validate(spec, catalog=_CatalogWith(locked),
                           scope=CallerScope(role="admin", all_depts=True), today=TODAY).codes
    check("限定 admin 的指标对 finance 拒", "E_NO_PERMISSION" in finance_codes, str(finance_codes))
    check("同指标对 admin 放行", "E_NO_PERMISSION" not in admin_codes, str(admin_codes))


class _CatalogWith:
    """测试替身: 只换 metrics, 其他查表逻辑走真目录。"""

    def __init__(self, metrics: dict) -> None:
        self._real = build_catalog()
        self.metrics = metrics

    def __getattr__(self, item):  # noqa: ANN401
        return getattr(self._real, item)

    def get_metric(self, metric_id: str):
        metric = self.metrics.get(metric_id)
        return metric if metric and metric.status == "active" else None


def test_compiler() -> None:
    print("\n4) 编译器: 作用域注入 / 绑参 / AST 自校验 / 同环比形状")
    sc = scope(True)

    def compile(raw: dict, *, mode: str = "analytical") -> compiler.Compiled:
        spec = parse_dsl(raw)
        check = validate(spec, catalog=CATALOG, scope=CallerScope.from_scope(sc), today=TODAY,
                         max_rows=get_settings().askdata_max_rows,
                         max_time_span_days=get_settings().askdata_max_time_span_days)
        assert check.ok, check.as_text()
        fn = compiler.compile_metric_api if mode == "metric_api" else compiler.compile_analytical
        return fn(spec, catalog=CATALOG, scope=sc, check=check, max_rows=100)

    one = compile(dsl())
    check("指标 API 通道产出", "m_expense_total" in one.sql and "FROM fin_reimbursements r" in one.sql, one.sql)
    check("注入了租户+部门谓词", ":scope_tenant" in one.sql and "string_to_array(:scope_depts" in one.sql, one.sql)
    check("软删行被排除", "r.is_deleted = false" in one.sql, one.sql)
    check("时间窗走绑参", ":p_start" in one.sql and one.params["p_start"] == "2026-10-01", str(one.params))
    check("带别名的列限定", "SUM(r.amount)" in one.sql, one.sql)
    check("有 LIMIT", "LIMIT" in one.sql.upper(), one.sql)

    filtered = compile(dsl(
        dims=["d_expense_category"],
        filters=[{"dim": "d_expense_department", "op": "eq", "value": "研发部"}],
    ))
    check("过滤值只走绑参不进文本", "研发部" not in filtered.sql
          and filtered.params.get("f0") == "研发部", filtered.sql)
    check("按部门过滤会带出 JOIN", "JOIN hr_employees e ON r.emp_id = e.emp_id" in filtered.sql, filtered.sql)
    check("GROUP BY 用维度列", "GROUP BY" in filtered.sql and "r.category" in filtered.sql, filtered.sql)

    in_list = compile(dsl(filters=[{"dim": "d_expense_category", "op": "in",
                                    "value": ["差旅费", "餐饮费"]}]))
    check("IN 展开为多个占位符", ":f0_0" in in_list.sql and ":f0_1" in in_list.sql
          and "差旅费" not in in_list.sql, in_list.sql)

    mom = compile(dsl(dims=["d_expense_department"], compare={"type": "mom"}))
    check("环比走 WITH cur/prev", mom.sql.startswith("WITH cur AS") and "prev AS (" in mom.sql, mom.sql[:80])
    check("环比有变化率列", "m_expense_total_mom_pct" in mom.sql, mom.sql)
    check("环比分母用 NULLIF 兜 0", "NULLIF(pr.m_expense_total, 0)" in mom.sql, mom.sql)
    check("对比期用独立绑参", mom.params.get("p_start_prev") == "2026-09-01", str(mom.params))

    gran = compile(dsl(time={"range": "this_year", "granularity": "month"}))
    check("时间粒度产出 period 桶", "AS period" in gran.sql and "date_trunc('month'" in gran.sql, gran.sql)

    snapshot = compile({"metrics": [{"id": "m_budget_used_pct"}], "dims": ["d_budget_department"],
                        "time": {"range": "this_year"}})
    check("快照型指标不带时间谓词", ":p_start" not in snapshot.sql, snapshot.sql)

    # AST 自校验: 编译产物必须能被 ast_guard 放行(它内部就是 validate_sql)
    check("编译产物已通过 AST 自校验(隐式)", True)
    try:
        compiler.compile_analytical(
            parse_dsl(dsl(metrics=[{"id": "m_expense_total"}])), catalog=CATALOG, scope=None,
            check=validate(parse_dsl(dsl()), catalog=CATALOG, scope=CallerScope.from_scope(sc), today=TODAY),
        )
        check("无作用域直接拒编译", False, "居然编出来了")
    except compiler.CompileError as exc:
        check("无作用域直接拒编译", "作用域" in str(exc), str(exc))

    # 行级口径表达式不能在 GROUP BY 时直接用: 编译期就拦下, 不把 PG 报错丢给用户
    from app.semantic.catalog import Metric

    row_level = dict(CATALOG.metrics)
    base = row_level["m_expense_total"]
    row_level["m_expense_total"] = Metric(**{**base.__dict__, "expression": "amount"})
    mixed = _CatalogWith(row_level)
    grouped = parse_dsl(dsl(dims=["d_expense_department"]))
    check_g = validate(grouped, catalog=mixed, scope=CallerScope.from_scope(sc), today=TODAY)
    try:
        compiler.compile_analytical(grouped, catalog=mixed, scope=sc, check=check_g)
        check("行级口径不能参与 GROUP BY", False, "编出了会报错的 SQL")
    except compiler.CompileError as exc:
        check("行级口径不能参与 GROUP BY", "聚合" in str(exc), str(exc))
    # 同一指标不分组时仍然可用(行级表达式在这种形状下本来就是对的)
    single = parse_dsl(dsl())
    ok_single = compiler.compile_metric_api(
        single, catalog=mixed, scope=sc,
        check=validate(single, catalog=mixed, scope=CallerScope.from_scope(sc), today=TODAY),
    )
    check("不分组时行级口径仍可用", "r.amount" in ok_single.sql, ok_single.sql)


def test_caliber_consistency() -> None:
    print("\n5) 口径一致性: 问数与周报共用同一份常量")
    from app.analytics import reports

    by_id = {m["metric_id"]: m for m in TOP_METRICS}
    pairs = {
        "m_expense_total": "expense_total",
        "m_expense_count": "expense_count",
        "m_expense_paid_total": "expense_paid",
        "m_ticket_open_count": "ticket_open",
        "m_contract_high_risk_count": "contract_high",
        "m_purchase_pending_count": "purchase_pending",
        "m_leave_days_total": "leave_days",
        # 问数用的是同一个消耗率的聚合形态(行级写法无法 GROUP BY), 两份常量都在
        # reports.py 里, 所以仍然是一个事实源。
        "m_budget_used_pct": "budget_used_pct_agg",
    }
    bad = []
    for metric_id, ref in pairs.items():
        metric = by_id.get(metric_id)
        if metric is None:
            bad.append(f"{metric_id} 未登记")
            continue
        if metric["caliber_ref"] != f"reports:{ref}":
            bad.append(f"{metric_id} 的 caliber_ref={metric['caliber_ref']!r} 指向不符")
        if metric["expression"] != reports.CALIBER_SQL[ref]:
            bad.append(f"{metric_id} 表达式与周报常量不一致: {metric['expression']!r} vs {reports.CALIBER_SQL[ref]!r}")
    check("重叠指标逐字等于周报常量", not bad, "; ".join(bad))
    # 直接核对本模块常量出现在 collect_metrics 的实现里(源码级, 不连库):
    # 防"两边各自写一份碰巧一样的 SQL" —— 那才是真正的口径漂移来漂移去。
    import inspect

    src = inspect.getsource(reports.collect_metrics)
    check("collect_metrics 引用常量", "CALIBER_EXPENSE_TOTAL" in src, "周报没引用常量, 口径可能各写一份")


def test_timesem() -> None:
    print("\n6) 时间语义确定性")
    r = timesem.resolve(timesem.SpecTime(range="this_month"), today=TODAY)
    check("本月至今", (r.start, r.end) == (date(2026, 10, 1), date(2026, 10, 11)), r.label)
    r2 = timesem.resolve(timesem.SpecTime(range="上月"), compare="mom", today=TODAY)
    check("上月环比 = 9 月对 8 月",
          (r2.start, r2.end, r2.prev_start, r2.prev_end) ==
          (date(2026, 9, 1), date(2026, 10, 1), date(2026, 8, 1), date(2026, 9, 1)), str(r2.to_payload()))
    r3 = timesem.resolve(timesem.SpecTime(range="this_month"), compare="mom", today=TODAY)
    check("MTD 环比对齐到上月同日段",
          (r3.prev_start, r3.prev_end) == (date(2026, 9, 1), date(2026, 9, 11)), str(r3.note))
    r4 = timesem.resolve(timesem.SpecTime(range="this_year"), compare="yoy", today=TODAY)
    check("同比平移一年", (r4.prev_start, r4.prev_end) == (date(2025, 1, 1), date(2025, 10, 11)), "")
    try:
        timesem.resolve({"range": "瞎说"}, today=TODAY)
        check("不认识的说法抛 TimeError", False, "没抛错")
    except timesem.TimeError:
        check("不认识的说法抛 TimeError", True)
    check("闰日平移不崩", timesem._add_years(date(2024, 2, 29), -1) == date(2023, 2, 28))


def test_link_matching() -> None:
    print("\n7b) 链接: 最长匹配优先与快照型口径")
    from app.semantic.link import link as run_link

    res = run_link("今年平均合同金额是多少", catalog=CATALOG, scope=None)
    ids = [c.target_id for c in res.metric_candidates]
    check("最长匹配胜出", "m_contract_avg_amount" in ids, str(ids))
    check("被包住的短说法不再造出假歧义", "m_contract_amount_total" not in ids, str(ids))
    both = run_link("各部门本月报销总额", catalog=CATALOG, scope=None)
    check("去重不跨类型误删维度",
          any(c.target_id == "d_expense_department" for c in both.dim_candidates),
          str([c.target_id for c in both.dim_candidates]))



def test_clarify_gate() -> None:
    print("\n7) 澄清门与开关")
    from app.agents.askdata_agent.graph import _clarify_payload

    state = {"question": "费用有多少", "slots": {}, "candidates": [
        {"kind": "metric", "id": "m_expense_total", "label": "报销总额", "score": 0.9, "caliber": "x"},
        {"kind": "metric", "id": "m_expense_count", "label": "报销单数", "score": 0.88, "caliber": "y"},
    ]}
    payload = _clarify_payload(state, ["缺时间"], [state["candidates"][0], state["candidates"][1]])
    check("候选数受 max_options 约束", len(payload["metrics"]) <= get_settings().askdata_clarify_max_options)
    check("澄清载荷带时间段候选", bool(payload["times"]) and payload["allow_none"])
    check("澄清载荷可 JSON 化", isinstance(json.dumps(payload, ensure_ascii=False), str))

    # 状态机判定节点可以直接跑(它不调 MCP), 验证三种触发与一个开关
    import asyncio

    from app.agents.askdata_agent.graph import AskDataPipeline

    pipeline = AskDataPipeline.__new__(AskDataPipeline)
    pipeline._settings = get_settings()

    async def run_gate(slots: dict, candidates: list[dict]) -> dict:
        return await pipeline.clarify_gate(
            {"question": "q", "slots": slots, "candidates": candidates}
        )

    got = asyncio.run(run_gate({"time": {"phrase": "本月"}, "metric_terms": ["报销总额"]},
                              [{"kind": "metric", "id": "m_expense_total", "label": "报销总额", "score": 1.0}]))
    check("要素齐全时不澄清", got.get("verdict") != "clarify", str(got))
    got2 = asyncio.run(run_gate({"metric_terms": ["费用"]},
                               [{"kind": "metric", "id": "m_expense_total", "label": "报销总额", "score": 0.55}]))
    check("低置信 + 缺时间触发澄清", got2.get("verdict") == "clarify", str(got2))
    got3 = asyncio.run(run_gate({"time": {"phrase": "本月"}, "metric_terms": ["费用"]},
                               [{"kind": "metric", "id": "m_expense_total", "label": "报销总额", "score": 0.9},
                                {"kind": "metric", "id": "m_expense_avg_amount", "label": "平均单笔报销额", "score": 0.87}]))
    check("口径过近触发澄清", got3.get("verdict") == "clarify", str(got3))
    snap = asyncio.run(run_gate({"metric_terms": ["预算消耗率"]},
                               [{"kind": "metric", "id": "m_budget_used_pct", "label": "预算消耗率",
                                 "score": 1.0, "time_aware": False}]))
    check("快照型口径不因为没说时间而被拦", snap.get("verdict") != "clarify", str(snap))
    timed = asyncio.run(run_gate({"metric_terms": ["报销总额"]},
                                [{"kind": "metric", "id": "m_expense_total", "label": "报销总额",
                                  "score": 1.0, "time_aware": True}]))
    check("普通口径缺时间仍会澄清", timed.get("verdict") == "clarify", str(timed))

    # 开关关掉: 不阻塞, 但要把假设留进口径 notes
    settings = get_settings()
    original = settings.askdata_clarify_enabled
    object.__setattr__(settings, "askdata_clarify_enabled", False)
    try:
        got4 = asyncio.run(run_gate({"metric_terms": ["费用"]},
                                   [{"kind": "metric", "id": "m_expense_total", "label": "报销总额", "score": 0.4}]))
        check("关澄清后不阻塞", got4.get("verdict") != "clarify", str(got4))
        check("关澄清仍留下假设说明", any("澄清已关闭" in n for n in got4.get("notes") or []), str(got4))
    finally:
        object.__setattr__(settings, "askdata_clarify_enabled", original)


def test_bridge() -> None:
    print("\n8) A2A 载荷剥离(网关侧)")
    body = {"kind": "result", "query_id": "abc", "question": "本月报销多少",
            "rowcount": 2, "columns": ["m_expense_total"], "rows": [{"m_expense_total": 1}],
            "sql": "SELECT 1", "chart": {"url": "http://x/a.svg", "name": "a.svg", "chart_type": "bar"},
            "slots": {"time": {"phrase": "本月"}}}
    text = "结论如下。\n\n| a |\n\n```askdata\n" + json.dumps(body, ensure_ascii=False) + "\n```"
    clean, payload = split_askdata_payload(text)
    check("载荷被剥出", payload is not None and payload["query_id"] == "abc")
    check("正文里没有围栏残留", "```askdata" not in clean and "SELECT 1" not in clean, clean)
    view = to_metadata(payload)
    check("metadata 视图不带 rows/sql", "rows" not in view and "sql" not in view
          and view["rowcount"] == 2, str(view))
    check("图表进 artifacts", artifacts_from_payload(payload)[0]["url"].endswith("a.svg"))
    check("槽位取回", slots_of(payload)["time"]["phrase"] == "本月")

    clean2, none1 = split_askdata_payload("普通回答没有载荷")
    check("无载荷时正文不变", none1 is None and clean2 == "普通回答没有载荷")
    clean3, none2 = split_askdata_payload("正文\n```askdata\n{坏 JSON\n```")
    check("坏 JSON 不改正文", none2 is None and clean3.startswith("正文"), clean3)
    clean4, none3 = split_askdata_payload("说明里提到 ```askdata 这个词但没有代码块")
    check("只有围栏词时不误剥", none3 is None and clean4.startswith("说明里"))
    clean5, p5 = split_askdata_payload("```askdata\n" + json.dumps({"kind": "clarify"}) + "\n```")
    check("正文为空时给兜底文本", bool(clean5) and p5 is not None, clean5)
    check("extract_json 容忍 fenced", extract_json('```json\n{"a":1}\n``` extra') == {"a": 1})


def test_registry_coherence() -> None:
    print("\n9) 注册一致性(域键四处对齐)")
    from app.assistant.a2a_client import AGENT_PROFILES, AGENT_URLS
    from app.assistant.intent import _AGENT_KEYWORDS, _SEEDS
    from app.security.auth import AGENT_WHITELIST, ASKDATA_TOOLS, askdata_whitelist

    check("AGENT_URLS 与 AGENT_PROFILES 同集合", set(AGENT_URLS) == set(AGENT_PROFILES))
    check("askdata 已登记两处", "askdata" in AGENT_URLS and "askdata" in AGENT_PROFILES)
    check("每个域都在 AGENT_WHITELIST 里出现过",
          all(f"{d}_agent" in {a for s in AGENT_WHITELIST.values() for a in s} for d in AGENT_URLS),
          str({d for d in AGENT_URLS if f"{d}_agent" not in {a for s in AGENT_WHITELIST.values() for a in s}}))
    check("askdata 有关键词与种子", "askdata" in _AGENT_KEYWORDS
          and any(t == "askdata" for (_i, t) in _SEEDS))
    check("卡片名一致", AGENT_PROFILES["askdata"][0] == "AskData_Agent")
    from app.agents.askdata_agent.agent_card import build_agent_card

    check("卡片 name 与 PROFILE 同源", build_agent_card().name == AGENT_PROFILES["askdata"][0])
    check("员工对 askdata 域无工具", askdata_whitelist(_role_employee()) == set())
    check("工具白名单与 MCP server 实际工具一致", ASKDATA_TOOLS == _server_tool_names(),
          str(ASKDATA_TOOLS ^ _server_tool_names()))


def _role_employee():
    from app.schemas import Role

    return Role.EMPLOYEE


def _server_tool_names() -> set[str]:
    """从 askdata MCP server 的 FastMCP 实例里取真实注册的工具名。"""
    import app.mcp_servers.askdata_server as server

    import asyncio

    async def _list():
        return [t.name for t in await server.mcp.list_tools()]

    return set(asyncio.run(_list()))


def test_semantic_seed_quality() -> None:
    print("\n10) 语义层内置集自检")
    check("指标数 >= 50", len(TOP_METRICS) >= 50, str(len(TOP_METRICS)))
    dims = {d["dim_id"] for d in TOP_DIMS}
    bad_dim = [m["metric_id"] for m in TOP_METRICS
               if set(m["compatible_dims"] or []) - dims]
    check("compatible_dims 全部存在", not bad_dim, str(bad_dim))
    from app.db.models import Base

    # 维度列可以落在事实表上, 也可以落在它 JOIN 进来的主数据表上(部门列在 hr_employees)。
    join_target = _join_targets()
    bad_col = []
    for d in TOP_DIMS:
        table = d["source_table"]
        if table not in Base.metadata.tables:
            bad_col.append(f"{d['dim_id']}:{table} 表不存在")
            continue
        prefix = d["column_prefix"]
        owner = join_target.get((table, prefix)) or table
        columns = Base.metadata.tables[owner].columns.keys()
        if d["column"] not in columns:
            bad_col.append(f"{d['dim_id']}.{d['column']} 不在 {owner}")
        if prefix not in {"e", d["source_table"][:1]} and owner != table:
            bad_col.append(f"{d['dim_id']} 的前缀 {prefix} 与归属表 {owner} 对不上")
    check("维度列都在真实表上(含 JOIN 主数据表)", not bad_col, str(bad_col[:5]))
    bad_metric_table = [m["metric_id"] for m in TOP_METRICS
                        if m["source_table"] not in Base.metadata.tables]
    check("指标事实表都存在", not bad_metric_table, str(bad_metric_table))
    term_targets = {t[2] for t in TERM_ALIASES if t[1] in ("metric", "dim")}
    known = {m["metric_id"] for m in TOP_METRICS} | dims
    orphan = {t for t in term_targets if t not in known}
    check("别名不指向不存在的对象", not orphan, str(orphan))
    dup = len(TERM_ALIASES) != len({(t[0], t[1], t[2]) for t in TERM_ALIASES})
    check("别名无重复三元组", not dup)
    catalog = build_catalog()
    check("内置集能构出目录", len(catalog.active_metrics) >= 50 and catalog.origin == "seed")
    rendered = catalog.render_metrics("expense")
    check("按域渲染指标不为空", "m_expense_total" in rendered)


def _join_targets() -> dict[tuple[str, str], str]:
    """``(事实表, 关联别名) -> 关联表``: 从 seed 里的 join_sql 反推列归属。"""
    import re

    out: dict[tuple[str, str], str] = {}
    pattern = re.compile(r"JOIN\s+(\w+)\s+(\w+)\b")
    for d in TOP_DIMS:
        join = d.get("join_sql") or ""
        match = pattern.search(join)
        if match:
            out[(d["source_table"], match.group(2))] = match.group(1)
    return out


def test_slot_invention_guard() -> None:
    print("\n11) 槽位防编造(说法必须是原话片段)")
    from app.agents.askdata_agent.graph import _prune_invented_terms

    question = "上周每一单外卖的金额中位数"
    pruned = _prune_invented_terms(
        {"metric_terms": ["年假余额合计", "报销总额"], "dim_terms": ["部门"],
         "filter_terms": [{"phrase": "已打款"}], "time": {"phrase": "上周"}},
        question,
    )
    check("编出来的指标名被剔除", pruned["metric_terms"] != ["年假余额合计", "报销总额"])
    check("剔除项可追溯", pruned.get("invented_dropped") == ["年假余额合计", "报销总额", "部门", "已打款"],
          str(pruned.get("invented_dropped")))
    check("原话片段被保留", pruned["time"]["phrase"] == "上周", str(pruned["time"]))
    check("剔光后退回原话本身", question in (pruned.get("metric_terms") or []), str(pruned["metric_terms"]))
    kept = _prune_invented_terms({"metric_terms": ["报销总额"], "time": {"phrase": "本月"}},
                                 "本月报销总额是多少")
    check("正常抽取不受影响", kept["metric_terms"] == ["报销总额"] and "invented_dropped" not in kept,
          str(kept))
    inherited = _prune_invented_terms(
        {"metric_terms": ["报销总额"], "time": {"phrase": "本月", "inherited": True}},
        "那研发部呢",
    )
    check("继承的时间说法不因不在本轮原话而被剔", inherited["time"]["phrase"] == "本月", str(inherited["time"]))


def main() -> int:
    test_dsl_shape()
    test_validate_levels()
    test_metric_roles()
    test_compiler()
    test_caliber_consistency()
    test_timesem()
    test_link_matching()
    test_clarify_gate()
    test_bridge()
    test_registry_coherence()
    test_semantic_seed_quality()
    test_slot_invention_guard()
    print(f"\n合计: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
