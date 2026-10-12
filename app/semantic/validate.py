"""三级校验器(独立模块, **不信任 LLM 自检**): 语法 -> 语义 -> 权限。

为什么不让模型自己在 prompt 里"保证合法": 模型说"我检查过了"与它是否真的合法之间
没有因果关系。本模块是纯确定性的第二双眼睛, 输入是 DSL + 语义层快照 + 调用者作用域,
输出是带错误码的问题清单 —— 错误码是给状态机用的(决定"回炉重写"还是"直接拒"),
消息文本是给人和模型看的。

三级各自的失败代价不同, 所以分级:
- **L1 语法**: 形状/枚举值/上限, 由 pydantic 在 :mod:`app.semantic.dsl` 构造时就把住;
  本模块只做 pydantic 做不了的**跨字段**判断(如同环比配 all_time)。
- **L2 语义**: 指标/维度是否登记、是否兼容、区间是否可解、枚举值是否存在、
  多指标是否共享同一张事实表与同一行集。
- **L3 权限**: 指标级角色白名单 + 行级作用域口径。注意 L3 只负责"**该不该给这个人算**",
  "这个人实际能读到哪些行"永远由数据库 RLS 决定, 这里不做二次实现, 只做明确注记。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.semantic.catalog import Dimension, Metric, MetricCatalog
from app.semantic.dsl import DslError, QuerySpec
from app.semantic import timesem

# ---------------- 错误码(状态机与归因路由都按码分流, 不按文本猜) ----------------
E_SCHEMA = "E_SCHEMA"                            # 跨字段形状错
E_UNKNOWN_METRIC = "E_UNKNOWN_METRIC"            # 指标未登记/已停用
E_UNKNOWN_DIM = "E_UNKNOWN_DIM"                  # 维度未登记
E_INCOMPATIBLE_DIM = "E_INCOMPATIBLE_DIM"        # 维度与该指标不兼容
E_TABLE_CONFLICT = "E_TABLE_CONFLICT"            # 多指标跨事实表, 一条聚合查询算不出来
E_CALIBER_CONFLICT = "E_CALIBER_CONFLICT"        # 多指标里有指标带自己的行集口径
E_TIME_INVALID = "E_TIME_INVALID"                # 区间解不出/跨度超限/all_time 配同环比
E_ENUM_INVALID = "E_ENUM_INVALID"                # 过滤值不在维度枚举内
E_NO_PERMISSION = "E_NO_PERMISSION"              # 指标级角色白名单不通过
E_COL_POLICY = "E_COL_POLICY"                    # 列级策略: 这个角色的查询里不许碰这一列
E_ROWS_CAP = "E_ROWS_CAP"                        # 行数越界

# ---------------- 注记(不是错, 但必须让用户看到口径变化) ----------------
N_DEPT_SCOPE = "N_DEPT_SCOPE"
N_ALL_TIME = "N_ALL_TIME"
N_GRANULARITY = "N_GRANULARITY"
N_TIME_IGNORED = "N_TIME_IGNORED"
N_ROW_CAP = "N_ROW_CAP"
N_AUTO_FILTER = "N_AUTO_FILTER"                  # 不兼容维度被默认过滤救回了
N_REDUNDANT_FILTER = "N_REDUNDANT_FILTER"        # 与指标口径重复的限定已丢弃

DEFAULT_MAX_ROWS = 200


@dataclass(frozen=True)
class Issue:
    """一条校验结论: 错误码 + 可读原因 + 出问题的字段。"""

    code: str
    message: str
    field: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


@dataclass(frozen=True)
class CallerScope:
    """校验需要的最小作用域信息(从 ``DataScope`` 投影, 避免本模块依赖 DB 层)。"""

    role: str = ""
    all_depts: bool = False
    tenant_id: str = ""
    note: str = ""

    @classmethod
    def from_scope(cls, scope: Any) -> "CallerScope":
        if scope is None:
            return cls()
        return cls(
            role=str(getattr(scope, "role", "") or ""),
            all_depts=bool(getattr(scope, "all_depts", False)),
            tenant_id=str(getattr(scope, "tenant_id", "") or ""),
            note=str(scope.note()) if callable(getattr(scope, "note", None)) else "",
        )


@dataclass
class ValidationResult:
    """一次校验的全部产出。"""

    errors: list[Issue] = field(default_factory=list)
    notices: list[Issue] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    dims: list[Dimension] = field(default_factory=list)
    filter_dims: dict[str, Dimension] = field(default_factory=dict)
    resolved: timesem.ResolvedRange | None = None
    # 兼容规则标 default_filter 时自动补上的过滤条件(编译器把它们当普通 filter 用):
    # 它们不是模型写的, 而是语义层登记过的救回规则, 所以可以进 SQL 谓词位。
    auto_filters: list[dict[str, Any]] = field(default_factory=list)
    # 被识别为"与指标口径重复"而决意不加的过滤三元组 (dim, op, str(value))。
    # 为什么不能只是"丢不算了": 编译器渲染的是 spec.filters, 两边各判一次就会出现
    # "校验说可以、编译说维度未登记"的自相矛盾(实际踩过)。
    redundant_filters: set[tuple[str, str, str]] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> list[str]:
        return [e.code for e in self.errors]

    def as_text(self, limit: int = 5) -> str:
        """给模型回炉用的紧凑拒因(只给可执行的部分, 不堆全量错误)。"""
        parts = [f"[{e.code}] {e.message}" for e in self.errors[:limit]]
        return " | ".join(parts) or "DSL 校验未通过(无具体错误项)"

    def to_payload(self) -> dict[str, Any]:
        return {
            "errors": [e.to_dict() for e in self.errors],
            "notices": [e.to_dict() for e in self.notices],
            "auto_filters": list(self.auto_filters),
            "redundant_filters": ["|".join(item) for item in sorted(self.redundant_filters)],
        }


def validate(
    spec: QuerySpec,
    *,
    catalog: MetricCatalog,
    scope: CallerScope | None = None,
    max_time_span_days: int | None = None,
    max_rows: int | None = None,
    today: Any = None,
) -> ValidationResult:
    """跑完 L1/L2/L3 三级校验, 返回问题清单与解析结果。

    刻意**一次跑完而不是首错即返**: 回炉重写时把全部问题一起给模型, 收敛比逐条挤快。
    """
    caller = scope or CallerScope()
    cap_rows = int(max_rows if max_rows is not None else _default_max_rows())
    result = ValidationResult()
    tables: set[str] = set()

    # ---------------- L2: 指标 ----------------
    for item in spec.metrics:
        metric = catalog.get_metric(item.id)
        if metric is None:
            result.errors.append(Issue(
                E_UNKNOWN_METRIC,
                f"指标 {item.id} 不在语义层登记集内(或已停用); 可用指标见语义层清单",
                f"metrics[{item.id}]",
            ))
            continue
        if metric.role_set and caller.role and caller.role not in metric.role_set:
            result.errors.append(Issue(
                E_NO_PERMISSION,
                f"指标 {metric.label} 限定角色 {sorted(metric.role_set)}, 当前角色无权取数",
                f"metrics[{item.id}]",
            ))
        if metric.source_table:
            tables.add(metric.source_table)
        result.metrics.append(metric)

    if len(result.metrics) != len(spec.metrics):
        return result  # 指标都解不出来, 后面的检查没有意义

    if len(tables) > 1:
        result.errors.append(Issue(
            E_TABLE_CONFLICT,
            f"这些指标分属不同事实表 {sorted(tables)}, 一条查询算不出来; 请拆成两次问数",
            "metrics",
        ))
        return result

    if len(result.metrics) > 1 and any(m.caliber_where for m in result.metrics):
        result.errors.append(Issue(
            E_CALIBER_CONFLICT,
            "同一次查询里的指标带各自限定行集(如只算已打款), 会互相污染; "
            "请每次只问一个带限定口径的指标",
            "metrics",
        ))

    # ---------------- L2: 时间 ----------------
    compare = spec.compare.type if spec.compare else "none"
    time_empty = not (spec.time.range or spec.time.start or spec.time.end)
    snapshots_only = bool(result.metrics) and not any(m.has_time for m in result.metrics)
    if time_empty and snapshots_only and compare == "none":
        # 快照型指标(在职人数、年度预算消耗率)库里就没有时间列: "没给时间"是合法问法。
        # 拿"缺少时间范围"拒它等于要求用户对一张没有时间列的表报一个时间(实际会让
        # "在职人数是多少"这类最自然的问法在回炉两次后变拒答)。
        result.resolved = timesem.ResolvedRange(
            token="all_time", start=None, end=None, gran="none",
            label="不受时间区间约束(快照型口径)", whole=True,
        )
        result.notices.append(Issue(
            N_TIME_IGNORED, "该指标是快照型(无时间列), 时间区间不影响结果", "time",
        ))
    else:
        try:
            resolved = timesem.resolve(
                spec.time, compare=compare, today=today, default_gran="none",
                max_span_days=max_time_span_days,
            )
        except (timesem.TimeError, DslError) as exc:
            # 两种拒因都要接住: 区间词不合法在 SpecTime 构造时是 DslError, 解不出区间是
            # TimeError; 只接一种的话另一种会一路抛穿状态机(变成"服务异常"而不是可修正错)。
            result.errors.append(Issue(E_TIME_INVALID, str(exc), "time"))
            return result
        result.resolved = resolved
        if resolved.whole:
            result.notices.append(Issue(N_ALL_TIME, "本次统计不限时间(全部历史)", "time"))
        if spec.time.granularity not in ("", "none") and resolved.gran != (result.metrics[0].calendar or resolved.gran):
            # 只在用户真的明说了粒度时才提: 模型把 granularity 写成 "none" 是"没说粒度",
            # 拿它去和指标默认周期比会喷出一条噪声注记, 把真正的口径说明埋掉。
            result.notices.append(Issue(
                N_GRANULARITY,
                f"聚合粒度按用户明说的 {resolved.gran}(指标默认周期 {result.metrics[0].calendar})",
                "time.granularity",
            ))
        if resolved.gran == "none" and not resolved.whole and not any(m.has_time for m in result.metrics):
            result.notices.append(Issue(
                N_TIME_IGNORED,
                "该指标是快照型(无时间列), 时间区间不影响结果",
                "time",
            ))

    # ---------------- L2: 维度 ----------------
    usable_dims: set[str] = set()
    for metric in result.metrics:
        usable_dims |= set(catalog.compatible_dim_ids(metric))
    for dim_id in spec.dims:
        dim = catalog.get_dim(dim_id)
        if dim is None:
            result.errors.append(Issue(
                E_UNKNOWN_DIM, f"维度 {dim_id} 不在语义层登记集内", f"dims[{dim_id}]",
            ))
            continue
        incompatible, rescued = _incompatible_metrics(catalog, result.metrics, dim_id)
        if rescued:
            # 不是"兼容"也不是"拒": 登记过一条默认过滤可以把这个问法救回来。
            # 不静默采用: 写成注记, 用户看到"本次已限定 X"才能发现这不是他要的口径。
            result.auto_filters.extend(rescued)
            result.notices.append(Issue(
                N_AUTO_FILTER,
                "维度 " + dim.label + " 与部分指标默认不一起看, 本次按登记规则自动加了限定: "
                + "; ".join(sorted({str(item.get("note") or "") for item in rescued if item.get("note")})),
                f"dims[{dim_id}]",
            ))
        if incompatible:
            result.errors.append(Issue(
                E_INCOMPATIBLE_DIM,
                f"维度 {dim.label} 与指标 {', '.join(incompatible)} 不兼容"
                f"(本次可下钻的维度: {', '.join(sorted(usable_dims)) or '无'})",
                f"dims[{dim_id}]",
            ))
            continue
        result.dims.append(dim)
        # 部门维度对非全员角色是"只能看到自己那一行", 不是"越权": 必须注记而不是静默。
        if "department" in dim_id and not caller.all_depts:
            result.notices.append(Issue(
                N_DEPT_SCOPE,
                f"当前作用域是 {caller.note or '本部门'}, 按 {dim.label} 下钻只会得到本范围内的行",
                f"dims[{dim_id}]",
            ))

    # ---------------- L2: 过滤条件 ----------------
    usable = {d.dim_id for d in result.dims}
    for filt in spec.filters:
        dim = catalog.get_dim(filt.dim)
        if dim is None:
            result.errors.append(Issue(
                E_UNKNOWN_DIM, f"过滤维度 {filt.dim} 不在语义层登记集内", f"filters[{filt.dim}]",
            ))
            continue
        op_text = str(filt.op.value if hasattr(filt.op, "value") else filt.op)
        if op_text in ("eq", "ne") and _filter_already_in_caliber(result.metrics, dim, filt):
            # 指标名里已经含这个限定(如"一般纳税人供应商数" = COUNT(*) FILTER (WHERE
            # qualification='一般纳税人')), 模型再写一遍会**改变结果形状**:
            # 按类别分组时, 加这个过滤只剩 3 行, 不加则是 5 行(此两个类别计数为 0)。
            # 宁肯不加重复限定(口径已保证结果), 也不能拿一张少几行的表当答案。
            result.filter_dims[filt.dim] = dim  # 维度仍登记: 它可能还带着 JOIN 需求
            result.redundant_filters.add((filt.dim, op_text, str(filt.value)))
            result.notices.append(Issue(
                N_REDUNDANT_FILTER,
                f"过滤条件 {dim.label}={filt.value} 已含在指标口径里, 本次不重复加(否则会多几行零值被当作没数据)",
                f"filters[{filt.dim}]",
            ))
            continue
        if spec.metrics and filt.dim not in usable:
            incompatible, rescued = _incompatible_metrics(catalog, result.metrics, filt.dim)
            if incompatible:
                result.errors.append(Issue(
                    E_INCOMPATIBLE_DIM,
                    f"过滤维度 {dim.label} 与所选指标不兼容",
                    f"filters[{filt.dim}]",
                ))
                continue
            for item in rescued:
                if item not in result.auto_filters:
                    result.auto_filters.append(item)
        result.filter_dims[filt.dim] = dim
        _check_enum(result, dim, filt)

    # ---------------- L3: 列级策略(按角色剔除敏感列) ----------------
    _check_column_policies(result, catalog, caller)

    # ---------------- L3: 行数上限 ----------------
    if spec.limit and spec.limit > cap_rows:
        result.errors.append(Issue(
            E_ROWS_CAP, f"limit {spec.limit} 超过本部署单次上限 {cap_rows}", "limit",
        ))
    return result


def _incompatible_metrics(
    catalog: MetricCatalog, metrics: list[Metric], dim_id: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """维度对哪些指标不兼容, 以及哪些靠登记的 default_filter 救回。

    兼容判定优先看细分表(``sem_metric_dim_compat``): 没行时回到指标上的名单。
    为什么不拿名单当唯一真相: 名单只能表达"能/不能", 救不回"能, 但得加一个限定"。
    """
    incompatible: list[str] = []
    rescued: list[dict[str, Any]] = []
    for metric in metrics:
        item = catalog.compat_for(metric.metric_id, dim_id)
        if item is None:
            if dim_id not in metric.compatible_dims:
                incompatible.append(metric.metric_id)
            continue
        if item.compat == "ok":
            continue
        if item.compat == "default_filter" and item.default_filter:
            entry = dict(item.default_filter)
            entry.setdefault("dim", dim_id)
            entry.setdefault("op", "eq")
            entry["_from"] = metric.metric_id
            entry.setdefault("note", item.note)
            rescued.append(entry)
            continue
        incompatible.append(metric.metric_id)
    return incompatible, rescued


def _check_column_policies(result: ValidationResult, catalog: MetricCatalog, caller: CallerScope) -> None:
    """列级策略命中即拒(默认拒而不是"把列默默剔掉接着算")。

    为什么不能默默剔除: 用户问的是"年假余额合计", 把列剔掉后给一个 0 或给另一个指标,
    都是"算了一个他没问的数"。拒答至少不会把错的数写进他的汇报。

    判据用整词匹配而不是解析表达式: 本模块要保持纯函数(不依赖 sqlglot 与 DB),
    而语义层的列名都是受控标识符, 整词命中足够保守(宁可多拒一次也不放过一个敏感列)。
    """
    if not caller.role or not catalog.col_policies:
        return
    for metric in result.metrics:
        denied = catalog.denied_columns_for(caller.role, metric.source_table)
        if not denied:
            continue
        text = f"{metric.expression} {metric.caliber_where} {metric.time_column}"
        hit = sorted(col for col in denied if _word_in(str(col), text))
        if hit:
            result.errors.append(Issue(
                E_COL_POLICY,
                f"指标 {metric.label} 的口径用到当前角色({caller.role})无权查询的列 {hit}; "
                "这不是口径不存在, 是这一层列级策略拦下的",
                f"metrics[{metric.metric_id}]",
            ))
    for dim in result.dims + list(result.filter_dims.values()):
        denied = catalog.denied_columns_for(caller.role, dim.source_table)
        if denied and _word_in(dim.column, " ".join(sorted(denied))):
            result.errors.append(Issue(
                E_COL_POLICY,
                f"维度 {dim.label} 落在列 {dim.column} 上, 该列对角色 {caller.role} 不开放",
                f"dims[{dim.dim_id}]",
            ))


def _word_in(word: str, text: str) -> bool:
    return bool(word) and re.search(rf"\b{re.escape(word)}\b", text or "") is not None


def _filter_already_in_caliber(metrics: list[Metric], dim: Dimension, filt: Any) -> bool:
    """这个过滤是否已被指标口径自带(列名与值都在口径文本里以 `col = '值'` 共现)。

    只认**完全等值且值字面一致**的情形(宁可少删): 删多了就是静默放宽口径, 比留个重复
    限定危险得多。值不来自数据库而是来自口径表达式本身, 所以这里只做文本比对。
    """
    column = (dim.column or "").strip()
    op = str(filt.op.value if hasattr(filt.op, "value") else filt.op)
    value = filt.value
    if not column or op != "eq" or not isinstance(value, str):
        return False
    want = re.compile(
        rf"\b(?:{re.escape(column)}|{re.escape(dim.source_table or '_')}\.{re.escape(column)})\s*=\s*"
        rf"'{re.escape(value.replace(chr(39), chr(39) * 2))}'",
        re.I,
    )
    for metric in metrics:
        for snippet in (metric.caliber_where, metric.expression):
            if snippet and want.search(snippet):
                return True
    return False


def _check_enum(result: ValidationResult, dim: Dimension, filt: Any) -> None:
    """静态枚举维度做值存在性校验(枚举靠库反查的维度不在这里判, 交给 SQL 自然为空)。"""
    values = list(dim.enum_values or [])
    if not values:
        return
    given = filt.value if isinstance(filt.value, (list, tuple)) else [filt.value]
    allowed = {str(v) for v in values}
    bad = [str(v) for v in given if str(v) not in allowed]
    if bad and str(filt.op.value if hasattr(filt.op, "value") else filt.op) in ("eq", "in", "ne"):
        result.errors.append(Issue(
            E_ENUM_INVALID,
            f"{dim.label} 没有取值 {bad}(可选: {', '.join(sorted(allowed))}); "
            "若这是部门/供应商等主数据名, 请让用户从候选里点选而不是改写",
            f"filters[{dim.dim_id}]",
        ))


def _default_max_rows() -> int:
    try:
        from app.config import get_settings

        return int(get_settings().askdata_max_rows or DEFAULT_MAX_ROWS)
    except Exception:  # noqa: BLE001 - 配置读不到时用保守默认, 而不是放开上限
        return DEFAULT_MAX_ROWS
