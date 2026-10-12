"""层 4(只读对偶): DSL -> SQL 编译器(确定性, 无 LLM)。

设计姿态与写通道(:mod:`app.db.dataops`)一致: **SQL 文本由服务端模板产出**, 模型给的
只有语义对象 id 与业务条件值。于是三件事在结构上不成立:

1. 标识符注入 —— 表名/列名/聚合表达式全部从语义层登记表取出, 且在拼进文本前用
   sqlglot 把裸列限定成事实表别名(``amount`` -> ``r.amount``), 多表 JOIN 不会 ambiguous;
2. 值注入 —— 过滤值与时间窗一律走 ``:name`` 绑参(占位符名由本模块编号, 不接受外部传入);
3. 越权 —— 作用域谓词用 :meth:`app.db.scope.DataScope.scope_clause` 注入, 与 RLS 双保险。

两条出口对应降级链的前两级:
- :func:`compile_metric_api` —— 口径确定性首选: 除时间窗外**没有任何来自模型的谓词**;
- :func:`compile_analytical` —— 通用分析: 多维下钻 + 过滤 + 同环比。

同环比默认走**单扫描条件聚合**(``SUM(CASE WHEN 窗口 THEN 度量 END)``), 解不出度量片段时
退回双窗口 CTE: 前者是两回事里更重要的那件(一遇表扫描), 后者是确定性(任何形状
的聚合都能原样跑两次)。两条路产出的列名一致, 所以下游校验与前端不必区分。

产出的文本必须再过一次 :func:`app.db.ast_guard.validate_sql`(``normalize=False``,
因为 sqlglot 会把 ``:name`` 重写成驱动才认的形式, 而这里执行的是服务端自己写的模板),
不通过就抛 :class:`CompileError` —— 绝不外发未校验文本。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
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
    # 本轮生效的语义层版本与行策略(只进审计/留痕, 不进给模型的文本): 回查时要知道
    # 这个数是"哪一版口径 + 哪几条额外谓词"算出来的。
    semver: str = ""
    policies: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "sql": self.sql,
            "columns": self.columns,
            "labels": self.labels,
            "caliber_notes": self.caliber_notes,
            "limit": self.limit,
            "semver": self.semver,
            "policies": list(self.policies),
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
    extra_windows: tuple[tuple[str, Any, Any], ...] = (),
    catalog: MetricCatalog | None = None,
    policy_log: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """一份 WHERE 条件(作用域谓词 + 行策略 + 软删 + 时间窗 + 指标口径)。

    ``window_suffix`` 让同环比的两个 CTE 用不同的绑参名而不共用值; ``extra_windows``
    是给单扫描条件聚合用的("当期与对比期都落在同一次扫描里"的 OR 谓词)。
    参数名由本模块编号生成, 永不来自模型输出。
    """
    parts: list[str] = []
    params: dict[str, Any] = {}
    if scope is not None:
        parts.append(scope.scope_clause(alias))
        params.update(scope.scope_params())
    else:
        raise CompileError("缺少数据作用域, 拒绝编译无隔离的查询")
    policy_parts, policy_params = _policy_parts(
        metric=metric, alias=alias, scope=scope, catalog=catalog, policy_log=policy_log
    )
    parts.extend(policy_parts)
    params.update(policy_params)
    if metric.source_table in SOFT_DELETE_TABLES:
        parts.append(f"{alias}.is_deleted = false")
    windows: list[tuple[str, Any, Any]] = [(window_suffix, resolved.start, resolved.end)]
    windows.extend(extra_windows)
    if not resolved.whole and metric.has_time:
        col = _qualify(metric.time_column, alias, where="时间列")
        clauses = []
        for suffix, start, end in windows:
            start_key, end_key = f"p_start{suffix}", f"p_end{suffix}"
            clauses.append(f"({col} >= :{start_key} AND {col} < :{end_key})")
            params[start_key] = start.isoformat() if start else ""
            params[end_key] = end.isoformat() if end else ""
        parts.append(clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")")
    if metric.caliber_where:
        parts.append(f"({_qualify(metric.caliber_where, alias, where='指标口径谓词')})")
    return parts, params


# 行策略模板里允许的变量位 -> ``(绑参名后缀, 取值函数, 是否部门集)``。
_POLICY_BINDERS = {
    "{user_id}": ("user_id", lambda scope: getattr(scope, "user_id", "") or "", False),
    "{dept_id}": ("dept_id", lambda scope: getattr(scope, "dept_scope_text", lambda: "")() or "__none__", True),
    "{tenant_id}": ("tenant_id", lambda scope: getattr(scope, "tenant_id", "") or "", False),
    "{today}": ("today", lambda scope: date.today(), False),
}


def _policy_parts(
    *, metric: Metric, alias: str, scope: Any,
    catalog: MetricCatalog | None, policy_log: list[str] | None,
) -> tuple[list[str], dict[str, Any]]:
    """把生效的行策略翻成 ``(谓词片段, 绑参)``(在 RLS 之上再叠一层按指标/角色的限定)。

    值一律走绑参(部门集用 ``ANY(string_to_array(:..., ','))``, 与 RLS 同一写法), 模板里只出现
    列名与占位符 —— 所以策略文本被谁改过都拼不进一个值。片段与参数一起返回而不是存模块级
    字典: FastMCP 的同步工具跑在 threadpool 里, 任何进程级可变槽位都是串请求的通道。
    拿不到 catalog(内置兜底集)时为空: 策略是库里治理过的东西, 兜底集不携带它。
    """
    if catalog is None or not catalog.row_policies:
        return [], {}
    parts: list[str] = []
    params: dict[str, Any] = {}
    role = str(getattr(scope, "role", "") or "")
    for index, policy in enumerate(catalog.row_policy_for(role, metric.source_table, metric.metric_id)):
        fragment = policy.predicate.replace("{alias}", alias)
        for marker, (suffix, getter, is_dept_set) in _POLICY_BINDERS.items():
            if marker not in fragment:
                continue
            name = f"rp_{suffix}_{index}"
            params[name] = getter(scope)
            fragment = fragment.replace(
                marker,
                f"ANY(string_to_array(:{name}, ','))" if is_dept_set else f":{name}",
            )
        if policy_log is not None:
            policy_log.append(policy.policy_id)
        parts.append(f"({fragment})")
    return parts, params


def _render_filters(
    spec: QuerySpec, dims: dict[str, Dimension], alias: str,
    extra: tuple[dict[str, Any], ...] = (),
    redundant: frozenset[tuple[str, str, str]] = frozenset(),
) -> tuple[list[str], dict[str, Any]]:
    """把过滤条件渲染成谓词 + 绑参(谓词模板与写通道同源: 复用 datadsl 的 ``_OP_TEMPLATES``)。

    列位上放的是**本模块限定好的列**(``r.category``), 而不是模型给的 dim_id ——
    所以谓词里没有任何一个字来自模型。

    ``extra`` 是校验器从语义层兼容规则里拿到的 default_filter: 它们同样不是模型写的,
    但同样要过"列名必须已登记"这一关(拿不到维度就直接拒, 不默默丢弃一条限定)。

    ``redundant`` 是校验器已判定"与指标口径重复"的过滤三元组: 由校验器决定不加,
    这里只照决定执行 —— 两边各自判一次就会出现"校验通过但编译说维度未登记"的矛盾。
    """
    from app.db.datadsl import FilterOp, predicate_sql
    from app.semantic.dsl import SpecFilter

    items: list[Any] = list(spec.filters)
    for item in extra:
        dim_id = str(item.get("dim") or "")
        if dim_id not in dims:
            raise CompileError(f"兼容规则指定的默认过滤维度 {dim_id or '?'} 未登记, 无法编译")
        try:
            items.append(SpecFilter.model_validate({
                "dim": dim_id, "op": item.get("op") or "eq", "value": item.get("value"),
            }))
        except Exception as exc:  # noqa: BLE001 - 兼容规则写错了是配置问题, 要说清楚
            raise CompileError(f"兼容规则的默认过滤不合法({dim_id}): {exc}") from exc

    parts: list[str] = []
    params: dict[str, Any] = {}
    for index, filt in enumerate(items):
        dim = dims.get(filt.dim)
        if dim is None:
            raise CompileError(f"过滤维度 {filt.dim} 未登记, 无法编译")
        op_name = str(filt.op.value if hasattr(filt.op, "value") else filt.op)
        if (str(filt.dim), op_name, str(filt.value)) in redundant:
            continue  # 口径里已经有这个限定, 再加一次会掉那些本应为 0 的分组行
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


_JOIN_ALIAS_RE = re.compile(r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)\b")


def _require_joined_alias(alias: str, joins: list[str], dims: list[Dimension]) -> None:
    """维度用到的表别名必须真的被 JOIN 过 —— 否则 PG 会报"missing FROM-clause entry"。

    为什么要在这里拒而不是让数据库报错: 运行时报错会**逐降级落到沙箱**, 沙箱里的模型
    会自己拼一个 JOIN 并算出一个看起来对但没人核对过的数(实际被这项问题抖出来过:
    报销指标用了登记在 hr_employees 上的员工姓名维度, 没 join_sql 也没人补)。
    拒在编译前、并把"该登记什么"写进拒因, 才能让人一步改到语义层。
    """
    available = {alias}
    for join in joins:
        match = _JOIN_ALIAS_RE.search(join)
        if match:
            available.add(match.group(2))
    for dim in dims:
        prefix = dim.column_prefix or alias
        if prefix in available:
            continue
        raise CompileError(
            f"维度 {dim.label}({dim.dim_id}) 落在 {prefix}.{dim.column} 上, 但本次 FROM/JOIN 里没有"
            f" {prefix}; 请在语义层为该事实表登记这个维度的 join_sql(或改用已登记的同义维度),"
            " 而不是让模型自己拼 JOIN"
        )


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
    # 单扫描同环比只要维度部分(度量要在它上面重新包一层 CASE)。
    dim_select = list(select_parts)
    dim_group = list(group_parts)

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
    filter_dims = dict(check.filter_dims)
    # 兼容规则救回来的 default_filter 也是一个过滤维度: 它同样可能要带进关联表。
    for item in check.auto_filters:
        dim = catalog.get_dim(str(item.get("dim") or ""))
        if dim is not None and dim.dim_id not in filter_dims:
            filter_dims[dim.dim_id] = dim
    for dim in filter_dims.values():
        if not dim.join_sql:
            continue
        text = dim.join_sql.replace("{alias}", alias)
        if text.split(" ON ")[0] not in join_keys:
            joins.append(text)
            join_keys.add(text.split(" ON ")[0])
    _require_joined_alias(alias, joins, check.dims + list(filter_dims.values()))

    policy_log: list[str] = []
    where_parts, params = _where_parts(
        metric=metric, alias=alias, scope=scope, resolved=resolved,
        catalog=catalog, policy_log=policy_log,
    )
    filter_parts, filter_params = _render_filters(
        spec, filter_dims, alias, extra=tuple(check.auto_filters),
        redundant=frozenset(check.redundant_filters),
    )
    params.update(filter_params)
    where_parts.extend(filter_parts)
    if check.auto_filters:
        notes.append("本次按语义层登记的兼容规则自动加了限定(不是模型自己补的条件)")
    if check.redundant_filters:
        notes.append("有一条限定与指标口径重复, 本次没重复加(否则计数为 0 的分组行会从结果里消失)")
    dim_cols = [dim_alias[d.dim_id] for d in check.dims]
    query = _Query(
        table=metric.source_table, alias=alias, select=select_parts,
        where=where_parts, group=group_parts, joins=joins,
    )
    semver = catalog.semver

    if not with_compare:
        sql = query.render() + _order_and_limit(
            spec, columns=columns, default_order=metric_outs[0] if dim_cols else "", max_rows=max_rows,
        )
        return _finish(sql, params, spec, columns, labels, notes, max_rows, "compiler",
                       semver=semver, policies=policy_log)

    if resolved.prev_start is None or resolved.prev_end is None:
        raise CompileError("对比期没算出来, 不能出同环比")
    if resolved.note:
        notes.append(f"对比期: {resolved.note}")
    kind = "mom" if (spec.compare.type if spec.compare else "mom") == "mom" else "yoy"

    # 首选单扫描条件聚合(同一张事实表只扫一次, 方案里同环比的标准形状)。
    single = _compare_single_scan(
        spec=spec, metrics=check.metrics, metric=metric, alias=alias, scope=scope,
        resolved=resolved, catalog=catalog, dim_select=dim_select, dim_group=dim_group,
        joins=joins, filter_parts=filter_parts, metric_outs=metric_outs, kind=kind,
        columns=columns, max_rows=max_rows, policy_log=policy_log,
    )
    if single is not None:
        sql, extra_columns, single_params = single
        notes.append("同环比走单扫描条件聚合(SUM(CASE WHEN 窗口 ...)), 事实表只扫一次")
        notes.append("同环比由引擎算(上一等长周期/去年同期), 不由模型做算术")
        for col in extra_columns:
            columns.append(col)
        labels.update({c: labels.get(m, m) + ("(上期)" if c.endswith("_prev") else "(变化%)")
                       for m in metric_outs for c in (f"{m}_prev", f"{m}_{kind}_pct")})
        return _finish(sql, single_params, spec, columns, labels, notes, max_rows, "compiler",
                       semver=semver, policies=policy_log)

    # 退回双窗口 CTE: 口径形状解不出"度量片段"(非常见聚合写法)时, 宁可多扫一次
    # 也不能算错。两条路产出同名列与同语义结果(上期为空时都得到 NULL 变化率),
    # 所以下游校验与前端不必区分。
    prev_where, prev_params = _where_parts(
        metric=metric, alias=alias, scope=scope,
        resolved=ResolvedRange(
            token=resolved.token, start=resolved.prev_start, end=resolved.prev_end,
            gran="none", label=resolved.label, whole=False,
        ),
        window_suffix="_prev", catalog=catalog,
    )
    prev_params.update(filter_params)
    prev_query = _Query(
        table=metric.source_table, alias=alias, select=list(select_parts),
        where=prev_where, group=list(group_parts), joins=list(joins),
    )
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
    notes.append("同环比走双窗口 CTE(口径未能安全拆成条件聚合), 结果与单扫描等价")
    return _finish(sql, params, spec, columns, labels, notes, max_rows, "compiler",
                   semver=semver, policies=policy_log)


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


_PH_RE = re.compile(r"__ph_([a-z0-9_]+)__")


def _restore_placeholders(sql: str | None) -> str | None:
    """把渲染阶段用的哨兵 token 恢复成 SQLAlchemy 的 ``:name`` 绑参。"""
    if sql is None:
        return None
    return _PH_RE.sub(r":\1", sql)


def _conditional(expression: str, cond_sql: str) -> str | None:
    """把口径表达式里的**每一个**聚合函数改写成条件聚合(只吃满足 ``cond_sql`` 的行)。

    为什么在 AST 上做而不是正则: 口径是登记过的自由形状片段(带 FILTER、COALESCE、
    两个聚合相除的比率型), 文本改写迟早会漏一种形状并**默默算错数**; AST 改写至少
    能保证"改不动就返回 None"(退回双窗口 CTE), 而不是猜。

    两个细节:
    - ``COUNT(*)`` 的参是 Star, 换成 ``CASE WHEN ... THEN 1 END``(COUNT 只数非 NULL);
    - ``FILTER (WHERE p)`` 的谓词并进 CASE 条件后整个节点换掉: PG 的
      ``COUNT(CASE ...) FILTER (WHERE p)`` 也合法, 但两种写法共存会让上期的 p 重复计算。
    """
    try:
        node = parse_one(expression, read="postgres")
    except ParseError:
        return None
    aggregates = list(node.find_all(exp.AggFunc))
    if not aggregates:
        return None  # 行级表达式(无聚合): 不能拿它做条件聚合, 交回 CTE 路径
    for agg in aggregates:
        parent = agg.parent
        wrapper = parent if isinstance(parent, exp.Filter) else None
        arg = agg.args.get("this")
        inner = "1" if arg is None or isinstance(arg, exp.Star) else arg.sql(dialect="postgres")
        condition = cond_sql
        if wrapper is not None:
            where_node = wrapper.expression
            condition = f"{condition} AND {where_node.this.sql(dialect='postgres')}"
        distinct = bool(agg.args.get("distinct"))
        name = agg.key.upper()
        replacement = f"{name}({'DISTINCT ' if distinct else ''}CASE WHEN {condition} THEN {inner} END)"
        try:
            new_node = parse_one(replacement, read="postgres")
        except ParseError:
            return None
        target = wrapper or agg
        # **根节点必须重新绑定**: 对 `COUNT(*) FILTER (WHERE 口径谓词)` 这种整棵表达式就是
        # 一个带 FILTER 的聚合的口径, replace() 只能改子节点 —— 拿它当已替换的话,
        # 渲出来的 SQL 还是原树(窗口条件静默消失, 上期=当期, 环比永远是 0%)。
        if target is node:
            node = new_node
        else:
            target.replace(new_node)
    return node.sql(dialect="postgres")


def _compare_single_scan(
    *,
    spec: QuerySpec,
    metrics: list[Metric],
    metric: Metric,
    alias: str,
    scope: Any,
    resolved: ResolvedRange,
    catalog: MetricCatalog,
    dim_select: list[str],
    dim_group: list[str],
    joins: list[str],
    filter_parts: list[str],
    metric_outs: list[str],
    kind: str,
    columns: list[str],
    max_rows: int,
    policy_log: list[str],
) -> tuple[str, list[str], dict[str, Any]] | None:
    """同环比的单扫描形状: ``(sql, 额外列, 绑参)``, 拿不到就返回 None 让上层走 CTE。

    形状是"内层一次扫描算出两列 + 外层算变化率": 同一层的 SELECT 里不能引用刚定义的别名,
    所以变化率必须到外层去; 这也顺带避开了"无维度时写恒真 JOIN"那个被 AST 校验器拒绝的形状。
    """
    if not metric.has_time or resolved.prev_start is None or resolved.prev_end is None:
        return None
    time_col = _qualify(metric.time_column, alias, where="时间列")
    # 窗口谓词里先放哨兵 token 而不是 `:p_start`: sqlglot 渲染时会把 `:name` 改写成驱动才认的
    # `%(name)s` 形式, 与模板里其余部分的 `:name` 混在一条语句里就是两种 paramstyle 同存
    # (轻则报绑定错, 重则默默少绑一个参数)。token 形如 __ph_p_start__ 被 sqlglot 当普通列名
    # 原样带过, 渲完再恢复成 `:p_start`。
    cond_cur = f"{time_col} >= __ph_p_start__ AND {time_col} < __ph_p_end__"
    cond_prev = f"{time_col} >= __ph_p_start_prev__ AND {time_col} < __ph_p_end_prev__"

    inner_select = list(dim_select)
    for out_name, m in zip(metric_outs, metrics):
        expression = _metric_expr(m, alias, grouped=bool(dim_group))
        cur = _restore_placeholders(_conditional(expression, cond_cur))
        prev = _restore_placeholders(_conditional(expression, cond_prev))
        if cur is None or prev is None:
            return None
        inner_select.append(f"{cur} AS {out_name}")
        inner_select.append(f"{prev} AS {out_name}_prev")

    where_parts, params = _where_parts(
        metric=metric, alias=alias, scope=scope, resolved=resolved,
        extra_windows=(("_prev", resolved.prev_start, resolved.prev_end),),
        catalog=catalog, policy_log=policy_log,
    )
    where_parts.extend(filter_parts)
    inner = _Query(
        table=metric.source_table, alias=alias, select=inner_select,
        where=where_parts, group=dim_group, joins=joins,
    ).render()

    extra: list[str] = []
    outer_select = list(_dim_select_columns(dim_select, columns))
    for out_name in metric_outs:
        prev_name = f"{out_name}_prev"
        pct_name = f"{out_name}_{kind}_pct"
        outer_select.append(out_name)
        outer_select.append(prev_name)
        # 分母用 NULLIF: 上期为 0/没数时变化率是 NULL("没得比")而不是一个除零异常或 0%。
        outer_select.append(
            f"ROUND(({out_name} - {prev_name}) / NULLIF({prev_name}, 0) * 100, 1) AS {pct_name}"
        )
        extra.extend([prev_name, pct_name])
    sql = f"SELECT {', '.join(outer_select)} FROM ({inner}) cmp" + _order_and_limit(
        spec, columns=columns + extra,
        default_order=metric_outs[0] if dim_select else "", max_rows=max_rows,
    )
    return sql, extra, params


def _dim_select_columns(select_parts: list[str], columns: list[str]) -> list[str]:
    """从内层维度选取片段里取回输出列名(外层只透传维度列)。"""
    out: list[str] = []
    for part in select_parts:
        name = part.rsplit(" AS ", 1)[-1].strip()
        if name in columns:
            out.append(name)
    return out


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


# 方言转译的目标集(方案 §4④ 要 CK/MySQL/Hive/PG): 只用于**比对产物**。
# 本仓数据源只有 PostgreSQL, 所以真正执行的永远是 `Compiled.sql`;
# 把 dialect 做成额外字段而不是替掉 sql, 是为了"能看目标方言长什么样"不会
# 顺便把取数打到不存在的引擎上(那才是真事故)。
DIALECTS = frozenset({"postgres", "clickhouse", "mysql", "hive", "spark", "trino", "duckdb"})


def transpile_sql(sql: str, dialect: str) -> str:
    """把编译产物转成另一种方言的 SQL 文本(不执行, 只给人对形状)。

    失败就抛 CompileError: 转译不出来比"默默给一段 PG 语法当作已转译"安全得多。
    """
    target = (dialect or "").strip().lower()
    if not target or target == "postgres":
        return sql
    if target not in DIALECTS:
        raise CompileError(f"不支持的方言 {dialect!r}(可用: {sorted(DIALECTS)})")
    try:
        from sqlglot import transpile

        converted = transpile(sql, read="postgres", write=target)
    except Exception as exc:  # noqa: BLE001 - 转译失败要当错报而不是静默返回原文
        raise CompileError(f"方言 {target} 转译失败: {type(exc).__name__}: {str(exc)[:160]}") from exc
    if not converted or not str(converted[0]).strip():
        raise CompileError(f"方言 {target} 转译结果为空")
    return str(converted[0])


def _finish(
    sql: str,
    params: dict[str, Any],
    spec: QuerySpec,
    columns: list[str],
    labels: dict[str, str],
    notes: list[str],
    max_rows: int,
    route: str,
    *,
    semver: str = "",
    policies: list[str] | None = None,
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
        caliber_notes=notes, limit=cap, semver=semver,
        # 去重: 同环比的单扫描与回退 CTE 都各跑一次 _where_parts, 同一策略会被记两遍;
        # 留痕里看"生效了哪几条策略"时不该重复计数。
        policies=sorted(set(policies or [])),
    )


def _allowed_tables() -> set[str]:
    from app.analytics.reports import ALLOWED_TABLES

    return set(ALLOWED_TABLES)
