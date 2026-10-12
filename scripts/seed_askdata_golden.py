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
    """评测用例集: 每个已登记指标沿 intent × 难度 分层展开多条问法。

    为什么是"按形状笛卡尔展开"而不是"多编几个同义句": 同义句只能刷文本多样性,
    分数会虚高; 而"单值/下钻/环比/同比/topN/趋势/过滤/多指标"这些**结构差异**才是
    准确率真正会掉的地方, 并且它们的 DSL 形状能由同一个源(语义层登记表)推出来 ——
    不需要人拍脑袋标, 也不会标出"库里根本算不出来"的假标准答案(跑不通就跳过)。

    每条都带 ``slots``(期望槽位说法)与 ``intent``/``difficulty``: slot-F1 需要前者,
    报告的分层视图需要后者。
    """
    cases: list[dict[str, Any]] = []
    for metric in catalog.active_metrics:
        cases.extend(_metric_cases(catalog, metric))
    cases.extend(_pair_cases(catalog))
    cases.extend(_with_labels(_EXTRA_CASES))
    return cases


# 手工补录的几条反向/边角用例: intent 与难度从 tags 推(避免两处手写不一致)。
_TAG_TO_INTENT = (("环比", "compare"), ("同比", "compare"), ("topN", "topn"),
                 ("趋势", "trend"), ("多指标", "composition"))


def _with_labels(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for case in cases:
        tags = str(case.get("tags") or "")
        intent = case.get("intent") or next(
            (value for keyword, value in _TAG_TO_INTENT if keyword in tags), "fetch"
        )
        difficulty = case.get("difficulty") or ("hard" if "应被拒" in tags
                                               or "多指标" in tags or "粒度" in tags else "medium")
        out.append({**case, "intent": intent, "difficulty": difficulty})
    return out


def _metric_cases(catalog: MetricCatalog, metric: Any) -> list[dict[str, Any]]:
    """一个指标能展开的所有问法(不适用的形状直接不出现在结果里, 不写成失败用例)。"""
    mid = metric.metric_id
    label = metric.label
    dims = [d for d in (metric.compatible_dims or []) if catalog.get_dim(d)]
    out: list[dict[str, Any]] = []

    def _slot(metric_label: str, *, time_word: str = "", dim_labels: list[str] | None = None,
              filter_words: list[str] | None = None, compare: str = "") -> dict[str, Any]:
        """期望槽位: 只写"会出现在问法原文里"的说法(状态机会剔除不在原话里的词)。"""
        slots: dict[str, Any] = {"metric_terms": [metric_label]}
        if dim_labels:
            slots["dim_terms"] = list(dim_labels)
        if filter_words:
            slots["filter_terms"] = [{"phrase": w} for w in filter_words]
        if time_word:
            slots["time"] = {"phrase": time_word}
        if compare:
            slots["compare"] = {"type": compare}
        return slots

    # --- easy: 单值 + 时间词形(今年/本月)两种 ---
    for word, token in (("今年", "this_year"), ("本月", "this_month")):
        out.append({
            "question": f"{word}的{label}是多少",
            "dsl": {"metrics": [{"id": mid}], "time": {"range": token}},
            "slots": _slot(label, time_word=word),
            "tags": "单指标", "intent": "fetch", "difficulty": "easy",
        })
    if not metric.has_time:
        # 快照型指标(年度预算/在职人数)额外给一条不带时间的: 它本来就不该被追问时间。
        out.append({
            "question": f"{label}是多少",
            "dsl": {"metrics": [{"id": mid}]},
            "slots": _slot(label),
            "tags": "单指标,快照", "intent": "fetch", "difficulty": "easy",
        })
    # --- medium: 下钻(前两个可下钻维度各一条) ---
    for dim_id in dims[:2]:
        dim_label = _dim_label(catalog, dim_id)
        out.append({
            "question": f"按{dim_label}看本年的{label}",
            "dsl": {"metrics": [{"id": mid}], "dims": [dim_id], "time": {"range": "this_year"}},
            "slots": _slot(label, time_word="本年", dim_labels=[dim_label]),
            "tags": "下钻", "intent": "fetch", "difficulty": "medium",
        })
    if metric.has_time:
        out.append({
            "question": f"本年{label}同比去年怎么样",
            "dsl": {"metrics": [{"id": mid}], "time": {"range": "this_year"},
                   "compare": {"type": "yoy"}},
            "slots": _slot(label, time_word="本年", compare="yoy"),
            "tags": "同比", "intent": "compare", "difficulty": "medium",
        })
        out.append({
            "question": f"本月{label}环比上月变化",
            "dsl": {"metrics": [{"id": mid}], "time": {"range": "this_month"},
                   "compare": {"type": "mom"}},
            "slots": _slot(label, time_word="本月", compare="mom"),
            "tags": "环比", "intent": "compare", "difficulty": "medium",
        })
        out.append({
            "question": f"本年逐月看{label}趋势",
            "dsl": {"metrics": [{"id": mid}], "time": {"range": "this_year", "granularity": "month"}},
            "slots": _slot(label, time_word="本年"),
            "tags": "趋势,粒度", "intent": "trend", "difficulty": "medium",
        })
    # --- hard: topN / 枚举过滤 ---
    if dims and metric.has_time:
        dim_id = dims[0]
        dim_label = _dim_label(catalog, dim_id)
        out_name = metric.output_alias
        out.append({
            "question": f"本年{label}最高的 5 个{dim_label}",
            "dsl": {"metrics": [{"id": mid}], "dims": [dim_id], "time": {"range": "this_year"},
                   "order_by": f"{out_name} desc", "limit": 5},
            "slots": _slot(label, time_word="本年", dim_labels=[dim_label]),
            "tags": "topN,排序", "intent": "topn", "difficulty": "hard",
        })
    enum_dim_id, enum_value = _first_enum_dim(catalog, dims)
    if enum_value:
        out.append({
            # 枚举值当**过滤条件**而不是下钻维度: 问法"本年研发部的X"要的是标量, 编成
            # dims 就得出一张表 —— 这正好是 DSL 生成最容易错的一类形状。
            "question": f"本年{enum_value}的{label}是多少",
            "dsl": {"metrics": [{"id": mid}],
                   "filters": [{"dim": enum_dim_id, "op": "eq", "value": enum_value}],
                   "time": {"range": "this_year"}},
            "slots": _slot(label, time_word="本年", filter_words=[enum_value]),
            "tags": "枚举反查,过滤", "intent": "fetch", "difficulty": "hard",
        })
    if metric.has_time:
        # 两个结构差异叠在一起才是真难题(单一形状几乎不会错):
        # 环比 + 下钻(对比期要按同一个维度对齐), 与 过滤 + 下钻(枚举值与分组共存)。
        if dims:
            dim_id = dims[0]
            dim_label = _dim_label(catalog, dim_id)
            out.append({
                "question": f"按{dim_label}看本月{label}环比变化",
                "dsl": {"metrics": [{"id": mid}], "dims": [dim_id],
                       "time": {"range": "this_month"}, "compare": {"type": "mom"}},
                "slots": _slot(label, time_word="本月", dim_labels=[dim_label], compare="mom"),
                "tags": "环比,下钻", "intent": "compare", "difficulty": "hard",
            })
            if len(dims) > 1:
                second = dims[1]
                out.append({
                    "question": f"本年逐月按{_dim_label(catalog, second)}看{label}趋势",
                    "dsl": {"metrics": [{"id": mid}], "dims": [second],
                           "time": {"range": "this_year", "granularity": "month"}},
                    "slots": _slot(label, time_word="本年", dim_labels=[_dim_label(catalog, second)]),
                    "tags": "趋势,粒度,下钻", "intent": "trend", "difficulty": "hard",
                })
        if enum_value:
            out.append({
                "question": f"按{_dim_label(catalog, dims[0])}看本年{enum_value}的{label}",
                "dsl": {"metrics": [{"id": mid}], "dims": [dims[0]],
                       "filters": [{"dim": enum_dim_id, "op": "eq", "value": enum_value}],
                       "time": {"range": "this_year"}},
                "slots": _slot(label, time_word="本年", dim_labels=[_dim_label(catalog, dims[0])],
                               filter_words=[enum_value]),
                "tags": "过滤,下钻", "intent": "fetch", "difficulty": "hard",
            })
    return out


def _first_enum_dim(catalog: MetricCatalog, dims: list[str]) -> tuple[str, str]:
    """第一个有静态枚举取值的可下钻维度与其首个取值(没有就返空, 不造假过滤)。"""
    for dim_id in dims:
        dim = catalog.get_dim(dim_id)
        values = list(dim.enum_values or []) if dim else []
        if values:
            return dim_id, str(values[0])
    return "", ""


def _pair_cases(catalog: MetricCatalog) -> list[dict[str, Any]]:
    """同一事实表上两个无各自行集限定的指标(占比/多指标同屏形状)。"""
    out: list[dict[str, Any]] = []
    by_table: dict[str, list[Any]] = {}
    for metric in catalog.active_metrics:
        if metric.caliber_where or not metric.compatible_dims:
            continue  # 带自己行集口径的两个指标会被 E_CALIBER_CONFLICT 拦下(那是反向用例)
        by_table.setdefault(metric.source_table, []).append(metric)
    for metrics in by_table.values():
        for first, second in zip(metrics, metrics[1:]):
            out.append({
                "question": f"本年{first.label}和{second.label}按{ _dim_label(catalog, first.compatible_dims[0])}看",
                "dsl": {"metrics": [{"id": first.metric_id}, {"id": second.metric_id}],
                       "dims": [first.compatible_dims[0]], "time": {"range": "this_year"}},
                "slots": {"metric_terms": [first.label, second.label], "time": {"phrase": "本年"},
                          "dim_terms": [_dim_label(catalog, first.compatible_dims[0])]},
                "tags": "多指标", "intent": "composition", "difficulty": "hard",
            })
            if len(out) >= 60:
                return out
            break  # 每张表只取一对: 同形状的十条不会多告诉我们什么
    return out


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
    return dim.label if dim else dim_id


def _case_id(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:24]


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
            slots=case.get("slots") or {}, difficulty=case.get("difficulty") or "easy",
            intent=case.get("intent") or "",
            # 记下标准答案是按哪一版口径算的: 语义缓存只复用同版的用例
            semver=compiled.semver,
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
    minimum = int(_settings().askdata_golden_min_cases or 0)
    have = store.golden_count()
    if minimum and have < minimum:
        print(f"警告: 用例 {have} 条低于下限 {minimum} 条 —— 分层结论不可信, "
              "继续靠 badcase 回流与真实问法补录(而不是堆同义句刷分)。")
    print("提示: 公开基准噪声大, 回归只认这套自建 golden 的 EX; 人工抽查过 note 里的口径再用于放行。")
    print("下一步: 跑 python -m scripts.embed_askdata_golden 回填问法向量(语义缓存与 few-shot 都靠它)。")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="智能问数 golden 评测集灌入")
    parser.add_argument("--refresh", action="store_true", help="重算全部用例的期望结果")
    parser.add_argument("--dry-run", action="store_true", help="只跑校验与编译, 不写库")
    args = parser.parse_args(argv)
    return build_rows(refresh=bool(args.refresh), dry_run=bool(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
