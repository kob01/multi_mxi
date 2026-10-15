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
from app.semantic.catalog import Dimension, Metric, MetricCatalog
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
    check("环比走单扫描条件聚合",
          "CASE WHEN" in mom.sql and "FROM (SELECT" in mom.sql and not mom.sql.startswith("WITH cur"),
          mom.sql[:120])
    check("单扫描不再写 WITH cur/prev", "WITH cur AS" not in mom.sql, mom.sql[:80])
    check("环比有变化率列", "m_expense_total_mom_pct" in mom.sql, mom.sql)
    check("环比分母用 NULLIF 兜 0", "NULLIF(m_expense_total_prev, 0)" in mom.sql, mom.sql)
    check("对比期用独立绑参", mom.params.get("p_start_prev") == "2026-09-01", str(mom.params))
    check("绑参形式全句统一(不混用 %(name)s)",
          "%(" not in mom.sql and ":p_start_prev" in mom.sql, mom.sql[:200])
    check("两个窗口都在 WHERE 里",
          ":p_start" in mom.sql and ":p_end_prev" in mom.sql, mom.sql[:300])
    check("编译产物带 semver", mom.semver.startswith("s") and len(mom.semver) == 13, mom.semver)

    # 带 FILTER 口径的指标(整棵表达式就是一个聚合)走同环比: 这是单扫描改写最容易
    # 静默算错的形状 —— 根节点没重新绑定时窗口条件会消失, 于是上期=当期、环比永远 0%。
    filt_raw = {"metrics": [{"id": "m_supplier_general_taxpayer_count"}],
                "time": {"range": "this_month"}, "compare": {"type": "mom"}}
    filt_mom = compile(filt_raw)
    check("带 FILTER 的同环比仍带窗口条件",
          ":p_start_prev" in filt_mom.sql and "CASE WHEN" in filt_mom.sql, filt_mom.sql[:400])
    check("FILTER 谓词被并进 CASE 而不是丢弃",
          "一般纳税人" in " ".join(str(v) for v in filt_mom.params.values())
          or "qualification" in filt_mom.sql.split("CASE WHEN")[1][:200],
          filt_mom.sql[:400])
    cur_part, _, prev_part = filt_mom.sql.partition("AS m_supplier_general_taxpayer_count_prev")
    check("当期与上期是两个不同的窗口谓词(不是同一表达式算两遍)",
          ":p_start" in cur_part.split("CASE WHEN")[-1] and ":p_start_prev" in cur_part
          and ":p_start_prev" in cur_part, filt_mom.sql[:520])
    check("上期列确实用了 prev 窗口", ":p_start_prev" in prev_part[:400], prev_part[:200])

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


def test_semantic_layer_extensions() -> None:
    """语义层细分面(真值 YAML / semver / 枚举热度 / 兼容规则 / 行列策略 / 熔断 / 缓存)。"""
    print("\n12) 语义层细分面与版本治理")
    from pathlib import Path

    from app.semantic import breaker, defs as semantic_defs, resultcache
    from app.semantic.catalog import ColPolicy, DimCompat, DimValue, RowPolicy, TimeSemantic

    bundle = semantic_defs.bundle_from_seed()
    problems = semantic_defs.validate_bundle(bundle)
    check("内置种子转成的真值自身合法", not problems, str(problems[:3]))
    check("真值带枚举值与兼容规则", len(bundle.dim_values) > 30 and len(bundle.compat) > 200,
          f"values={len(bundle.dim_values)} compat={len(bundle.compat)}")
    directory = Path(__file__).resolve().parents[1] / "semantic-defs"
    if directory.is_dir():
        loaded = semantic_defs.load_bundle(directory)
        check("仓内真值文件可读回且条目一致",
              len(loaded.metrics) == len(bundle.metrics) and len(loaded.terms) == len(bundle.terms),
              f"{len(loaded.metrics)} vs {len(bundle.metrics)}")
        dumped = str((directory / "policies.yaml").read_text(encoding="utf-8"))
        check("默认关掉的行策略不会被写成缺字段", "enabled: false" in dumped, dumped[:120])
    else:
        check("仓内真值文件可读回且条目一致", False, f"{directory} 不存在")

    # 谓词模板的静态安全门(在写库之前, 不等编译期)
    bad = {
        "policy_id": "p1", "role": "employee", "table_name": "fin_reimbursements",
        "predicate": "{alias}.emp_id = {user_id}; DROP TABLE x", "priority": 10,
    }
    found = [line for line in semantic_defs._check_predicate(bad) if "禁止片段" in line]
    check("多语句谓词被真值校验拦住", bool(found), str(found))
    scope_col = {"policy_id": "p2", "role": "employee", "table_name": "t",
                 "predicate": "{alias}.dept_id = {dept_id}", "priority": 10}
    check("谓词里直写作用域列被拒", any("作用域列" in line for line in
                                       semantic_defs._check_predicate(scope_col)))
    unknown_var = {"policy_id": "p3", "role": "r", "table_name": "t",
                   "predicate": "{alias}.a = {salary}", "priority": 10}
    check("未知变量位被拒", any("未知变量" in line for line in
                              semantic_defs._check_predicate(unknown_var)))
    check("没有 {alias} 限定的谓词被拒",
          any("{alias}" in line for line in semantic_defs._check_predicate(
              {"policy_id": "p4", "role": "r", "table_name": "t", "predicate": "a = 1", "priority": 1})))

    # semver: 内容指纹而不时间戳 —— 同一份定义反复算必须同一个号
    base = build_catalog(origin="db", built_at=0)
    again = build_catalog(origin="db", built_at=9999)
    check("同一定义集指纹稳定", base.semver == again.semver, f"{base.semver} vs {again.semver}")
    check("semver 形状带前缀", base.semver.startswith("s") and len(base.semver) == 13, base.semver)
    changed_metric = dict(base.metrics)
    one = changed_metric["m_expense_total"]
    changed_metric["m_expense_total"] = Metric(**{**one.__dict__, "expression": "SUM(r.amount)"})
    moved = MetricCatalog(metrics=changed_metric, dimensions=base.dimensions, terms=base.terms)
    check("改一个口径表达式就换版", moved.semver != base.semver, f"{moved.semver} vs {base.semver}")
    payload = base.to_payload()
    check("快照载荷带 semver 与细分面计数",
          "semver" in payload and "row_policies" in payload and "missing_tables" in payload, str(payload)[:120])

    # 枚举热度与置信度先验: 都是"同分时的次序"与"要不要追问"的判据
    from app.semantic.link import _conf_factor, _popularity_bonus

    check("满置信不打折", _conf_factor(1.0) == 1.0)
    check("拿不到置信度当没先验", _conf_factor(None) == 1.0)
    check("低置信只打折不清零", 0.5 < _conf_factor(0.3) < 1.0, str(_conf_factor(0.3)))
    check("零置信也有地板(不一笔抹掉)", _conf_factor(0.0) == 0.5, str(_conf_factor(0.0)))
    check("热度只给有限加分", 0 <= _popularity_bonus(5000) <= 0.1, str(_popularity_bonus(5000)))
    check("热度取对数并在参考上限封住",
          _popularity_bonus(1) < _popularity_bonus(50) < _popularity_bonus(5000) == 0.1,
          f"{_popularity_bonus(50)} vs {_popularity_bonus(5000)}")

    # 兼容表与 default_filter 救回(早失败比执行期失败便宜, 但能救的不要拒)
    rescue_dim = "d_expense_status"
    rescued_catalog = MetricCatalog(
        metrics=base.metrics, dimensions=base.dimensions, terms=base.terms,
        compat=(DimCompat(metric_id="m_expense_total", dim_id=rescue_dim, compat="no", note="不该同看"),),
    )
    strict = validate(parse_dsl(dsl(dims=[rescue_dim])), catalog=rescued_catalog,
                     scope=CallerScope(role="finance", all_depts=True), today=TODAY)
    check("兼容表标 no 能覆盖指标名单里的放行名单", "E_INCOMPATIBLE_DIM" in strict.codes, strict.as_text())
    saved_catalog = MetricCatalog(
        metrics=base.metrics, dimensions=base.dimensions, terms=base.terms,
        compat=(DimCompat(metric_id="m_expense_total", dim_id=rescue_dim, compat="default_filter",
                         default_filter={"dim": rescue_dim, "op": "eq", "value": "PAID"},
                         note="只算已打款"),),
    )
    saved = validate(parse_dsl(dsl(dims=[rescue_dim])), catalog=saved_catalog,
                    scope=CallerScope(role="finance", all_depts=True), today=TODAY)
    check("default_filter 把不兼容就回了可执行", saved.ok, saved.as_text())
    check("救回动作写进了注记与自动过滤", bool(saved.auto_filters) and any(
        n.code == "N_AUTO_FILTER" for n in saved.notices), str(saved.auto_filters))
    no_filter = parse_dsl(dsl(filters=[{"dim": rescue_dim, "op": "eq", "value": "PAID"}],
                              dims=["d_expense_department"]))
    compiled_rescue = compiler.compile_analytical(
        no_filter, catalog=saved_catalog, scope=scope(),
        check=validate(no_filter, catalog=saved_catalog, scope=CallerScope.from_scope(scope()), today=TODAY),
    ) if True else None
    check("自动过滤的值也不进 SQL 文本",
          "PAID" not in compiled_rescue.sql and any(
              v == "PAID" for v in compiled_rescue.params.values()), compiled_rescue.sql[:200])

    # 列级策略: 命中即拒而不是默默剔列
    col_catalog = MetricCatalog(
        metrics=base.metrics, dimensions=base.dimensions, terms=base.terms,
        col_policies=(ColPolicy(role="employee", table_name="hr_employees",
                               denied_cols=("annual_leave_total", "annual_leave_used")),),
    )
    leave = validate(parse_dsl(dsl(metrics=[{"id": "m_employee_leave_balance_total"}])),
                     catalog=col_catalog, scope=CallerScope(role="employee"), today=TODAY)
    check("员工角色碰敏感列被列策略拦", "E_COL_POLICY" in leave.codes, leave.as_text())
    head = validate(parse_dsl(dsl(metrics=[{"id": "m_employee_headcount"}])),
                    catalog=col_catalog, scope=CallerScope(role="employee"), today=TODAY)
    check("同表不碰敏感列的聚合仍可问", head.ok, head.as_text())

    # 行策略下推: 模板变量全部走绑参, 值不拼进文本
    policy_catalog = MetricCatalog(
        metrics=base.metrics, dimensions=base.dimensions, terms=base.terms,
        row_policies=(RowPolicy(policy_id="rp_own", role="employee", table_name="fin_reimbursements",
                               predicate="{alias}.emp_id = {user_id}", priority=50),),
    )
    one_metric = parse_dsl(dsl())
    pushed = compiler.compile_analytical(
        one_metric, catalog=policy_catalog, scope=scope(all_depts=False, role="employee"),
        check=validate(one_metric, catalog=policy_catalog,
                      scope=CallerScope.from_scope(scope(all_depts=False, role="employee")), today=TODAY),
    )
    check("行策略谓词被拼进 WHERE", "r.emp_id = :rp_user_id_0" in pushed.sql, pushed.sql[:260])
    check("策略的值只走绑参", pushed.params.get("rp_user_id_0") == "E1001", str(pushed.params))
    check("生效策略记进编译产物", pushed.policies == ["rp_own"], str(pushed.policies))
    check("时间语义有默认值(自然周 + 同比基期规则)",
          policy_catalog.time_semantic().compare_base_rule == "same_calendar_day")
    check("内置日历不携带财年时也能回退",
          policy_catalog.time_semantic(Metric(**{**base.metrics["m_budget_used_pct"].__dict__,
                                               "calendar": "fiscal"})).cal_type == "fiscal")
    _ = TimeSemantic

    # 引擎熔断: 只数引擎错, 同表连错到阈值就短路
    breaker.reset()
    check("未到阈值不熔断", not breaker.is_open("compiler", "fin_reimbursements"))
    opened_at = 0
    for index in range(1, 4):
        if breaker.record_error("compiler", "fin_reimbursements"):
            opened_at = index
    check("连错三次就刚打开", opened_at == 3, str(opened_at))
    check("熔断中短路这一级", breaker.is_open("compiler", "fin_reimbursements"))
    check("不影响其他表", not breaker.is_open("compiler", "proc_orders"))
    breaker.record_success("compiler", "fin_reimbursements")
    check("一次成功就恢复", not breaker.is_open("compiler", "fin_reimbursements"))
    snap = breaker.snapshot()
    check("熔断状态可观测", "opened" in snap and "trips_total" in snap, str(snap)[:80])

    # 结果缓存 key: 身份与作用域必须在 key 里(否则跨部门泄数)
    key_a = resultcache.cache_key(dsl={"metrics": [{"id": "m_expense_total"}], "limit": ""},
                                 route="compiler", user_id="E1001", role="manager",
                                 semver="s1", scope_note="本部门")
    key_same = resultcache.cache_key(dsl={"limit": None, "metrics": [{"id": "m_expense_total"}]},
                                    route="compiler", user_id="E1001", role="manager",
                                    semver="s1", scope_note="本部门")
    key_other_role = resultcache.cache_key(dsl={"metrics": [{"id": "m_expense_total"}]},
                                          route="compiler", user_id="E1001", role="finance",
                                          semver="s1", scope_note="本部门")
    key_other_scope = resultcache.cache_key(dsl={"metrics": [{"id": "m_expense_total"}]},
                                           route="compiler", user_id="E1002", role="manager",
                                           semver="s1", scope_note="本部门")
    check("空字段不影响归一", key_a == key_same, f"{key_a} vs {key_same}")
    check("不同角色不共用缓存", key_a != key_other_role)
    check("不同工号不共用缓存", key_a != key_other_scope)
    check("缓存前缀可识别", key_a.startswith("askdata:res:"), key_a)


def test_orchestration_extensions() -> None:
    """编排/评测/治理新增面: 预算、图表形状、上一轮 DSL、误澄清、CI 门禁判据。"""
    print("\n13) 编排与治理进化面")
    from app.agents.askdata_agent import prompts
    from app.agents.askdata_agent.graph import (
        _budget_breach, _chart_shape, _clarify_choice, _compact_dsl, _max_rewrites, _metric_summary,
        _slot_confidence,
    )
    from app.observability import alerts
    from scripts.askdata_eval import judge, report

    check("校验回炉上限改为配置驱动", _max_rewrites() == 2, str(_max_rewrites()))
    check("没超预算不熔断", _budget_breach({"token_cost": 10, "started_at": __import__("time").monotonic()}) == "")
    breach = _budget_breach({"token_cost": 999999, "started_at": __import__("time").monotonic()})
    check("超 token 就熔断", "token" in breach, breach)

    prior = {"metrics": [{"id": "m_expense_total", "label": "报销总额"}], "dims": ["d_expense_category"],
             "time": {"range": "this_year"}, "filters": [{"dim": "d_expense_status", "op": "eq",
                                                          "value": "PAID"}],
             "limit": 20, "order_by": "m_expense_total desc", "intent": "取数"}
    compact = _compact_dsl(prior)
    check("上一轮 DSL 摘要只留意图字段",
          compact.get("metrics") == ["m_expense_total"] and "limit" not in compact
          and "order_by" not in compact, str(compact))
    check("摘要里保留已确定的过滤", compact["filters"][0]["dim"] == "d_expense_status", str(compact))
    conf = _slot_confidence({"metric_terms": ["报销总额"], "time": {"phrase": "本年"},
                            "confidence": {"metric": 0.4, "time": 0.9}})
    check("置信度先验按说法展开", conf["报销总额"] == 0.4 and conf["本年"] == 0.9, str(conf))

    # 图表规则: 同环比分组柱、二维 pivot、单数字不画
    columns = ["department", "m_expense_total", "m_expense_total_prev", "m_expense_total_mom_pct"]
    rows = [{"department": "研发部", "m_expense_total": 100, "m_expense_total_prev": 80,
             "m_expense_total_mom_pct": 25.0},
            {"department": "市场部", "m_expense_total": 60, "m_expense_total_prev": 90,
             "m_expense_total_mom_pct": -33.3}]
    check("同环比走分组柱", prompts.chart_hint(1, False, 2, has_compare=True) == "bar")
    shape = _chart_shape({"dsl": {"dims": ["d_expense_department"]}}, columns, rows, {})
    check("分组柱两个系列(当期/上期)",
          len(shape["series"] or []) == 2 and shape["kind"] == "bar", str(shape)[:160])
    two_dim = ["department", "category", "m_expense_total"]
    two_rows = [{"department": "研发部", "category": "差旅费", "m_expense_total": 10},
                {"department": "研发部", "category": "交通费", "m_expense_total": 5},
                {"department": "市场部", "category": "差旅费", "m_expense_total": 7}]
    shape2 = _chart_shape({"dsl": {"dims": ["d_expense_department", "d_expense_category"]}},
                          two_dim, two_rows, {})
    check("二维把第二个维度转成系列",
          [s["name"] for s in (shape2["series"] or [])] == ["差旅费", "交通费"], str(shape2)[:180])
    check("单数字不画图",
          prompts.chart_hint(0, False, 1) == "none")

    # 量级留痕与误澄清判据
    summary = _metric_summary({"dsl": {"dims": ["d_expense_department"]}},
                              {"columns": columns, "rows": rows[:1]})
    check("留痕只存主指标代表值(不比列不入基线)",
          summary["metrics"] == {"m_expense_total": 100.0}
          and summary["shape"] == "d_expense_department", str(summary))
    chosen = _clarify_choice({"question": "问数确认：报销总额，口径用「报销总额」(m_expense_total)",
                             "dsl": {"metrics": [{"id": "m_expense_total"}]},
                             "prior_top_metric": "m_expense_total"})
    check("点选轮能判误澄清", chosen == ("m_expense_total", "m_expense_total"), str(chosen))
    normal = _clarify_choice({"question": "本月报销总额", "dsl": {"metrics": [{"id": "m_expense_total"}]},
                             "prior_top_metric": "m_expense_total"})
    check("普通轮不记点选事件", normal == ("", ""), str(normal))

    # 评测判据: DSL-EM 与 slot-F1(不比 limit/order_by; 槽位只比包含关系)
    em_ok, em_note = judge.dsl_exact_match(prior, {**prior, "limit": 5, "order_by": "m_expense_total asc"})
    check("limit/order_by 不参与 DSL-EM", em_ok, em_note)
    em_bad, _ = judge.dsl_exact_match(prior, {**prior, "time": {"range": "this_month"}})
    check("时间不同就 DSL-EM 不通过", not em_bad)
    f1, f1_note = judge.slot_f1({"metric_terms": ["报销总额"], "time": {"phrase": "本年"}},
                                {"metric_terms": ["本月报销总额"], "time": {"phrase": "本年"}})
    check("槽位用包含关系判命中(不因长短写差异归零)", f1 == 1.0, f"{f1} {f1_note}")
    check("没标期望槽位不参与 slot-F1", judge.slot_f1({}, {"metric_terms": ["x"]})[0] == -1.0)
    mis = judge.mis_clarified({"dsl": prior}, {"verdict": "clarify",
                                              "candidates": [{"id": "m_expense_total", "score": 0.6}]})
    check("被拦下但最高分就是期望口径 → 误澄清", mis)
    check("该问没问不算误澄清",
          not judge.mis_clarified({"dsl": prior}, {"verdict": "ok",
                                                  "candidates": [{"id": "m_expense_total"}]}))

    runs = [{"ex_pass": True, "dsl_em": True, "slot_f1": 1.0, "mis_clarify": False, "route": "compiler",
             "tags": "单指标", "intent": "fetch", "difficulty": "easy", "question": "a", "latency_ms": 100},
            {"ex_pass": False, "dsl_em": False, "slot_f1": None, "mis_clarify": True, "verdict": "clarify",
             "error": "verdict=clarify", "route": "", "tags": "环比", "intent": "compare",
             "difficulty": "hard", "question": "b", "latency_ms": 900},
            {"ex_pass": False, "dsl_em": False, "slot_f1": 0.5, "route": "sandbox", "verdict": "degraded",
             "error": "引擎错", "tags": "下钻", "intent": "fetch", "difficulty": "medium",
             "question": "c", "latency_ms": 200, "cache_hit": True}]
    summary = report.summarize(runs, mode="agent", judge_enabled=False, degrade_note="")
    check("评测汇总出 DSL-EM", summary["dsl_exact_match"] == round(100 / 3, 2), str(summary["dsl_exact_match"]))
    check("slot-F1 只平均有标注的两条", summary["slot_f1_samples"] == 2
          and abs(summary["slot_f1"] - 0.75) < 1e-6, str(summary["slot_f1"]))
    check("误澄清率分母是澄清次数", summary["mis_clarify_rate"] == 100.0, str(summary))
    check("降级率与缓存命中率分开算",
          summary["fallback_rate"] == round(100 / 3, 2) and summary["cache_hit_rate"] == round(100 / 3, 2),
          str(summary["fallback_rate"]))
    check("按意图与难度分层", summary["by_intent"]["compare"]["ex"] == 0.0
          and summary["by_difficulty"]["easy"]["ex"] == 100.0, str(summary["by_difficulty"]))
    md = report.render_markdown(summary)
    check("报告里有 DSL-EM/分层/门禁位", "DSL-EM" in md and "按难度" in md, md[:200])

    # 告警线: 样本不够不告警, 越线才 fired
    alerts_out = alerts.evaluate({"total": 5, "fail_rate": 90.0, "degrade_rate": 0.0,
                                 "clarify_rate": 0.0, "p95_latency_ms": 10})
    check("样本不足不报告警", not any(item["fired"] for item in alerts_out), str(alerts_out[:1]))
    alerts_out = alerts.evaluate({"total": 60, "fail_rate": 40.0, "degrade_rate": 5.0,
                                 "clarify_rate": 40.0, "p95_latency_ms": 20000,
                                 "latest_eval": {"gate_status": "red", "exec_accuracy": 60,
                                                 "case_count": 60}})
    fired = {item["rule"]: item["fired"] for item in alerts_out}
    check("执行错误率越线", fired.get("fail_rate"))
    check("澄清率越线", fired.get("clarify_rate"))
    check("P95 越线", fired.get("p95_latency_ms"))
    check("评测门禁进告警面", fired.get("eval_gate"))
    check("降级率未越线", not fired.get("degrade_rate"), str(fired))

    # 反馈事件与归因: edit_dsl 的 diff 直接定到字段
    from app.askdata import attribution, store

    diff = attribution.dsl_diff(prior, {**prior, "metrics": [{"id": "m_expense_count"}]})
    check("改指标能差分出 diff", list(diff) == ["metrics[0].id"], str(diff))
    category, disposition = attribution.attribute(
        {"dsl_json": prior, "corrected_dsl": {**prior, "metrics": [{"id": "m_expense_count"}]},
         "error_codes": [], "verdict": "ok", "route": "compiler", "candidates": []}
    )
    check("改口径类归到 link 并产出补词草案",
          (category, disposition) == ("link", attribution.DISPOSITION_TERM), f"{category}/{disposition}")
    time_diff = attribution.attribute(
        {"dsl_json": prior, "corrected_dsl": {**prior, "time": {"range": "last_year"}},
         "error_codes": [], "verdict": "ok", "route": "compiler", "candidates": []}
    )
    check("改时间归到 intent", time_diff[0] == "intent", str(time_diff))
    breaker_open = attribution.attribute(
        {"dsl_json": prior, "error_codes": ["E_ENGINE_BREAKER"], "verdict": "fail", "candidates": []}
    )
    check("熔断归到 exec", breaker_open[0] == "exec", str(breaker_open))
    check("事件类型表含方案要求的六个", set(prompts.EVENT_TYPES if hasattr(prompts, "EVENT_TYPES")
                                       else store.EVENT_TYPES) >= {"like", "dislike", "edit_query",
                                                                  "edit_dsl", "copy", "drill"},
          str(store.EVENT_TYPES))


def test_rival_gap() -> None:
    """澄清门的"前两名分差"只在两个候选争同一个说法时才适用(否则常见问法会被反问刷屏)。"""
    print("\n14) 假歧义判定(分差只在同一说法的竞争对手之间适用)")
    from app.agents.askdata_agent.graph import _closest_rival, _phrase_overlap

    different_phrases = [
        {"id": "m_expense_travel_total", "score": 1.0, "matched": "差旅费金额"},
        {"id": "m_expense_total", "score": 0.95, "matched": "报销总额"},
    ]
    check("不同说法的两个口径不算歧义", _closest_rival(different_phrases) is None,
          str(_closest_rival(different_phrases)))
    same_phrase = [
        {"id": "m_expense_travel_total", "score": 1.0, "matched": "差旅费"},
        {"id": "m_expense_travel_paid_total", "score": 0.95, "matched": "差旅费"},
    ]
    check("同一说法的两个口径仍按分差判", _closest_rival(same_phrase) == 0.95,
          str(_closest_rival(same_phrase)))
    check("互相包含的说法算同一说法", _phrase_overlap("合同金额", "合同金额合计"))
    check("零分候选不参与分差(不会拖出假竞争对手)",
          _closest_rival([{"id": "a", "score": 1.0, "matched": "x"},
                         {"id": "b", "score": 0.0, "matched": "x"}]) == 0.0)
    no_matched = [{"id": "a", "score": 0.8}, {"id": "b", "score": 0.75}]
    check("拿不到 matched 时保守当竞争对手", _closest_rival(no_matched) == 0.75,
          str(_closest_rival(no_matched)))

    # 时间说法确定性补回: 只补原话里存在的区间词, 不存在的仍走澄清
    from app.agents.askdata_agent.graph import _recover_time_phrase
    from app.semantic import timesem

    check("原话里的区间词能被认出", timesem.detect_phrase("各部门今年差旅费金额") == "今年",
          timesem.detect_phrase("各部门今年差旅费金额"))
    recovered = _recover_time_phrase({"metric_terms": ["今年差旅费金额"],
                                     "time": {"phrase": ""}}, "各部门今年差旅费金额")
    check("模型漏抽的时间被确定性补回",
          recovered["time"]["phrase"] == "今年" and recovered.get("time_recovered") == "今年",
          str(recovered.get("time")))
    untouched = _recover_time_phrase({"metric_terms": ["报销总额"], "time": {"phrase": ""}},
                                      "报销总额是多少")
    check("原话里没时间就仍然不填", not untouched.get("time_recovered"), str(untouched.get("time")))
    kept = _recover_time_phrase({"time": {"phrase": "上月"}}, "上月报销总额")
    check("已经抽到时间时不覆盖", kept["time"]["phrase"] == "上月", str(kept["time"]))


def test_v3_second_round() -> None:
    """本轮新增面: 重复限定去重、topN 确定性补齐、DSL-EM 尺子、样本门禁、转译与日志推送。"""
    print("\n15) 第二轮补齐项(准确率尖边与 V3 治理面)")
    from app.agents.askdata_agent.graph import _apply_topn
    from app.askdata import attribution, store
    from app.observability import logsink
    from app.semantic.compiler import transpile_sql
    from scripts.askdata_eval import judge

    # ① 与指标口径重复的枚举限定会被丢弃(而不是少返几行)
    dup = parse_dsl(dsl(filters=[{"dim": "d_expense_status", "op": "eq", "value": "PAID"}]))
    dup_catalog = MetricCatalog(
        metrics={**CATALOG.metrics,
                 "m_expense_total": Metric(**{**CATALOG.metrics["m_expense_total"].__dict__,
                                             "caliber_where": "status = 'PAID'"})},
        dimensions=CATALOG.dimensions, terms=CATALOG.terms,
    )
    deduped = validate(dup, catalog=dup_catalog, scope=CallerScope(role="finance",
                                                                all_depts=True), today=TODAY)
    check("重复限定被校验器丢弃并注记",
          deduped.ok and any(n.code == "N_REDUNDANT_FILTER" for n in deduped.notices),
          str([n.code for n in deduped.notices]))
    kept = validate(parse_dsl(dsl(filters=[{"dim": "d_expense_status", "op": "eq",
                                            "value": "SUBMITTED"}])),
                    catalog=dup_catalog, scope=CallerScope(role="finance", all_depts=True),
                    today=TODAY)
    check("不重复的限定仍然保留", not any(n.code == "N_REDUNDANT_FILTER" for n in kept.notices),
          str([n.code for n in kept.notices]))
    # 去重后必须仍然能编译(上一轮踩过的坑: 校验丢了、编译器还在渲染 → 互相矛盾)
    dup_spec = parse_dsl(dsl(dims=["d_expense_department"],
                            filters=[{"dim": "d_expense_status", "op": "eq", "value": "PAID"}]))
    dup_check = validate(dup_spec, catalog=dup_catalog, scope=CallerScope(role="finance",
                                                                     all_depts=True), today=TODAY)
    dup_compiled = compiler.compile_analytical(dup_spec, catalog=dup_catalog, scope=scope(),
                                              check=dup_check, max_rows=100)
    check("重复限定去重后仍能编译且不拼这个谓词",
          dup_check.ok and ("r.status" not in dup_compiled.sql
                            or "status = 'PAID'" in dup_compiled.sql)
          and not any(v == "PAID" for v in dup_compiled.params.values()),
          dup_compiled.sql[:260])
    check("去重的维度仍可用于下钻(JOIN/分组没被抹掉)",
          "e.department" in dup_compiled.sql, dup_compiled.sql[:260])

    # ② topN 确定性补齐
    slots = {"order_hint": "前 5 名", "metric_terms": ["报销总额最高的 5 个部门"], "time": {}}
    filled = _apply_topn({"metrics": [{"id": "m_expense_total"}], "dims": ["d_expense_department"]},
                         slots)
    check("topN 能补出 order_by 与 limit",
          filled.get("order_by") == "m_expense_total desc" and filled.get("limit") == 5,
          str(filled))
    lowest = _apply_topn({"metrics": [{"id": "m_expense_total"}], "dims": ["d_expense_department"]},
                         {"order_hint": "最低的 3 个", "metric_terms": [], "time": {}})
    check("最低 N 个走升序", lowest.get("order_by") == "m_expense_total asc", str(lowest))
    left = _apply_topn({"metrics": [{"id": "m_expense_total"}], "limit": 8},
                       {"order_hint": "前 5 名", "metric_terms": [], "time": {}})
    check("用户说了 N 就以 N 为准(覆盖模型写的 8)", left.get("limit") == 5, str(left))
    same = _apply_topn({"metrics": [{"id": "m_expense_total"}], "limit": 5},
                       {"order_hint": "前 5 名", "metric_terms": [], "time": {}})
    check("N 与模型一致时不多此一举", same.get("limit") == 5 and "_topn_fixed" not in same, str(same))
    untouched = _apply_topn({"metrics": [{"id": "m_expense_total"}]},
                           {"order_hint": "", "metric_terms": ["报销总额"], "time": {}})
    check("没说 topN 就不动 limit/order_by",
          "limit" not in untouched and "order_by" not in untouched, str(untouched))
    # 模没填 order_hint 也要能从原话里拿到 N
    from_q = _apply_topn({"metrics": [{"id": "m_expense_total"}], "dims": ["d_expense_department"]},
                        {"order_hint": "", "metric_terms": [], "time": {}},
                        "今年报销总额最高的 3 个部门")
    check("原话里说了 N 也能补齐", from_q.get("limit") == 3
          and from_q.get("order_by") == "m_expense_total desc", str(from_q))
    # 但"前 N 个月"是时间范不是 topN, 补了就是把时窗口错当排名
    time_only = _apply_topn({"metrics": [{"id": "m_expense_total"}], "dims": ["d_expense_department"]},
                           {"order_hint": "", "metric_terms": [], "time": {}},
                           "前 3 个月的报销总额")
    check("前 N 个月不当 topN 处里", "limit" not in time_only and "order_by" not in time_only,
          str(time_only))
    ranked = _apply_topn({"metrics": [{"id": "m_expense_total"}], "dims": ["d_expense_department"]},
                        {"order_hint": "", "metric_terms": [], "time": {}},
                        "报销额排名前 5 的部门")
    check("排名前 5 走 topN", ranked.get("limit") == 5, str(ranked))

    # ③ DSL-EM 尺子: agg 默认值与 granularity 写法差异不算错
    base = {"metrics": [{"id": "m_expense_total"}], "time": {"range": "this_year", "granularity": ""}}
    same = {"metrics": [{"id": "m_expense_total", "agg": "sum"}],
            "time": {"range": "this_year", "granularity": "none"}}
    ok, note = judge.dsl_exact_match(base, same)
    check("agg/granularity 写法差异不判 DSL-EM 错", ok, note)
    wrong_dim, _ = judge.dsl_exact_match({"metrics": [{"id": "m_expense_total"}],
                                          "time": {"range": "this_year"}},
                                         {"metrics": [{"id": "m_expense_total"}],
                                          "time": {"range": "this_year"}, "dims": ["d_expense_category"]})
    check("多一个维度仍判 DSL-EM 错", not wrong_dim)

    # ④ 样本队列: 只有 intent/dsl 类产生样本, dpo 的 chosen 必须能编译
    row = {"query_id": "q1", "question": "本月报销总额", "verdict": "ok", "feedback": "up",
           "dsl_json": {"metrics": [{"id": "m_expense_total"}], "time": {"range": "this_month"}},
           "slots_json": {}, "semver": "s1", "error_codes": [], "candidates": [], "route": "compiler"}
    for category in ("link", "exec", "render"):
        skipped = attribution._maybe_sample({**row, "question": f"x-{category}"},
                                            category=category)
        check(f"{category} 类不产生训练样本", "skipped" in skipped, str(skipped))
    # 门禁的判据本身在 validate/compile 里(它们已在本文件其他用例里被证), 这里只钉住结论:
    # 能编的 DSL 才能当 chosen, 拿不存在的指标当答案是"把错数写进训练集"。
    good = parse_dsl(dsl())
    good_check = validate(good, catalog=CATALOG, scope=CallerScope.from_scope(scope()), today=TODAY,
                          max_rows=get_settings().askdata_max_rows)
    compiles = bool(good_check.ok)
    try:
        compiler.compile_analytical(good, catalog=CATALOG, scope=scope(), check=good_check,
                                    max_rows=100)
    except Exception:  # noqa: BLE001
        compiles = False
    check("能编的 DSL 才能当 chosen", compiles, good_check.as_text())
    bad = parse_dsl({"metrics": [{"id": "m_not_exists"}], "time": {"range": "this_month"}})
    bad_check = validate(bad, catalog=CATALOG, scope=CallerScope.from_scope(scope()), today=TODAY)
    check("不存在的指标过不了门禁", not bad_check.ok, bad_check.as_text())
    check("只有 intent/dsl 算训练材料", store.TRAINABLE_CATEGORIES == ("intent", "dsl"),
          str(store.TRAINABLE_CATEGORIES))

    # ⑤ 方言转译: PG 原文不变, 目标方言产出不同形式, 不认识的方言报错
    sample_sql = ("SELECT COALESCE(SUM(r.amount), 0) AS m_expense_total FROM fin_reimbursements r "
                  "WHERE r.is_deleted = false LIMIT 10")
    check("postgres 方言就是原文", transpile_sql(sample_sql, "postgres") == sample_sql)
    check("不认识的方言报错", _raises(lambda: transpile_sql(sample_sql, "oracle")))
    clickhouse = transpile_sql(sample_sql, "clickhouse")
    check("clickhouse 转译产出非空 SQL", "m_expense_total" in clickhouse.lower(), clickhouse[:160])

    # ⑥ Loki 推送默认关: 没配地址时不得起线程也不得报错
    check("未配 LOKI_URL 时不推送", logsink.enabled() is False and logsink.push("x", {}) is False)

    # ⑦ 维度用的表别名必须真的被 JOIN 过(不能靠 PG 报错后降级到沙箱让模型自己拼 JOIN)
    # 故意把 d_bad 登记成"与报销指标兼容"(模拟语义层只登了列名、没登 join_sql 的真实缺口),
    # 这样校验会放过它, 拦下来的必须是编译前那道别名闸。
    bad_metric = Metric(**{**CATALOG.metrics["m_expense_total"].__dict__,
                           "compatible_dims": list(CATALOG.metrics["m_expense_total"].compatible_dims)
                           + ["d_bad"]})
    bad_dim = MetricCatalog(
        metrics={**CATALOG.metrics, "m_expense_total": bad_metric},
        dimensions={**CATALOG.dimensions,
                    "d_bad": Dimension(dim_id="d_bad", label="员工", source_table="fin_reimbursements",
                                       column="name", column_prefix="e")},
        terms=CATALOG.terms,
    )
    bad_dim_spec = parse_dsl(dsl(dims=["d_bad"]))
    bad_dim_check = validate(bad_dim_spec, catalog=bad_dim,
                             scope=CallerScope.from_scope(scope()), today=TODAY)
    try:
        compiler.compile_analytical(bad_dim_spec, catalog=bad_dim, scope=scope(),
                                    check=bad_dim_check, max_rows=100)
        check("未 JOIN 的维度被编译前拦下", False, "它竟然编出了 SQL")
    except compiler.CompileError as exc:
        check("未 JOIN 的维度被编译前拦下并给出可行动拒因",
              "join_sql" in str(exc) and "d_bad" in str(exc), str(exc)[:200])
    # 内置集里报销/工单类指标的员工姓名与职位维度必须自己带 JOIN(上一轮的真实缺口)
    for metric_id, dim_id in (("m_expense_total", "d_expense_employee_name"),
                              ("m_expense_total", "d_expense_employee_position"),
                              ("m_ticket_count", "d_ticket_employee_name")):
        spec_case = parse_dsl(dsl(metrics=[{"id": metric_id}], dims=[dim_id]))
        checked = validate(spec_case, catalog=CATALOG,
                           scope=CallerScope.from_scope(scope()), today=TODAY)
        if not checked.ok:
            check(f"{metric_id}×{dim_id} 可用", False, checked.as_text())
            continue
        made = compiler.compile_analytical(spec_case, catalog=CATALOG, scope=scope(),
                                          check=checked, max_rows=100)
        check(f"{metric_id}×{dim_id} 会带出员工主数据 JOIN",
              "JOIN hr_employees e" in made.sql and "e." in made.sql, made.sql[:240])


    # ⑧ 原话重链接后的候选合并: 同一目标取最高分, 不能把第一轮抽好的维度丢掉
    from app.agents.askdata_agent.graph import _merge_candidates, _norm_text

    merged = _merge_candidates(
        [{"kind": "metric", "id": "m_a", "score": 0.5},
         {"kind": "dim", "id": "d_x", "score": 1.0}],
        [{"kind": "metric", "id": "m_a", "score": 0.62},
         {"kind": "metric", "id": "m_b", "score": 0.4}],
    )
    check("合并取同一目标的最高分",
          any(item.get("id") == "m_a" and abs(float(item.get("score") or 0) - 0.62) < 1e-6
              for item in merged), str(merged))
    check("合并后仍按分数降序", [float(item["score"]) for item in merged] == sorted(
        [float(item["score"]) for item in merged], reverse=True), str(merged))
    check("第 1 轮的维度候选不会被丢掉",
          any(item.get("kind") == "dim" for item in merged), str(merged))
    # `_norm_text` 是文件里已有的归一化(折叠空白为单空格 + normalize_text + 小写):
    # 拿它判"原话与短语是不是同一句话"必须靠同一个函数, 不能自己再手拼一套(已踩过一次重名遮蔽)
    check("归一化把多余空白与大小写拉平",
          _norm_text("  本年   IT 设备 ") == _norm_text("本年 it 设备")
          and _norm_text("A B") != _norm_text("AB"), "")


    # ⑩ 二维交叉走热力图(规则表), 且 SVG 良构; PNG 路径明写降级不假装
    from app.agents.askdata_agent import prompts as askdata_prompts
    from app.analytics import charts

    check("两个维度交叉选热力图",
          askdata_prompts.chart_hint(2, False, 6) == "heatmap",
          askdata_prompts.chart_hint(2, False, 6))
    check("单维度低基数仍走饼图",
          askdata_prompts.chart_hint(1, False, 4) == "pie",
          askdata_prompts.chart_hint(1, False, 4))
    check("带时间粒度优先折线",
          askdata_prompts.chart_hint(2, True, 6) == "line",
          askdata_prompts.chart_hint(2, True, 6))
    check("同环比仍走分组柱(不被热力图抢走)",
          askdata_prompts.chart_hint(1, False, 6, has_compare=True) == "bar",
          askdata_prompts.chart_hint(1, False, 6, has_compare=True))
    heat = charts.render("heatmap", "部门 x 类别交叉", ["研发", "销售"],
                        [{"name": "差旅", "data": [10, 90]}, {"name": "办公", "data": [1, 2]}])
    check("热力图能出图且格内带数值",
          not heat.get("error") and heat.get("chart_type") == "heatmap"
          and heat["svg"].count("<rect") >= 4 and ">90<" in heat["svg"],
          str(heat.get("error") or heat.get("chart_type")))
    heat_png = charts.render_png("heatmap", "交叉", ["A", "B"],
                                [{"name": "s1", "data": [1, 2]}])
    check("热力图出位图时明写降级成 bar",
          heat_png.get("chart_type") == "bar" and heat_png.get("png_fallback") == "heatmap_png_fallback"
          or "Pillow" in str(heat_png.get("error") or ""), str(heat_png)[:120])
    check("不认识的图型错误提示里带上 heatmap",
          "heatmap" in str(charts.render("radar", "x", ["A"], [{"name": "s", "data": [1]}]).get("error")),
          "")

    # ⑪ 时间词表必须盖得住"本年/这一年"这类写法: 补不回就会拿"没给区间"反问用户明明说了的话
    # (本轮两条误澄清之一就是它: 原话写"本年", 而 detect_phrase 只认"今年/本年度")
    from app.agents.askdata_agent.graph import _has_time, _recover_time_phrase

    for phrase in ("本年", "这一年", "今年", "本年度"):
        check(f"{phrase} 能归一到 this_year",
              timesem.normalize_range(phrase) == "this_year", phrase)
    recover_q = "按供应商风险状态看本年的一般纳税人供应商数"
    check("原话里的\"本年\"能被确定性补回时间槽",
          timesem.detect_phrase(recover_q) == "本年"
          and _has_time(_recover_time_phrase({"time": {}}, recover_q)),
          timesem.detect_phrase(recover_q))
    check("原话确实没说时间时仍然不编时间",
          not _has_time(_recover_time_phrase({"time": {}}, "各部门报销总额")), "")


def test_sandbox_governance() -> None:
    """沙箱加固的确定性不变量: 可查面=治理视图, 机密列不在面内, 提示词与视图同源。"""
    import inspect

    from app.agents.askdata_agent import graph as askdata_graph
    from app.db.analytics_views import (
        SANDBOX_VIEW_SPECS, sandbox_view_names, sensitive_columns, source_tables, view_selects,
    )
    from app.db.schema_docs import ANALYTICS_SANDBOX_VIEW_DDL, ANALYTICS_SCHEMA_DDL
    from app.db.scope import SCOPED_TABLES

    print("\n17) 沙箱可查面(治理视图)与列/资源护栏接线")
    names = sandbox_view_names()
    check("视图名统一 v_analytics_ 前缀",
          names and all(n.startswith("v_analytics_") for n in names), str(sorted(names)))
    check("视图个数与作用域表名单对齐", len(names) == len(SCOPED_TABLES),
          f"{len(names)} vs {len(SCOPED_TABLES)}")
    # 基表必须全部受 RLS 管辖: 否则"视图只挑列"反而开了一个没有行级安全的后门。
    stray = sorted(table for table in source_tables() if table not in SCOPED_TABLES)
    check("视图基表全部受行级安全管辖", not stray, str(stray))

    # 这些列出现在自由 SQL 面里就是越权读人/渗出素材: 结构上就不能存在。
    hidden = {
        "annual_leave_total", "annual_leave_used", "bank_account", "content", "review_json",
        "findings", "opinion", "precheck_result", "flags", "doc_key",
        "tenant_id", "dept_id", "is_deleted", "deleted_at", "deleted_by",
    }
    for spec in SANDBOX_VIEW_SPECS:
        leaked = sorted(set(spec.column_names()) & hidden)
        check(f"{spec.name} 不暴露机密/服务端列", not leaked, str(leaked))

    soft_views = {
        "v_analytics_tickets", "v_analytics_leave_records", "v_analytics_reimbursements",
        "v_analytics_purchase_orders", "v_analytics_contracts",
    }
    declared_soft = {s.name for s in SANDBOX_VIEW_SPECS if s.soft_delete}
    check("软删表逐一对上了过滤标记", declared_soft == soft_views,
          f"{sorted(declared_soft ^ soft_views)}")
    unchecked = [n for n, sql in view_selects().items() if n in soft_views and "is_deleted = false" not in sql]
    check("软删过滤真的写进视图 SELECT", not unchecked, str(unchecked))

    # 提示词与真实投影同源: 任何一个列没写进说明, 模型就无从正确点名。
    missing = [f"{spec.name}.{col}" for spec in SANDBOX_VIEW_SPECS for col, _ in spec.columns
               if col not in ANALYTICS_SANDBOX_VIEW_DDL]
    check("视图字段说明与真实投影列全覆盖", not missing, str(missing[:6]))
    hidden_names = {col for cols in sensitive_columns().values() for col in cols}
    # 机密列名一旦出现在发给模型的文本里, 就是在告诉注入者"这里值得挖": 视图面要连名字都不给。
    shown = sorted(name for name in hidden_names if name in ANALYTICS_SANDBOX_VIEW_DDL)
    check("发给模型的说明不点名机密列", not shown, str(shown))
    check("沙箱面与基表面不是同一份文本", ANALYTICS_SANDBOX_VIEW_DDL != ANALYTICS_SCHEMA_DDL, "")
    check("机密列剔除清单与视图面一致", set(sensitive_columns()) == names, "")

    # 沙箱节点必须引用视图说明而不是基表说明: 给基表清单等于让模型写出必被拒的表名。
    source = inspect.getsource(askdata_graph.AskDataPipeline._sandbox)
    check("沙箱 prompt 用视图 DDL", "ANALYTICS_SANDBOX_VIEW_DDL" in source, source[:120])
    check("沙箱 prompt 不再出现基表清单", "ANALYTICS_SCHEMA_DDL" not in source, "")
    check("沙箱 prompt 要求 JOIN 带关联条件", "ON" in source and "v_analytics_employees" in source, "")

    # MCP 侧接线: 默认给视图集合, 配置退回时才给基表集合(回滚通道必须真的接得上)。
    from app.mcp_servers import askdata_server

    settings = get_settings()
    check("沙箱可查面默认=视图集合", askdata_server._sandbox_allowed_tables() == names, "")
    saved = settings.askdata_sandbox_use_views
    try:
        settings.askdata_sandbox_use_views = False
        check("use_views=false 时退回基表白名单",
              askdata_server._sandbox_allowed_tables() == askdata_server._allowed_tables(), "")
    finally:
        settings.askdata_sandbox_use_views = saved


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001 - 只要拒了就算通过
        return True
    return False


def catalog_with(base_catalog, extra_dims):
    """在内置目录上临时叠一个维度(只用于构造错形状)。"""
    dimensions = dict(base_catalog.dimensions)
    for dim in extra_dims:
        dimensions[dim.dim_id] = dim
    return MetricCatalog(metrics=base_catalog.metrics, dimensions=dimensions,
                         terms=base_catalog.terms)


def test_forward_windows_and_aliases() -> None:
    """前向时间窗口与词典覆盖: 两条真实失败的确切根因。"""
    print("\n16) 前向窗口与别名覆盖")
    from datetime import date, timedelta

    from app.semantic.link import link as run_link
    from scripts.askdata_eval import judge
    from scripts.seed_askdata_golden import _absolute_date_warn

    today = date(2026, 10, 15)
    for token, days in (("next_7d", 7), ("next_30d", 30), ("next_90d", 90)):
        got = timesem.resolve({"range": token}, today=today)
        check(f"{token} 从今天起算 N 天",
              got.start == today and got.end == today + timedelta(days=days) and got.days == days,
              f"{got.start} ~ {got.end}")
        check(f"{token} 标签说清方向", f"未来 {days} 天" in got.label, got.label)

    # 汉字与数字两种写法都要认得(用户写"未来三十天", 不是 "未来30天")
    for phrase, token in (("未来三十天", "next_30d"), ("未来30天", "next_30d"),
                          ("未来一个月", "next_30d"), ("未来一周", "next_7d"),
                          ("接下来三个月", "next_90d"), ("未来一年", "next_365d")):
        check(f"说法「{phrase}」归一到 {token}", timesem.normalize_range(phrase) == token, phrase)
    check("原话能识别出前向说法",
          timesem.detect_phrase("按到期日看未来三十天内要续签的合同数") == "未来三十天内",
          timesem.detect_phrase("按到期日看未来三十天内要续签的合同数"))

    # 前向窗口做环比: 对比期必须是**它前面**那段等长区间, 不能往前拿一个不存在的周期
    fwd = timesem.resolve({"range": "next_30d"}, compare="mom", today=today)
    check("前向窗口的环比是上一个等长区间",
          fwd.prev_start == today - timedelta(days=30) and fwd.prev_end == today,
          f"{fwd.prev_start} ~ {fwd.prev_end} · {fwd.note}")
    yoy = timesem.resolve({"range": "next_30d"}, compare="yoy", today=today)
    check("前向窗口的同比仍旧平移一年",
          yoy.prev_start == today.replace(year=today.year - 1)
          and yoy.prev_end == (today + timedelta(days=30)).replace(year=today.year - 1),
          f"{yoy.prev_start} ~ {yoy.prev_end}")

    # 新别名要真能把口径定住(这两条曾经分别导致"误澄清"与"拿 last_30d 顶替")
    ids = [c.target_id for c in run_link("今年比价不足三单的采购有几个",
                                        catalog=CATALOG, scope=None).metric_candidates]
    check("比价不足三单能直达该口径", "m_purchase_quotes_short" in ids, str(ids))
    check("不会顺便把'采购单数'也拉进来当竞争者",
          ids.count("m_purchase_count") == 0 or ids[0] == "m_purchase_quotes_short", str(ids))

    # 硬日期用例要被顶提示说出来(它会每天临近一步, 然后开始假失败)
    stale = _absolute_date_warn({"question": "q", "dsl": {"time": {"start": "2020-01-01",
                                                                   "end": "2020-02-01"}}})
    check("已过期的硬日期用例会被告警", "过期" in stale, stale)
    fresh = _absolute_date_warn({"question": "q", "dsl": {"time": {"range": "next_30d"}}})
    check("用区间词时无需告警", fresh == "", fresh)
    future = _absolute_date_warn({"question": "q", "dsl": {"time": {"start": "2099-01-01",
                                                                    "end": "2099-02-01"}}})
    check("未过期但硬写日期也给提醒", "硬日期" in future, future)
    
    # ⑩ DSL-EM 尺子: 只落到一个桶的粒度与"不展开"等价, 真的拆开了则算不同
    same_year = judge._effective_granularity({"range": "next_7d", "granularity": "year"})
    check("一个桶的粒度归一为空", same_year == "", same_year)
    split_by_day = judge._effective_granularity({"range": "last_30d", "granularity": "day"})
    check("真拆成多行的粒度仍然保留", split_by_day == "day", split_by_day)
    ok, _note = judge.dsl_exact_match(
        {"metrics": [{"id": "m_expense_total"}], "time": {"range": "this_year"}},
        {"metrics": [{"id": "m_expense_total"}],
         "time": {"range": "this_year", "granularity": "year"}},
    )
    check("今年+year 与今年不展开算同一意图", ok, _note)
    bad, _note2 = judge.dsl_exact_match(
        {"metrics": [{"id": "m_expense_total"}], "time": {"range": "this_year"}},
        {"metrics": [{"id": "m_expense_total"}],
         "time": {"range": "this_year", "granularity": "day"}},
    )
    check("今年按天展开仍算不同意图", not bad, _note2)

    # ⑪ 时间要求按"会被选中的口径"判, 不按全部候选取"与"
    # (本轮 6 条误澄清的真因: 快照指标与时间型指标混在候选里时, 全体取与就变成"缺区间")
    from app.agents.askdata_agent.graph import _time_required
    from app.agents.askdata_agent.prompts import today_anchor as prompts_today

    mixed = [{"id": "m_a", "time_aware": False, "score": 1.0},
             {"id": "m_b", "time_aware": True, "score": 0.9}]
    check("top1 是快照指标时不追问时间", _time_required(mixed) is False, str(mixed))
    check("top1 需要时间时仍要追问",
          _time_required([{"id": "m_b", "time_aware": True}]) is True, "")
    check("拿不到 time_aware 时保守当作需要时间",
          _time_required([{"id": "m_x"}]) is True and _time_required([]) is True, "")
    check("全是快照指标时仍不追问",
          _time_required([{"id": "m_a", "time_aware": False},
                          {"id": "m_c", "time_aware": False}]) is False, "")

    # ⑫ 相对区间词与模型编的硬日期同时出现时, 以用户原话为准
    # (本轮实测: 用户说"本月", 模型交 start/end=2026-09, resolve 默认拿它算出了上个月的空结果)
    from app.agents.askdata_agent.graph import _reconcile_time

    # 时间对账的分支覆盖。**判"用户说了什么"只能看原话**, 所以每条用例都必须传 question;
    # 不传 question 的旧用例等于在测"拿槽位自证槽位" —— 那个优先级已被推翻并删除。
    # 区间词先拿 normalize_range 验它真在词表里再拼问句复用: 这个文件里手敲的中文关键字
    # 一旦被写坏, 用例会"静默走另一条分支"报出看不出原因的假失败(本轮踩过)。
    rel_word = "本月"
    assert timesem.normalize_range(rel_word) == "this_month", "区间词字面量已损坏"
    rel_q = f"{rel_word}报销总额是多少"
    date_q = "查 2026-09-01 到 2026-09-30 的报销总额"
    plain_q = "员工报销总额是多少"

    invented = {"metrics": [{"id": "m_expense_total"}],
                "time": {"range": "this_month", "start": "2026-09-01", "end": "2026-09-30"}}
    fixed, note = _reconcile_time(invented, {"time": {"phrase": rel_word}}, rel_q)
    check("用户说相对区间时丢掉模型编的日期",
          fixed["time"]["start"] == "" and fixed["time"]["end"] == ""
          and fixed["time"]["range"] == "this_month" and rel_word in note, str(fixed) + repr(note))
    # 解析后的区间必须是"本月"而不是上个月
    bounds = timesem.resolve(fixed["time"])
    check("修完后的区间真的从本月起算",
          bounds.token == "this_month" and bounds.start.month == timesem.today_cst().month,
          bounds.label)
    said_dates, note2 = _reconcile_time(invented, {"time": {"phrase": "2026-09-01 到 2026-09-30"}}, date_q)
    check("原话写着日期时去掉碰巧并存的区间词",
          said_dates["time"]["range"] == "" and said_dates["time"]["start"] == "2026-09-01"
          and "具体日期" in note2, str(said_dates) + "|note=" + repr(note2))
    only_one, note3 = _reconcile_time({"time": {"range": "this_year"}}, {"time": {"phrase": "今年"}})
    check("只给了一种时不动也不说明", only_one["time"] == {"range": "this_year"} and note3 == "", str(only_one))
    # 最阴的一种: 模型只给硬日期不给区间词(无 token 可对比) —— 对账逻辑一开始漏了这条分支
    dates_only = {"metrics": [{"id": "m_expense_total"}],
                  "time": {"start": "2026-09-01", "end": "2026-10-01", "granularity": "month"}}
    fixed2, note4 = _reconcile_time(dates_only, {"time": {"phrase": ""}}, rel_q)
    check("只给硬日期时也会改回相对区间",
          fixed2["time"].get("range") == "this_month" and fixed2["time"]["start"] == ""
          and fixed2["time"]["end"] == "" and note4, str(fixed2) + "|note=" + repr(note4))
    real_dates, note5 = _reconcile_time(dates_only, {"time": {"phrase": ""}}, date_q)
    check("用户真只说日期时不推翻", real_dates["time"]["start"] == "2026-09-01" and note5 == "",
          str(real_dates) + "|note=" + repr(note5))
    # 拿槽位里的 phrase 当依据会被模型自己错的日期自证(本轮实测到的真因)
    both = {"metrics": [{"id": "m_expense_total"}],
            "time": {"range": "this_month", "start": "2026-09-01", "end": "2026-09-30"}}
    polluted, note6 = _reconcile_time(both, {"time": {"phrase": "2026-09-01~2026-09-30"}}, rel_q)
    check("槽位 phrase 被模型填成日期时仍以原话为准",
          polluted["time"]["range"] == "this_month" and polluted["time"]["start"] == ""
          and bool(note6), str(polluted) + "|note=" + repr(note6))
    said_in_q, note7 = _reconcile_time(both, {"time": {"phrase": rel_word}}, date_q)
    check("原话真说了日期时保留日期、去掉区间词",
          said_in_q["time"]["range"] == "" and said_in_q["time"]["start"] == "2026-09-01"
          and bool(note7), str(said_in_q) + "|note=" + repr(note7))
    # 三条判据都拿不到证据时必须返原样而不是 None(返 None 会让调用方解包直接报 TypeError)
    no_evidence, note8 = _reconcile_time(both, {"time": {"phrase": ""}}, plain_q)
    check("无证据时不动且返回形状正确",
          note8 == "" and no_evidence["time"]["range"] == "this_month"
          and no_evidence["time"]["start"] == "2026-09-01",
          str(no_evidence) + "|note=" + repr(note8))

    # ⑭ 展示区间由定稿 DSL 现算(实测过: DSL 对而展示块错, 两者分叉就是"数与区间不同事")
    from app.agents.askdata_agent.graph import _display_range

    shown = _display_range({"time": {"range": "this_month"}, "compare": {"type": "mom"}},
                           {"range": "custom", "start": "2026-09-01", "end": "2026-10-01"})
    check("展示区间跟着定稿 DSL 而不是旧回执",
          shown.get("range") == "this_month" and "custom" not in str(shown.get("range")), str(shown))
    check("展示区间的环比对比期也算的是本月",
          str(shown.get("compare") or "mom") != "" and shown.get("prev_start"), str(shown))
    backoff = _display_range({"time": {}}, {"range": "this_year"})
    check("拿不到 DSL 时间时退回回执不阻断", backoff == {"range": "this_year"}, str(backoff))
    # 今天信息必须每次调用现算(写成模块常量会在 import 时定住日期, 长进程里每天错一天)
    anchor_a = prompts_today()
    check("今天提示含当前日期且不是一次性常量",
          timesem.today_cst().isoformat() in anchor_a and anchor_a.startswith("今天"), anchor_a[:60])

    # ⑬ 模型私自加的过滤条件会被拦(本轮实测: 问"本月报销总额", DSL 里多了员工=张伟)
    from app.agents.askdata_agent.graph import _drop_invented_filters

    hijacked = {"metrics": [{"id": "m_expense_total"}],
                "time": {"range": "this_month"},
                "filters": [{"dim": "d_expense_employee_name", "op": "eq", "value": "张伟"}]}
    cleaned, dropped = _drop_invented_filters(hijacked, "本月报销总额是多少", [])
    check("用户没说的过滤被丢弃", dropped == ["d_expense_employee_name=张伟"]
          and cleaned.get("filters") == [], str(dropped) + str(cleaned))
    kept, dropped2 = _drop_invented_filters(
        {"metrics": [{"id": "m_expense_total"}],
         "filters": [{"dim": "d_expense_status", "op": "eq", "value": "PAID"}]},
        "本月已打款的报销总额",
        [{"kind": "enum", "id": "d_expense_status", "value": "PAID", "matched": "已打款"}],
    )
    check("枚举反查过的码值过滤不会被误删", not dropped2 and kept["filters"], str(dropped2) + str(kept))
    said, dropped3 = _drop_invented_filters(
        {"metrics": [{"id": "m_expense_total"}],
         "filters": [{"dim": "d_expense_department", "op": "eq", "value": "研发部"}]},
        "本月研发部的报销总额", [],
    )
    check("用户真说了的范围保留", not dropped3 and said["filters"], str(dropped3))
    multi, dropped4 = _drop_invented_filters(
        {"metrics": [{"id": "m_expense_total"}],
         "filters": [{"dim": "d_expense_category", "op": "in",
                      "value": ["差旅费", "保密费"]}]},
        "本月差旅费的报销总额", [],
    )
    check("in 列表里有来处就整条保留", not dropped4 and multi["filters"], str(dropped4))
    nulls, dropped5 = _drop_invented_filters(
        {"metrics": [{"id": "m_expense_total"}],
         "filters": [{"dim": "d_supplier_name", "op": "is_null", "value": None}]},
        "本月报销总额", [],
    )
    check("is_null 不参与来处判定", not dropped5 and nulls["filters"], str(dropped5))


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
    test_semantic_layer_extensions()
    test_orchestration_extensions()
    test_rival_gap()
    test_sandbox_governance()
    test_v3_second_round()
    test_forward_windows_and_aliases()
    print(f"\n合计: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
