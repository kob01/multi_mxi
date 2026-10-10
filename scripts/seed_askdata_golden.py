"""智能问数 golden 评测集灌入(自建基准, 不用公开集)。

为什么必须自建并自己跑一遍取期望结果: 公开基准(BIRD/Spider)的标注噪声很大(其错误率
本身被报告到五成以上), 拿它做回归等于"跟着噪声调 prompt"。本脚本以语义层登记表为真值源:
每条用例的期望结果 = **服务端编译出的 SQL 在库里真跑一遍**得到的行集, 于是 EX 比的是
"引擎给的数变了没", 而不是"某个人当年手写的一条 SQL"。

三条约束:
1. 期望结果存**行集**而不是存 SQL 文本(文本等价而结果不同也算错, 反过来同理);
2. 灌入用"全员作用域"(与经营口径一致); 换调用者时数字会因 RLS 不同 —— 这条写进 note,
   评测跑 EX 时也按同一作用域比(见 scripts/askdata_eval), 否则比的是权限不是准确率;
3. 写库前先过校验器 + 编译器, 编不出来的用例跳过并报告 —— 绝不写"假标准答案"。

用法::

    uv run python -m scripts.seed_askdata_golden             # 只补缺失用例
    uv run python -m scripts.seed_askdata_golden --refresh    # 重算全部期望结果(数据/口径变了)
    uv run python -m scripts.seed_askdata_golden --dry-run     # 只跑编译不写库
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from typing import Any

from app.db import sync as dbsync
from app.db.rls import read_role
from app.db.scope import DataScope, resolve_scope_sync
from app.semantic import compiler
from app.semantic.catalog import MetricCatalog
from app.semantic.dsl import parse_dsl
from app.semantic.registry import get_catalog
from app.semantic.validate import CallerScope, validate

# 灌入与评测都走同一个入口取作用域: 自己拼部门列表会与 resolve_scope_sync 算出的不一致,
# 于是 EX 比的其实是权限而不是准确率(最难查的一类假阴性)。
EVAL_USER = "eval-runner"
EVAL_ROLE = "admin"


def golden_cases(catalog: MetricCatalog) -> list[dict[str, Any]]:
    """评测用例集: 每个已登记指标一条单值问法 + 一批多维/同环比/澄清型问法。

    用例数量随语义层增长(52 个指标 → 至少 52 条), 目标 500 条靠后续把真实业务问法
    (点踩/改写草案)补进来, 而不是凭空编更多同义句 —— 后者只会把分数刷高。
    """
    cases: list[dict[str, Any]] = []
    for metric in catalog.active_metrics:
        dims = list(metric.compatible_dims or [])
        cases.append({
            "question": f"{_today_word()}的{metric.label}是多少",
            "dsl": {"metrics": [{"id": metric.metric_id}], "time": {"range": "this_year"}},
            "tags": "单指标",
        })
        if dims:
            dim_label = _dim_label(catalog, dims[0]) or "相关维度"
            cases.append({
                # 问法用维度自己的名字(不写死"各部门"): 合同类指标首个可下钻维度是
                # "合同类别", 问成"各部门"就是一条自相矛盾的用例。
                "question": f"按{dim_label}看今年的{metric.label}",
                "dsl": {"metrics": [{"id": metric.metric_id}], "dims": [dims[0]],
                        "time": {"range": "this_year"}},
                "tags": "下钻",
            })
    cases.extend(_EXTRA_CASES)
    return cases


# 多维/同环比/过滤/粒度/澄清型问法: 这些才是准确率真正会掉的地方
# (单指标问法几乎不会错, 全用它评测等于给自己打高分)。
_EXTRA_CASES: list[dict[str, Any]] = [
    {"question": "各部门本月报销总额，并做环比",
     "dsl": {"metrics": [{"id": "m_expense_total"}], "dims": ["d_expense_department"],
             "time": {"range": "this_month"}, "compare": {"type": "mom"}},
     "tags": "环比,下钻"},
    {"question": "今年报销总额同比去年怎么样",
     "dsl": {"metrics": [{"id": "m_expense_total"}], "time": {"range": "this_year"},
             "compare": {"type": "yoy"}},
     "tags": "同比"},
    {"question": "研发部今年的差旅费有多少",
     "dsl": {"metrics": [{"id": "m_expense_travel_total"}],
             "filters": [{"dim": "d_expense_department", "op": "eq", "value": "研发部"}],
             "time": {"range": "this_year"}},
     "tags": "过滤"},
    {"question": "按月份看今年各部门的报销单数趋势",
     "dsl": {"metrics": [{"id": "m_expense_count"}], "dims": ["d_expense_department"],
             "time": {"range": "this_year", "granularity": "month"}},
     "tags": "下钻,粒度"},
    {"question": "已批准和已打款的采购金额分别是多少", "dsl": None, "expect_refuse": True,
     "tags": "多指标带各自行集,应被拒"},
    {"question": "预算消耗率最高的部门",
     "dsl": {"metrics": [{"id": "m_budget_used_pct"}], "dims": ["d_budget_department"],
             "time": {"range": "this_year"}, "order_by": "m_budget_used_pct desc", "limit": 5},
     "tags": "排序,topN,快照"},
    {"question": "历史上高风险合同有几份",
     "dsl": {"metrics": [{"id": "m_contract_high_risk_count"}], "time": {"range": "all_time"}},
     "tags": "全部历史"},
    {"question": "按到期日看未来三十天内要续签的合同数",
     "dsl": {"metrics": [{"id": "m_contract_expiring_count"}],
             "time": {"start": "2026-10-01", "end": "2026-11-30"}},
     "tags": "显式区间"},
    {"question": "本季度按部门看未结工单数",
     "dsl": {"metrics": [{"id": "m_ticket_open_count"}], "dims": ["d_ticket_department"],
             "time": {"range": "this_quarter"}},
     "tags": "下钻"},
    {"question": "今年比价不足三单的采购有几个",
     "dsl": {"metrics": [{"id": "m_purchase_quotes_short"}], "time": {"range": "this_year"}},
     "tags": "合规"},
    {"question": "黑名单供应商有几家",
     "dsl": [{"id": "m_supplier_blacklist_count"}], "expect_refuse": True,
     "tags": "错误形状,应被拒"},
]


def _dim_label(catalog: MetricCatalog, dim_id: str) -> str:
    dim = catalog.get_dim(dim_id)
    return dim.label if dim else ""


def _case_id(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:24]


def _today_word() -> str:
    return "今年"


def _scope() -> DataScope:
    """评测灌入用的全员作用域(与经营口径一致; note 里会写明, 比对时同口径)。

    走 :func:`resolve_scope_sync` 而不是手拼部门集: 它才是取数时真正用的那一份。
    """
    return resolve_scope_sync(EVAL_USER, EVAL_ROLE)


def _expected(compiled: compiler.Compiled, scope: DataScope) -> dict[str, Any]:
    settings = _settings()
    payload, _scan = dbsync.execute_scoped_select(
        compiled.sql, compiled.params, db_role=read_role(),
        session_settings=scope.session_settings(),
        max_scan_rows=settings.sqlguard_max_scan_rows,
    )
    block = payload[0] if payload else {"columns": [], "rows": [], "rowcount": 0}
    return {
        "columns": block.get("columns") or [],
        "rows": (block.get("rows") or [])[:20],
        "rowcount": int(block.get("rowcount") or 0),
    }


def _settings():
    from app.config import get_settings

    return get_settings()


def _assert_refused(dsl: Any, catalog: MetricCatalog, caller: CallerScope) -> str:
    """反向用例: 返回拦下它的理由, 没拦住则返空串(调用方计作失守)。"""
    try:
        spec = parse_dsl(dsl)
    except Exception as exc:  # noqa: BLE001 - 语法级拦住就算通过
        return f"DSL 语法拒: {type(exc).__name__}"
    check = validate(spec, catalog=catalog, scope=caller, today=None,
                     max_rows=_settings().askdata_max_rows,
                     max_time_span_days=_settings().askdata_max_time_span_days)
    if not check.ok:
        return f"校验器拒: {check.as_text()}"
    try:
        compiler.compile_analytical(spec, catalog=catalog, scope=_scope(), check=check)
    except compiler.CompileError as exc:
        return f"编译器拒: {exc}"
    return ""


def _prune_seeded() -> int:
    """删掉本脚本以前灌过的用例(按 created_by 认, 人工补录的不碰)。

    为什么需要: 用例主键是问题文本的哈希, 改了问法(如把"各部门的X"改成"按类别看X")
    会新增一行而旧行还在, 于是评测集里同时留着两种问法的重复项 —— 分数会被拉得看不出门道。
    """
    from sqlalchemy import text

    from app.db import sync as dbsync

    with dbsync.get_sync_engine().begin() as conn:
        result = conn.execute(
            text("DELETE FROM askdata_golden WHERE created_by = :who"),
            {"who": "seed_askdata_golden"},
        )
        return int(result.rowcount or 0)


def build_rows(*, refresh: bool, dry_run: bool) -> int:
    catalog = get_catalog()
    scope = _scope()
    caller = CallerScope.from_scope(scope)
    from app.askdata import store

    if refresh and not dry_run:
        print(f"清理旧灌入用例: {_prune_seeded()} 条(人工补录的不动)")
    existing = {c["question"] for c in store.list_golden(enabled_only=False, limit=2000)}
    written = skipped = failed = 0
    for case in golden_cases(catalog):
        question = case["question"]
        dsl = case.get("dsl")
        if not refresh and question in existing:
            skipped += 1
            continue
        if dsl is None or case.get("expect_refuse"):
            # 反向用例: 只验"它确实被拦住", 不存标准答案(存了就等于鼓励"拿错 DSL 也有分")。
            refused = _assert_refused(dsl, catalog, caller)
            if refused:
                skipped += 1
                print(f"  [反向用例通过] {question}: {refused}")
            else:
                failed += 1
                print(f"  [反向用例失守] {question}: 本该被拦却编译通过了")
            continue
        try:
            spec = parse_dsl(dsl)
            check = validate(spec, catalog=catalog, scope=caller, today=None,
                             max_rows=_settings().askdata_max_rows,
                             max_time_span_days=_settings().askdata_max_time_span_days)
            if not check.ok:
                raise RuntimeError(check.as_text())
            compiled = compiler.compile_analytical(spec, catalog=catalog, scope=scope, check=check,
                                                   max_rows=_settings().askdata_max_rows)
            if dry_run:
                print(f"  [dry] {question} -> {compiled.route} ok, columns={compiled.columns}")
                written += 1
                continue
            expected = _expected(compiled, scope)
        except Exception as exc:  # noqa: BLE001 - 单条编不出/跑不出来只跳过这一条
            failed += 1
            print(f"  [失败] {question}: {type(exc).__name__}: {exc}")
            continue
        out = store.upsert_golden(
            question=question, dsl=dsl, expected_sql=compiled.sql,
            expected_result=expected, tags=case.get("tags", ""),
            note=f"route={compiled.route}; 作用域=全员(比 EX 时须同作用域); "
                 f"caliber={' | '.join(compiled.caliber_notes)[:300]}",
            created_by="seed_askdata_golden", enabled=True, case_id=_case_id(question),
        )
        if out.get("error"):
            failed += 1
            print(f"  [写库失败] {question}: {out['error']}")
        else:
            written += 1
    print(f"\n合计: 写入/更新 {written}, 跳过 {skipped}, 失败 {failed}; "
          f"启用中用例 {store.golden_count()} 条")
    print("提示: 公开基准噪声大, 回归只认这套自建 golden 的 EX; 人工抽查过 note 里的口径再用于放行。")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="智能问数 golden 评测集灌入")
    parser.add_argument("--refresh", action="store_true", help="重算全部用例的期望结果")
    parser.add_argument("--dry-run", action="store_true", help="只跑校验与编译, 不写库")
    args = parser.parse_args(argv)
    return build_rows(refresh=bool(args.refresh), dry_run=bool(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
