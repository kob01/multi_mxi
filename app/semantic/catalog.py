"""语义层目录(纯内存形状): 指标/维度/别名的只读快照。

为什么单独一个模块: 校验器与编译器都只认这个形状, 于是
- 有数据库时由 :mod:`app.semantic.registry` 从 ``sem_*`` 三表灌进来;
- 数据库不可用时由 :mod:`app.semantic.seed` 用内置集灌进来。
两条路径给校验器的是同一个对象, 不会出现"库挂了于是口径校验也一起没了"的降级空洞。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    description: str = ""
    status: str = "active"

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
            description=row.description or "",
            status=row.status or "active",
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
    time_like: bool = False
    description: str = ""
    status: str = "active"

    @property
    def qualified_column(self) -> str:
        """带前缀的列引用(多表 JOIN 时不写前缀会得到 ambiguous column)。"""
        if self.column_prefix:
            return f"{self.column_prefix}.{self.column}"
        return self.column

    def sample_values(self, limit: int = 6) -> str:
        return ", ".join(self.enum_values[:limit]) if self.enum_values else "-"

    @classmethod
    def from_row(cls, row: Any) -> "Dimension":
        values = row.enum_values or []
        return cls(
            dim_id=row.dim_id,
            label=row.label or row.dim_id,
            source_table=row.source_table or "",
            column=row.column or "",
            join_sql=row.join_sql or "",
            column_prefix=row.column_prefix or "",
            enum_source=row.enum_source or "static",
            enum_values=tuple(str(v) for v in values),
            time_like=bool(row.time_like),
            description=row.description or "",
            status=row.status or "active",
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
    """一次语义层快照(进程级缓存的单位; 构建成本 = 3 条 SELECT)。"""

    metrics: dict[str, Metric] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    terms: tuple[Term, ...] = ()
    # 快照来源与时间: 出问题时要能回答"这次用的是库里的版本还是内置兜底集"。
    origin: str = "db"
    built_at: float = 0.0

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
        return frozenset(metric.compatible_dims) if metric else frozenset()

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
            "metric_count": len(self.active_metrics),
            "dimension_count": len(self.active_dimensions),
            "domains": self.domains(),
        }
