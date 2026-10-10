"""层 4(只读对偶): DSL -> SQL 编译器(确定性, 无 LLM)。

设计姿态与写通道(:mod:`app.db.dataops`)一致: **SQL 文本由服务端模板产出**, 模型给的
只有语义对象 id 与业务条件值。于是三件事在结构上不成立:

1. 标识符注入 —— 表名/列名/聚合表达式全部从语义层登记表取出, 且在拼进文本前用
   sqlglot 把裸列限定成事实表别名(``amount`` -> ``r.amount``), 多表 JOIN 不会 ambiguous;
2. 值注入 —— 过滤值与时间窗一律走 ``:name`` 绑参(占位符名由本模块编号, 不接受外部传入);
3. 越权 —— 作用域谓词用 :meth:`app.db.scope.DataScope.scope_clause` 注入, 与 RLS 双保险。

两条出口对应降级链的前两级:
- :func:`compile_metric_api` —— 口径确定性首选: 除时间窗外**没有任何来自模型的谓词**;
- :func:`compile_analytical` —— 通用分析: 多维下钻 + 过滤 + 同环比(CTE 双窗口)。

产出的文本必须再过一次 :func:`app.db.ast_guard.validate_sql`(``normalize=False``,
因为 sqlglot 会把 ``:name`` 重写成驱动才认的形式, 而这里执行的是服务端自己写的模板),
不通过就抛 :class:`CompileError` —— 绝不外发未校验文本。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from app.semantic.catalog import Dimension, Metric, MetricCatalog
from app.semantic.dsl import QuerySpec
from app.semantic.seed import SOFT_DELETE_TABLES, TABLE_ALIAS
from app.semantic.timesem import ResolvedRange
from app.semantic.validate import ValidationResult, validate

logger = logging.getLogger(__name__)

# 事实表上的时间列只允许这一 kinds(其他列当时间用会静默算错)。
_TIME_BUCKET: dict[str, tuple[str, str]] = {
    "day": ("to_char(date_trunc('day', {col}), 'YYYY-MM-DD')", "按天"),
    "week": ("to_char(date_trunc('week', {col}), 'IYYY-IW')", "按 ISO 周"),
    "month": ("to_char(date_trunc('month', {col}), 'YYYY-MM')", "按月"),
    "quarter": ("to_char(date_trunc('quarter', {col}), 'YYYY-MM')", "按季度(取季首月)"),
    "year": ("to_char(date_trunc('year', {col}), 'YYYY')", "按年"),
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_MAX_JOIN_DEPTH = 3  # 语义层目前最多"事实表 + 员工主数据"一跳, 留一点余量


class CompileError(ValueError):
    """DSL 编不成可执行 SQL(带原因, 上层据此决定降级还是拒答)。"""


@dataclass
class Compiled:
    """一次编译的完整产物。"""

    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    route: str = "compiler"
    columns: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    caliber_notes: list[str] = field(default_factory=list)
    limit: int = 100

    def to_payload(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "sql": self.sql,
            "columns": self.columns,
            "labels": self.labels,
            "caliber_notes": self.caliber_notes,
            "limit": self.limit,
        }


def _ident(name: str, *, where: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise CompileError(f"{where} 不是合法标识符: {name!r}")
    return name


def _qualify(text: str, alias: str, *, where: str) -> str:
    """把片段里未限定表名的列换成事实表别名(确定性重写, 不改变语义)。"""
    try:
        node = parse_one(text, read="postgres")
    except ParseError as exc:
        raise CompileError(f"{where} 无法解析: {text!r} ({exc})") from exc
    if node is None:
        raise CompileError(f"{where} 解析为空: {text!r}")
    changed = False
    for column in node.find_all(exp.Column):
        if not column.table:
            column.set("table", exp.to_identifier(alias))
            changed = True
    if not changed:
        return text
    rendered = node.sql(dialect="postgres")
    if not rendered.strip():
        raise CompileError(f"{where} 规范化后为空")
    return rendered


def _fact_alias(table: str) -> str:
    alias = TABLE_ALIAS.get(table)
    if not alias:
        raise CompileError(f"表 {table} 没有登记别名, 无法安全拼接列限定")
    return _ident(alias, where="表别名")


def _metric_expr(metric: Metric, alias: str, *, grouped: bool = False) -> str:
    """取指标的聚合片段(指标级 agg 覆盖只允许换聚合函数名, 不换列)。

    ``grouped=True`` 时额外查一道"表达式里到底有没有聚合函数": 没有的话 PG 会直接报
    "column must appear in the GROUP BY clause" —— 那是一条用户看不懂、归因也归不到口径
    上的错。在此拦下可以给出"这个指标登记成了行级表达式"这种能行动的原因。
    """
    expression = (metric.expression or "").strip()
    if not expression:
        raise CompileError(f"指标 {metric.metric_id} 没有登记聚合表达式")
    qualified = _qualify(expression, alias, where=f"指标 {metric.metric_id} 的表达式")
    if grouped and not _has_aggregate(qualified):
        raise CompileError(
            f"指标 {metric.label}({metric.metric_id}) 的口径是行级表达式, 无法在维度下钻时聚合; "
            "需要先在语义层把它登记成聚合形态"
        )
    return qualified


def _has_aggregate(sql_fragment: str) -> bool:
    """片段里是否存在聚合函数(COUNT/SUM/AVG/MAX/MIN/string_agg 等)。"""
    try:
        node = parse_one(sql_fragment, read="postgres")
    except ParseError:
        return False
    return node is not None and next(node.find_all(exp.AggFunc), None) is not None


def _dim_alias(dim: Dimension, used: set[str]) -> str:
    """维度输出列名: 用真实列名, 撞名时追加序号(同一查询里不会有两个同名输出列)。"""
    base = _ident(dim.column, where=f"维度 {dim.dim_id} 的列名")
    if base in used:
        i = 2
        while f"{base}_{i}" in used:
            i += 1
        base = f"{base}_{i}"
    used.add(base)
    return base


def _where_parts(
    *,
    metric: Metric,
    alias: str,
    scope: Any,
    resolved: ResolvedRange,
    window_suffix: str = "",
) -> tuple[list[str], dict[str, Any]]:
    """一份 WHERE 条件(作用域谓词 + 软删 + 时间窗 + 指标口径)。

    ``window_suffix`` 让同环比的两个 CTE 用不同的绑参名而不共用值: 参数名由本模块
    编号生成, 永不来自模型输出。
    """
    parts: list[str] = []
    params: dict[str, Any] = {}
    if scope is not None:
        parts.append(scope.scope_clause(alias))
        params.update(scope.scope_params())
    else:
        raise CompileError("缺少数据作用域, 拒绝编译无隔离的查询")
    if metric.source_table in SOFT_DELETE_TABLES:
        parts.append(f"{alias}.is_deleted = false")
    if not resolved.whole and metric.has_time:
        start_key, end_key = f"p_start{window_suffix}", f"p_end{window_suffix}"
        col = _qualify(metric.time_column, alias, where="时间列")
        parts.append(f"{col} >= :{start_key}")
        parts.append(f"{col} < :{end_key}")
        params[start_key] = resolved.start.isoformat() if resolved.start else ""
        params[end_key] = resolved.end.isoformat() if resolved.end else ""
    if metric.caliber_where:
        parts.append(f"({_qualify(metric.caliber_where, alias, where='指标口径谓词')})")
    return parts, params


def _render_filters(spec: QuerySpec, dims: dict[str, Dimension], alias: str) -> tuple[list[str], dict[str, Any]]:
    """把过滤条件渲染成谓词 + 绑参(谓词模板与写通道同源: 复用 datadsl 的 ``_OP_TEMPLATES``)。

    列位上放的是**本模块限定好的列**(``r.category``), 而不是模型给的 dim_id ——
    所以谓词里没有任何一个字来自模型。
    """
    from app.db.datadsl import FilterOp, predicate_sql

    parts: list[str] = []
    params: dict[str, Any] = {}
    for index, filt in enumerate(spec.filters):
        dim = dims.get(filt.dim)
        if dim is None:
            raise CompileError(f"过滤维度 {filt.dim} 未登记, 无法编译")
        column = _qualify(dim.qualified_column, alias, where=f"维度 {filt.dim} 的列")
        op = filt.op if isinstance(filt.op, FilterOp) else FilterOp(str(filt.op))
        fragment, names, values = predicate_sql(_DslFilter(field=column, op=op, value=filt.value), f"f{index}")
        parts.append(f"({fragment})")
        for name, value in zip(names, values):
            params[name] = value
    return parts, params


class _DslFilter:
    """predicate_sql 期望的形状(只用到 field/op/value 三个属性)。"""

    __slots__ = ("field", "op", "value")

    def __init__(self, *, field: str, op: Any, value: Any) -> None:
        self.field = field
        self.op = op
        self.value = value


def _collect_joins(dims: list[Dimension], alias: str) -> list[str]:
    joins: list[str] = []
    for dim in dims:
        if not dim.join_sql:
            continue
        text = dim.join_sql.replace("{alias}", alias)
        if text not in joins:
            joins.append(text)
    if len(joins) > _MAX_JOIN_DEPTH:
        raise CompileError(f"本次下钻需要 {len(joins)} 张关联表, 超过安全上限 {_MAX_JOIN_DEPTH}")
    return joins


def _resolve_columns(dims: list[Dimension], alias: str) -> list[tuple[Dimension, str]]:
    """维度 -> ``(维度, 限定列)``; 非事实表维度的列前缀由 dim 自己带(如 ``e.department``)。"""
    out: list[tuple[Dimension, str]] = []
    for dim in dims:
        column = dim.qualified_column
        if dim.column_prefix == alias and "." not in column:
            column = f"{alias}.{column}"
        out.append((dim, _qualify(column, alias, where=f"维度 {dim.dim_id} 的列")))
    return out


def compile_analytical(
    spec: QuerySpec,
    *,
    catalog: MetricCatalog,
    scope: Any,
    check: ValidationResult | None = None,
    allowed_tables: set[str] | None = None,
    max_rows: int = 100,
) -> Compiled:
    """通用编译: 多维下钻 + 模型给的过滤 + 同环比(两条窗口的 CTE)。"""
    check = check or validate(spec, catalog=catalog, scope=_caller_scope(scope))
    if not check.ok:
        raise CompileError(f"DSL 未通过校验: {check.as_text()}")
    if not check.metrics:
        raise CompileError("DSL 里没有可计算的指标")
    metric = check.metrics[0]
    alias = _fact_alias(metric.source_table)
    resolved = check.resolved
    if resolved is None:
        raise CompileError("缺少已解析的时间区间")

    used: set[str] = {"period"}
    select_parts: list[str] = []
    group_parts: list[str] = []
    columns: list[str] = []
    metric_outs: list[str] = []
    labels: dict[str, str] = {}
    notes: list[str] = []
    dim_alias: dict[str, str] = {}

    # 时间桶: 同环比时抑制(否则"上一周期"与当期不同桶, JOIN 永远配不上)。
    with_compare = (spec.compare.type if spec.compare else "none") != "none"
    bucket: str = ""
    if resolved.gran in _TIME_BUCKET and metric.has_time and not with_compare:
        template, note = _TIME_BUCKET[resolved.gran]
        bucket = template.format(
            col=_qualify(metric.time_column, alias, where="时间列")
        )
        select_parts.append(f"{bucket} AS period")
        group_parts.append(bucket)
        columns.append("period")
        labels["period"] = note
        notes.append(f"时间粒度: {note}")
    elif with_compare and resolved.gran in _TIME_BUCKET:
        notes.append("同环比按整段区间对比, 本次不展开时间粒度")

    dim_pairs = _resolve_columns(check.dims, alias)
    dim_alias: dict[str, str] = {}
    for dim, column in dim_pairs:
        out_name = _dim_alias(dim, used)
        dim_alias[dim.dim_id] = out_name
        select_parts.append(f"{column} AS {out_name}")
        group_parts.append(column)
        columns.append(out_name)
        labels[out_name] = dim.label

    for item, m in zip(spec.metrics, check.metrics):
        out_name = m.output_alias
        if out_name in used:
            out_name = f"{out_name}_{len(used)}"
        used.add(out_name)
        select_parts.append(f"{_metric_expr(m, alias, grouped=bool(group_parts))} AS {out_name}")
        columns.append(out_name)
        metric_outs.append(out_name)
        labels[out_name] = item.label or m.label
        notes.append(f"{m.label}: {m.caliber_note()}")

    joins = _collect_joins(check.dims, alias)
    # 过滤维度可能带进新的 JOIN(如只按部门过滤而不按部门下钻)。
    join_keys = {j.split(" ON ")[0] for j in joins}
    for dim in check.filter_dims.values():
        if not dim.join_sql:
            continue
        text = dim.join_sql.replace("{alias}", alias)
        if text.split(" ON ")[0] not in join_keys:
            joins.append(text)
            join_keys.add(text.split(" ON ")[0])

    where_parts, params = _where_parts(metric=metric, alias=alias, scope=scope, resolved=resolved)
    filter_parts, filter_params = _render_filters(spec, check.filter_dims, alias)
    params.update(filter_params)
    where_parts.extend(filter_parts)
    dim_cols = [dim_alias[d.dim_id] for d in check.dims]
    query = _Query(
        table=metric.source_table, alias=alias, select=select_parts,
        where=where_parts, group=group_parts, joins=joins,
    )

    if not with_compare:
        sql = query.render() + _order_and_limit(
            spec, columns=columns, default_order=metric_outs[0] if dim_cols else "", max_rows=max_rows,
        )
        return _finish(sql, params, spec, columns, labels, notes, max_rows, "compiler")

    if resolved.prev_start is None or resolved.prev_end is None:
        raise CompileError("对比期没算出来, 不能出同环比")
    # 同环比已抑制时间桶, 所以对比期查询与当期只差窗口绑参名(形状一份模板产出两次)。
    prev_where, prev_params = _where_parts(
        metric=metric, alias=alias, scope=scope,
        resolved=ResolvedRange(
            token=resolved.token, start=resolved.prev_start, end=resolved.prev_end,
            gran="none", label=resolved.label, whole=False,
        ),
        window_suffix="_prev",
    )
    prev_params.update(filter_params)
    if resolved.note:
        notes.append(f"对比期: {resolved.note}")
    prev_query = _Query(
        table=metric.source_table, alias=alias, select=list(select_parts),
        where=prev_where, group=list(group_parts), joins=list(joins),
    )
    kind = "mom" if (spec.compare.type if spec.compare else "mom") == "mom" else "yoy"
    sql, extra_columns = _compare_sql(
        cur=query.render(), prev=prev_query.render(),
        dim_cols=dim_cols, metric_cols=metric_outs, kind=kind,
        spec=spec, columns=columns, max_rows=max_rows,
    )
    params.update(prev_params)
    for col in extra_columns:
        columns.append(col)
    labels.update({c: labels.get(m, m) + ("(上期)" if c.endswith("_prev") else "(变化%)")
                   for m in metric_outs for c in (f"{m}_prev", f"{m}_{kind}_pct")})
    notes.append("同环比由引擎算(上一等长周期/去年同期), 不由模型做算术")
    return _finish(sql, params, spec, columns, labels, notes, max_rows, "compiler")


def compile_metric_api(
    spec: QuerySpec,
    *,
    catalog: MetricCatalog,
    scope: Any,
    check: ValidationResult | None = None,
    max_rows: int = 100,
) -> Compiled:
    """降级链第 1 级: 指标 API —— 除时间窗外不含任何来自模型的谓词。

    带过滤条件/同环比/多指标的 DSL 不走这一级(抛 :class:`CompileError` 让上层降级),
    因为"确定性口径"的含义就是**这条 SQL 完全由语义层定义决定**。
    """
    check = check or validate(spec, catalog=catalog, scope=_caller_scope(scope))
    if not check.ok:
        raise CompileError(f"DSL 未通过校验: {check.as_text()}")
    if spec.filters:
        raise CompileError("指标 API 通道不接受自定义过滤条件, 请走编译器")
    if spec.compare and spec.compare.type != "none":
        raise CompileError("指标 API 通道不做同环比, 请走编译器")
    if len(check.metrics) > 1:
        raise CompileError("指标 API 通道一次只算一个指标, 请走编译器")
    compiled = compile_analytical(spec, catalog=catalog, scope=scope, check=check, max_rows=max_rows)
    return Compiled(**{**compiled.__dict__, "route": "metric_api"})


# ---------------------------------------------------------------------------
# 文本装配(都是模板, 没有一次模型输出参与)
# ---------------------------------------------------------------------------
@dataclass
class _Query:
    """一条聚合查询的部件(文本装配只在本类里发生, 保证形状只有一种)。"""

    table: str
    alias: str
    select: list[str]
    where: list[str]
    group: list[str]
    joins: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.select:
            raise CompileError("内部错误: 空 SELECT")
        sql = f"SELECT {', '.join(self.select)} FROM {self.table} {self.alias}"
        for join in self.joins:
            sql += f" {join}"
        if self.where:
            sql += " WHERE " + " AND ".join(f"({w})" for w in self.where)
        if self.group:
            sql += " GROUP BY " + ", ".join(self.group)
        return sql


def _caller_scope(scope: Any) -> Any:
    from app.semantic.validate import CallerScope

    return CallerScope.from_scope(scope)


def _cap_rows(spec: QuerySpec, max_rows: int) -> int:
    return max(1, min(int(spec.limit or max_rows), max_rows))


def _order_and_limit(
    spec: QuerySpec, *, columns: list[str], default_order: str, max_rows: int
) -> str:
    """ORDER BY 只允许引用已产出的列(列名由本模块生成, 不来自模型)。"""
    order = ""
    raw = (spec.order_by or "").strip()
    if raw:
        name, _, direction = raw.partition(" ")
        if name not in columns:
            raise CompileError(
                f"order_by 的 {name} 不是本次产出的列(可选: {', '.join(columns)})"
            )
        order = f" ORDER BY {name}" + (f" {direction}" if direction else "")
    elif default_order:
        # 默认按主指标降序: 问数结果的人读习惯是"最大的排前面", 而不是按名序。
        order = f" ORDER BY {default_order} DESC"
    return f"{order} LIMIT {_cap_rows(spec, max_rows)}"


def _compare_sql(
    *,
    cur: str,
    prev: str,
    dim_cols: list[str],
    metric_cols: list[str],
    kind: str,
    spec: QuerySpec,
    columns: list[str],
    max_rows: int,
) -> tuple[str, list[str]]:
    """同环比装配: 两条同形查询做 CTE, 差异只在绑参时间窗。

    两个分支都是确定的:
    - 有下钻维度: ``cur LEFT JOIN prev ON 维度列相等``(上一周期没有该维度的行则上期为 NULL);
    - 无维度(单值对比): 用标量子查询, 避开"无键 JOIN 须写恒真 ON"那种形状 ——
      恒真条件在 AST 校验器里是被拒项, 不该出现在自己写的模板里。

    变化率交给 SQL 算(``NULLIF`` 把分母为 0 变成 NULL而不是报错), 不由模型做算术。
    """
    head = f"WITH cur AS ({cur}), prev AS ({prev}) "
    cu, pr = "cu", "pr"
    extra: list[str] = []
    if dim_cols:
        select = [f"{cu}.{d} AS {d}" for d in dim_cols]
        for m in metric_cols:
            select.append(f"{cu}.{m} AS {m}")
            select.append(f"{pr}.{m} AS {m}_prev")
            pct = f"{m}_{kind}_pct"
            select.append(
                f"ROUND(({cu}.{m} - {pr}.{m}) / NULLIF({pr}.{m}, 0) * 100, 1) AS {pct}"
            )
            extra.extend([f"{m}_prev", pct])
        on = " AND ".join(f"{cu}.{d} = {pr}.{d}" for d in dim_cols)
        body = f"{head}SELECT {', '.join(select)} FROM cur {cu} LEFT JOIN prev {pr} ON {on}"
        default_order = metric_cols[0]
    else:
        scalars = []
        for m in metric_cols:
            scalars.append(f"(SELECT {m} FROM cur) AS {m}")
            scalars.append(f"(SELECT {m} FROM prev) AS {m}_prev")
            scalars.append(
                f"ROUND(((SELECT {m} FROM cur) - (SELECT {m} FROM prev)) "
                f"/ NULLIF((SELECT {m} FROM prev), 0) * 100, 1) AS {m}_{kind}_pct"
            )
            extra.extend([f"{m}_prev", f"{m}_{kind}_pct"])
        body = f"{head}SELECT {', '.join(scalars)}"
        default_order = ""
    sql = body + _order_and_limit(
        spec, columns=columns + extra, default_order=default_order, max_rows=max_rows,
    )
    return sql, extra


def _finish(
    sql: str,
    params: dict[str, Any],
    spec: QuerySpec,
    columns: list[str],
    labels: dict[str, str],
    notes: list[str],
    max_rows: int,
    route: str,
) -> Compiled:
    from app.db.ast_guard import READ_MODE, validate_sql

    cap = max(1, min(int(spec.limit or max_rows), max_rows))
    try:
        checked = validate_sql(
            sql, allowed_tables=_allowed_tables(), mode=READ_MODE, max_rows=cap, normalize=False,
        )
    except Exception as exc:  # noqa: BLE001 - 编译产物过不了自己的 AST 关, 是代码问题而非用户问题
        logger.exception("编译产物未通过 AST 自校验")
        raise CompileError(f"编译产物未通过安全校验: {exc}") from exc
    return Compiled(
        sql=checked, params=params, route=route, columns=columns, labels=labels,
        caliber_notes=notes, limit=cap,
    )


def _allowed_tables() -> set[str]:
    from app.analytics.reports import ALLOWED_TABLES

    return set(ALLOWED_TABLES)
