"""语义层目录(纯内存形状): 指标/维度/枚举值/别名/兼容性/时间语义/行列策略的只读快照。

为什么单独一个模块: 校验器与编译器都只认这个形状, 于是
- 有数据库时由 :mod:`app.semantic.registry` 从 ``sem_*`` 表灌进来;
- 数据库不可用时由 :mod:`app.semantic.seed` 用内置集灌进来。
两条路径给校验器的是同一个对象, 不会出现"库挂了于是口径校验也一起没了"的降级空洞。

``semver`` 是定义集的内容指纹(不是时间戳): 同一个定义集在任何进程、任何时刻算出来
同一个号。 Trace 里带它才能把"一个 badcase" 回放到"当时那份口径"上 —— 用 updated_at
做版本号的后果是同一份定义每次回灌都换号, 回放时找不到当时那一版。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def fingerprint(parts: list[str]) -> str:
    """把定义片段列表压成一个短指纹(顺序无关: 调用方负责排序)。"""
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return "s" + digest[:12]


@dataclass(frozen=True)
class DimValue:
    """一个维度的取值(带黑话别名与热度)。"""

    dim_id: str
    value: str
    aliases: tuple[str, ...] = ()
    popularity: int = 0

    def matched_text(self) -> str:
        return " ".join((self.value, *self.aliases)).strip()


@dataclass(frozen=True)
class DimCompat:
    """指标×维度的兼容结论与补救措施。"""

    metric_id: str
    dim_id: str
    compat: str = "ok"            # ok / no / default_filter
    default_filter: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def allowed(self) -> bool:
        """``default_filter`` 也算允许: 它不是"试试看能不能 JOIN", 而是登记过的救回规则。"""
        return self.compat in ("ok", "default_filter")


@dataclass(frozen=True)
class TimeSemantic:
    """日历与对比基期规则(全局口径, 不写在单个指标行里)。"""

    cal_type: str = "natural"       # natural / fiscal
    week_start: int = 1             # 1=周一
    fiscal_year_start_month: int = 1
    compare_base_rule: str = "same_calendar_day"
    timezone: str = "Asia/Shanghai"
    description: str = ""


@dataclass(frozen=True)
class RowPolicy:
    """一条行级谓词模板(在 RLS 之上叠加"按指标/角色"的额外限定)。"""

    policy_id: str
    role: str
    table_name: str
    predicate: str
    metric_id: str = ""
    priority: int = 100
    note: str = ""


@dataclass(frozen=True)
class ColPolicy:
    """一个角色在某张表上被剔除的列。"""

    role: str
    table_name: str
    denied_cols: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Metric:
    """一个指标定义(与 ``sem_metrics`` 表列一一对应)。"""

    metric_id: str
    label: str
    domain: str = ""
    unit: str = ""
    agg: str = "sum"
    source_table: str = ""
    expression: str = ""
    caliber_where: str = ""
    join_sql: str = ""
    time_column: str = "created_at"
    compatible_dims: tuple[str, ...] = ()
    calendar: str = "month"
    allowed_roles: str = ""
    caliber_ref: str = ""
    owner: str = ""
    description: str = ""
    status: str = "active"
    version: int = 1

    @property
    def has_time(self) -> bool:
        """``time_column=''`` = 年度快照型指标(如预算), 不受时间区间约束。"""
        return bool(self.time_column)

    @property
    def output_alias(self) -> str:
        """输出列名: metric_id 里的非法字符换成下划线。"""
        return self.metric_id.replace("-", "_")

    @property
    def role_set(self) -> frozenset[str]:
        """,finance,admin, 形式的角色串 -> 集合(空串 = 不限, 继承域级白名单)。"""
        return frozenset(p.strip() for p in (self.allowed_roles or "").split(",") if p.strip())

    def caliber_note(self) -> str:
        """给人看的口径说明(必须出现在回答里, 让"这个数字是怎么算的"可追问)。"""
        parts = [f"口径: {self.expression}"]
        if self.caliber_where:
            parts.append(f"限定 {self.caliber_where}")
        if self.time_column:
            parts.append(f"按 {self.time_column} 左闭右开")
        else:
            parts.append("年度快照(不受时间区间约束)")
        if self.caliber_ref:
            parts.append(f"出处 {self.caliber_ref}")
        return "; ".join(parts)

    def embedding_text(self) -> str:
        """向量化的文本(名称 + 单位 + 口径 + 说明), 与别名文本同构便于统一召回。"""
        return f"{self.label} {self.unit} {self.description} {self.caliber_note()}".strip()

    @classmethod
    def from_row(cls, row: Any) -> "Metric":
        dims = row.compatible_dims or []
        return cls(
            metric_id=row.metric_id,
            label=row.label or row.metric_id,
            domain=row.domain or "",
            unit=row.unit or "",
            agg=row.agg or "sum",
            source_table=row.source_table or "",
            expression=row.expression or "",
            caliber_where=row.caliber_where or "",
            join_sql=row.join_sql or "",
            time_column=row.time_column or "",
            compatible_dims=tuple(str(d) for d in dims),
            calendar=row.calendar or "month",
            allowed_roles=row.allowed_roles or "",
            caliber_ref=row.caliber_ref or "",
            owner=getattr(row, "owner", "") or "",
            description=row.description or "",
            status=row.status or "active",
            version=int(getattr(row, "version", 1) or 1),
        )


@dataclass(frozen=True)
class Dimension:
    """一个维度定义(维度身份 = 语义概念 + 落在哪张表, 所以"部门"会有多行)。"""

    dim_id: str
    label: str
    source_table: str = ""
    column: str = ""
    join_sql: str = ""
    column_prefix: str = ""
    enum_source: str = "static"
    enum_values: tuple[str, ...] = ()
    dim_values: tuple[DimValue, ...] = ()
    hierarchy: tuple[str, ...] = ()
    time_like: bool = False
    description: str = ""
    status: str = "active"
    version: int = 1

    @property
    def qualified_column(self) -> str:
        """带前缀的列引用(多表 JOIN 时不写前缀会得到 ambiguous column)。"""
        if self.column_prefix:
            return f"{self.column_prefix}.{self.column}"
        return self.column

    def sample_values(self, limit: int = 6) -> str:
        return ", ".join(self.enum_values[:limit]) if self.enum_values else "-"

    @property
    def all_values(self) -> tuple[str, ...]:
        """静态枚举 + 细分表里的取值(去重保序): 枚举反查不再只能靠 DISTINCT 现查。"""
        out: list[str] = []
        for value in list(self.enum_values) + [v.value for v in self.dim_values]:
            if value and value not in out:
                out.append(value)
        return tuple(out)

    @classmethod
    def from_row(cls, row: Any) -> "Dimension":
        values = row.enum_values or []
        hierarchy = getattr(row, "hierarchy", None) or []
        return cls(
            dim_id=row.dim_id,
            label=row.label or row.dim_id,
            source_table=row.source_table or "",
            column=row.column or "",
            join_sql=row.join_sql or "",
            column_prefix=row.column_prefix or "",
            enum_source=row.enum_source or "static",
            enum_values=tuple(str(v) for v in values),
            hierarchy=tuple(str(h) for h in hierarchy),
            time_like=bool(row.time_like),
            description=row.description or "",
            status=row.status or "active",
            version=int(getattr(row, "version", 1) or 1),
        )


@dataclass(frozen=True)
class Term:
    """一条说法 -> 语义对象的映射。"""

    term: str
    term_type: str  # metric / dim / enum
    target_id: str
    weight: float = 1.0
    source: str = "seed"
    status: str = "active"

    @classmethod
    def from_row(cls, row: Any) -> "Term":
        return cls(
            term=row.term,
            term_type=row.term_type or "metric",
            target_id=row.target_id or "",
            weight=float(row.weight or 1.0),
            source=row.source or "seed",
            status=row.status or "active",
        )


@dataclass
class MetricCatalog:
    """一次语义层快照(进程级缓存的单位; 构建成本 = 几张元数据表的几条 SELECT)。"""

    metrics: dict[str, Metric] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    terms: tuple[Term, ...] = ()
    # 下面几样都可以为空(内置兜底集不携带细分面): 为空时的行为与改造前一致,
    # 而不是"没灌细分表就一个数都算不出来"。
    compat: tuple[DimCompat, ...] = ()
    time_semantics: dict[str, TimeSemantic] = field(default_factory=dict)
    row_policies: tuple[RowPolicy, ...] = ()
    col_policies: tuple[ColPolicy, ...] = ()
    # 读不出来的细分表(表名列表): “策略没生效”与“策略生效但没命中”必须分得开。
    missing: tuple[str, ...] = ()
    # 快照来源与时间: 出问题时要能回答"这次用的是库里的版本还是内置兜底集"。
    origin: str = "db"
    built_at: float = 0.0
    # semver 的 memo 位(不是定义的一部分, 不进指纹)。
    _semver_cache: str = field(default="", repr=False)

    # ---------------- 版本 ----------------
    @property
    def semver(self) -> str:
        """定义集内容指纹(不含 embedding/时间戳/别名权重以外的可变字段)。

        memo 在 ``_semver_cache``: 每次编译与每份留痕都要拿它, 而它是~500 个定义片段的
        拼接与哈希; 快照本身是 TTL 缓存的不变量, 所以指纹也可以算一次。
        """
        if self._semver_cache:
            return self._semver_cache
        parts: list[str] = []
        for metric in sorted(self.metrics.values(), key=lambda m: m.metric_id):
            parts.append(
                "|".join([
                    "m", metric.metric_id, metric.label, metric.unit, metric.agg,
                    metric.source_table, metric.expression, metric.caliber_where,
                    metric.join_sql, metric.time_column, metric.calendar,
                    metric.allowed_roles, metric.caliber_ref, metric.owner,
                    ",".join(sorted(metric.compatible_dims)), metric.status,
                ])
            )
        for dim in sorted(self.dimensions.values(), key=lambda d: d.dim_id):
            parts.append(
                "|".join([
                    "d", dim.dim_id, dim.label, dim.source_table, dim.column,
                    dim.join_sql, dim.column_prefix, dim.enum_source,
                    ",".join(sorted(dim.enum_values)), ",".join(sorted(dim.hierarchy)),
                    str(int(dim.time_like)), dim.status,
                ])
            )
            for value in sorted(dim.dim_values, key=lambda v: v.value):
                parts.append("|".join([
                    "v", dim.dim_id, value.value, ",".join(sorted(value.aliases)),
                    str(int(value.popularity)),
                ]))
        for term in sorted(self.terms, key=lambda t: (t.term, t.term_type, t.target_id)):
            parts.append("|".join([
                "t", term.term, term.term_type, term.target_id, f"{term.weight:.3f}", term.status,
            ]))
        for item in sorted(self.compat, key=lambda c: (c.metric_id, c.dim_id)):
            parts.append("|".join([
                "c", item.metric_id, item.dim_id, item.compat,
                json.dumps(item.default_filter, ensure_ascii=False, sort_keys=True),
            ]))
        for policy in sorted(self.row_policies, key=lambda p: p.policy_id):
            parts.append("|".join([
                "rp", policy.policy_id, policy.role, policy.table_name,
                policy.metric_id, policy.predicate, str(int(policy.priority)),
            ]))
        for policy in sorted(self.col_policies, key=lambda p: (p.role, p.table_name)):
            parts.append("|".join(["cp", policy.role, policy.table_name, *sorted(policy.denied_cols)]))
        for cal in sorted(self.time_semantics.values(), key=lambda t: t.cal_type):
            parts.append("|".join([
                "ts", cal.cal_type, str(cal.week_start), str(cal.fiscal_year_start_month),
                cal.compare_base_rule, cal.timezone,
            ]))
        self._semver_cache = fingerprint(parts)
        return self._semver_cache

    def object_versions(self) -> dict[str, int]:
        """每个语义对象的当前版本(写进 Trace 属性, 供"哪个口径被改过"回查)。"""
        out = {f"metric:{m.metric_id}": m.version for m in self.metrics.values()}
        out.update({f"dim:{d.dim_id}": d.version for d in self.dimensions.values()})
        return out

    # ---------------- 兼容性(细分表优先, 没灌则回到指标行上的名单) ----------------
    def compat_for(self, metric_id: str, dim_id: str) -> DimCompat | None:
        for item in self.compat:
            if item.metric_id == metric_id and item.dim_id == dim_id:
                return item
        return None

    def compatible_dim_ids(self, metric: Metric) -> frozenset[str]:
        """指标可下钻维度全集(名单 + 兼容表里标 ok/default_filter 的)。"""
        out = set(metric.compatible_dims)
        for item in self.compat:
            if item.metric_id == metric.metric_id and item.allowed:
                out.add(item.dim_id)
        return frozenset(out)

    def default_filter_for(self, metric_id: str, dim_id: str) -> dict[str, Any]:
        item = self.compat_for(metric_id, dim_id)
        if item is not None and item.compat == "default_filter":
            return dict(item.default_filter or {})
        return {}

    # ---------------- 时间语义 ----------------
    def time_semantic(self, metric: Metric | None = None) -> TimeSemantic:
        """按指标默认周期选日历(预算类走财年, 其他走自然年)。"""
        cal_type = "natural"
        if metric is not None and metric.calendar == "fiscal":
            cal_type = "fiscal"
        return self.time_semantics.get(cal_type) or TimeSemantic(cal_type=cal_type)

    # ---------------- 行列策略 ----------------
    def row_policy_for(self, role: str, table: str, metric_id: str = "") -> tuple[RowPolicy, ...]:
        """可用的谓词模板(角色匹配 + 表匹配 + 指标匹配), 按 priority 升序。"""
        hits = [
            p for p in self.row_policies
            if p.role in ("*", role or "") and p.table_name in ("*", table or "")
            and (not p.metric_id or p.metric_id == metric_id)
        ]
        return tuple(sorted(hits, key=lambda p: (p.priority, p.policy_id)))

    def denied_columns_for(self, role: str, table: str) -> frozenset[str]:
        out: set[str] = set()
        for policy in self.col_policies:
            if policy.table_name in ("*", table or "") and policy.role in ("*", role or ""):
                out |= set(policy.denied_cols)
        return frozenset(out)

    def get_metric(self, metric_id: str) -> Metric | None:
        metric = self.metrics.get(metric_id)
        return metric if metric and metric.status == "active" else None

    def get_dim(self, dim_id: str) -> Dimension | None:
        dim = self.dimensions.get(dim_id)
        return dim if dim and dim.status == "active" else None

    @property
    def active_metrics(self) -> list[Metric]:
        return [m for m in self.metrics.values() if m.status == "active"]

    @property
    def active_dimensions(self) -> list[Dimension]:
        return [d for d in self.dimensions.values() if d.status == "active"]

    def domains(self) -> list[str]:
        return sorted({m.domain for m in self.active_metrics if m.domain})

    def metrics_in_domain(self, domain: str) -> list[Metric]:
        key = (domain or "").strip().lower()
        if not key or key == "all":
            return self.active_metrics
        return [m for m in self.active_metrics if m.domain == key]

    def compatible_dims(self, metric_id: str) -> frozenset[str]:
        metric = self.get_metric(metric_id)
        return self.compatible_dim_ids(metric) if metric else frozenset()

    def dims_for_term(self, term: str) -> list[Dimension]:
        """按维度标签/别名反查维度(给澄清面板列候选用)。"""
        key = (term or "").strip()
        if not key:
            return []
        hits = [d for d in self.active_dimensions if d.label == key]
        return hits or [d for d in self.active_dimensions if key in d.label or d.label in key]

    # ---------------- prompt 渲染(单一事实源: 由快照展开, 不会与校验器漂移) ----------------
    def render_metrics(self, domain: str = "all", limit: int = 80) -> str:
        rows = self.metrics_in_domain(domain)[:limit]
        lines = []
        for m in rows:
            dims = ",".join(m.compatible_dims[:6]) or "-"
            lines.append(
                f"- {m.metric_id} | {m.label}({m.unit or '-'}) | 表 {m.source_table} | "
                f"聚合 {m.expression} | 可下钻 {dims}"
            )
        return "\n".join(lines) or "(语义层暂无可用指标)"

    def render_dimensions(self, limit: int = 60) -> str:
        lines = []
        for d in self.active_dimensions[:limit]:
            lines.append(f"- {d.dim_id} | {d.label} | 取值示例 {d.sample_values()}")
        return "\n".join(lines) or "(语义层暂无可用维度)"

    def to_payload(self) -> dict[str, Any]:
        """给前端/日志的紧凑视图(不含表达式, 只含人能看懂的字段)。"""
        return {
            "origin": self.origin,
            "semver": self.semver,
            "metric_count": len(self.active_metrics),
            "dimension_count": len(self.active_dimensions),
            "value_count": sum(len(d.dim_values) or len(d.enum_values) for d in self.active_dimensions),
            "compat_rules": len(self.compat),
            "row_policies": len(self.row_policies),
            "col_policies": len(self.col_policies),
            "missing_tables": list(self.missing),
            "domains": self.domains(),
        }
