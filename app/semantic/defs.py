"""语义层真值面(YAML): 定义的可审查、可 diff、可回滚的那一份。

为什么要单独一份 YAML 而不是"代码内置集 + 库里随手改":
- **可 review**: 口径是一次业务决策, 它应该在 git diff 里看得见(谁把"已打款"从口径里
  去掉了), 而不是散落在某次 UPDATE 的 SQL 里;
- **可回滚**: 改坏了可以 revert 一个文件并重跑同步, 而不是凭记忆把 52 个表达式改回去;
- **CI 可校验**: 定义引用的 dim_id/表/列是否存在、谓词模板是否安全, 这些能在**不动库**
  的前提下判, 于是错误在合并前就爆。

真值方向是**单向**的: YAML -> 同步脚本 -> PG -> 运行时快照。库里允许人工微调(紧急热修),
但下一次同步会按 YAML 覆盖同名对象并留 sem_change_log —— 所以"临时改库"必须同时补 YAML,
否则它会在下一次同步时消失(这是有意的设计: 让口径的最终形态只有一个来源)。

本模块是纯函数(不碰数据库): 状态机侧与同步脚本都能 import 它, 不会把 DB 凭据带进 agent 容器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.semantic.seed import TERM_ALIASES, TOP_DIMS, TOP_METRICS

# 与 registry 的列名单保持一致(导出/导入都用同一套键, 避免两份字段清单漂移)。
METRIC_KEYS = (
    "metric_id", "label", "domain", "unit", "agg", "source_table", "expression",
    "caliber_where", "join_sql", "time_column", "compatible_dims", "calendar",
    "allowed_roles", "caliber_ref", "owner", "description", "status",
)
DIM_KEYS = (
    "dim_id", "label", "source_table", "column", "join_sql", "column_prefix",
    "enum_source", "enum_values", "hierarchy", "time_like", "description", "status",
)
TERM_KEYS = ("term", "term_type", "target_id", "weight", "source", "status")
VALUE_KEYS = ("dim_id", "value", "aliases", "popularity", "status")
COMPAT_KEYS = ("metric_id", "dim_id", "compat", "default_filter", "note")
TIME_KEYS = (
    "cal_type", "week_start", "fiscal_year_start_month", "compare_base_rule",
    "timezone", "description",
)
ROW_POLICY_KEYS = (
    "policy_id", "role", "table_name", "predicate", "metric_id", "priority", "enabled", "note",
)
COL_POLICY_KEYS = ("role", "table_name", "denied_cols", "enabled", "note")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# 策略谓词模板里允许的变量位: 只有这四个, 且都由服务端代入(值永不进 SQL 文本)。
_POLICY_VARS = ("{alias}", "{user_id}", "{dept_id}", "{tenant_id}", "{today}")
# 谓词模板里绝对不许出现的东西(多条语句/注释绕过/DDL): 载入时就拒, 不等编译。
_POLICY_BANNED = (";", "--", "/*", " into ", " copy ", " call ", " execute ")
_FORBIDDEN_COLS = ("tenant_id", "dept_id")  # 作用域列由服务端注入, 模板里出现即越权面


class DefsError(ValueError):
    """真值文件不合法(形状/引用/策略谓词), 必须在写库之前爆出来。"""


@dataclass
class DefsBundle:
    """一份完整的语义层定义(与数据库行同构的字典列表)。"""

    metrics: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    terms: list[dict[str, Any]] = field(default_factory=list)
    dim_values: list[dict[str, Any]] = field(default_factory=list)
    compat: list[dict[str, Any]] = field(default_factory=list)
    time_semantics: list[dict[str, Any]] = field(default_factory=list)
    row_policies: list[dict[str, Any]] = field(default_factory=list)
    col_policies: list[dict[str, Any]] = field(default_factory=list)

    def dim_ids(self) -> set[str]:
        return {str(d.get("dim_id") or "") for d in self.dimensions}

    def metric_ids(self) -> set[str]:
        return {str(m.get("metric_id") or "") for m in self.metrics}


# ---------------------------------------------------------------------------
# 由内置种子生成(第一次落 YAML 的路径; 也是库里/文件都不可用时的最终兜底)
# ---------------------------------------------------------------------------
def bundle_from_seed() -> DefsBundle:
    """把 :mod:`app.semantic.seed` 的内置集转成真值结构。

    兼容表(``compat``)从指标的 ``compatible_dims`` 派生: 第一版里它只是名单的表化,
    之后新增"不兼容但可默认过滤救回"的规则时才真正长出信息量。
    """
    bundle = DefsBundle()
    for raw in TOP_METRICS:
        item = {k: raw.get(k) for k in METRIC_KEYS}
        item.setdefault("owner", "")
        bundle.metrics.append(item)
        for dim_id in list(raw.get("compatible_dims") or []):
            bundle.compat.append({
                "metric_id": raw["metric_id"], "dim_id": dim_id, "compat": "ok",
                "default_filter": {}, "note": "由内置集 compatible_dims 派生",
            })
    for raw in TOP_DIMS:
        item = {k: raw.get(k) for k in DIM_KEYS}
        item.setdefault("hierarchy", [])
        # 刻意不把 ENUM_HINTS 当作真值写进去: 那四五个部门名是**无库时的兜底样本**,
        # 一旦进了 enum_values, 主数据型维度就不再按作用域反查(于是真部门列表里
        # 新成立的部门永远认不出来)。兜底集由 seed.build_catalog 自己叠加。
        bundle.dimensions.append(item)
        for value in list(item.get("enum_values") or []):
            bundle.dim_values.append({
                "dim_id": item["dim_id"], "value": value,
                "aliases": [], "popularity": 0, "status": "active",
            })
    for term, kind, target, weight in TERM_ALIASES:
        bundle.terms.append({
            "term": term, "term_type": kind, "target_id": target,
            "weight": float(weight), "source": "seed", "status": "active",
        })
    bundle.time_semantics = [
        {
            "cal_type": "natural", "week_start": 1, "fiscal_year_start_month": 1,
            "compare_base_rule": "same_calendar_day", "timezone": "Asia/Shanghai",
            "description": "自然年/自然月; 同比对齐同一日历日",
        },
        {
            "cal_type": "fiscal", "week_start": 1, "fiscal_year_start_month": 1,
            "compare_base_rule": "same_calendar_day", "timezone": "Asia/Shanghai",
            "description": "财年口径(本部署与自然年一致; 预算年度切换时改这一条)",
        },
    ]
    # 列级策略: 剔除的是"本就不该被同级别同事看到"的列, 不改任何已有口径的数字。
    bundle.col_policies = [
        {
            "role": "employee", "table_name": "hr_employees",
            "denied_cols": ["annual_leave_total", "annual_leave_used"], "enabled": True,
            "note": "员工角色不能问同事的年假额度(问得到就是越权读人); "
                    "在职人数/新入职人数这类聚合不受影响",
        },
    ]
    # 行级策略: 只放一条**默认关掉**的示例。为什么: 把员工可见范围从"本部门"收到
    # "本人"是一个业务决策而不是技术默认值, 预置启用等于用一个部署脚本替业务方拍口径。
    # 机制本身在编译器里是活的(启用的策略必然生效), 打开这一条就是改 YAML 里的一个 bool。
    bundle.row_policies = [
        {
            "policy_id": "rp_employee_own_reimbursements", "role": "employee",
            "table_name": "fin_reimbursements", "predicate": "{alias}.emp_id = {user_id}",
            "metric_id": "", "priority": 50, "enabled": False,
            "note": "打开后员工问报销只能得到本人单据(RLS 仍是底线, 这一层是叠加的收紧)",
        },
    ]
    return bundle


# ---------------------------------------------------------------------------
# 校验(不依赖数据库: 让"引用了不存在的维度"这种错在同步之前就爆)
# ---------------------------------------------------------------------------
def validate_bundle(bundle: DefsBundle) -> list[str]:
    """返回问题清单(空 = 合法)。刻意一次跑完而不是首错即返: 修 YAML 时要一次看全。"""
    problems: list[str] = []
    dims, metrics = bundle.dim_ids(), bundle.metric_ids()
    for metric in bundle.metrics:
        mid = str(metric.get("metric_id") or "")
        if not _IDENT_RE.match(mid):
            problems.append(f"metric_id 不是合法标识符: {mid!r}")
        if not str(metric.get("expression") or "").strip():
            problems.append(f"指标 {mid} 没有 expression(编译器无法聚合)")
        table = str(metric.get("source_table") or "")
        if table and not _IDENT_RE.match(table):
            problems.append(f"指标 {mid} 的 source_table 非法: {table!r}")
        for dim_id in list(metric.get("compatible_dims") or []):
            if dim_id not in dims:
                problems.append(f"指标 {mid} 引用了未登记的维度 {dim_id}")
    for dim in bundle.dimensions:
        did = str(dim.get("dim_id") or "")
        if not _IDENT_RE.match(did):
            problems.append(f"dim_id 不是合法标识符: {did!r}")
        for col in (str(dim.get("column") or ""), str(dim.get("source_table") or "")):
            if col and not _IDENT_RE.match(col):
                problems.append(f"维度 {did} 的标识符非法: {col!r}")
    seen_terms: set[tuple[str, str, str]] = set()
    for term in bundle.terms:
        key = (str(term.get("term")), str(term.get("term_type")), str(term.get("target_id")))
        if key in seen_terms:
            problems.append(f"别名重复: {key}")
        seen_terms.add(key)
        if not key[0]:
            problems.append("别名 term 为空")
        if key[1] == "metric" and key[2] not in metrics:
            problems.append(f"别名 {key[0]} 指向未登记的指标 {key[2]}")
        if key[1] == "dim" and key[2] not in dims:
            problems.append(f"别名 {key[0]} 指向未登记的维度 {key[2]}")
    for item in bundle.compat:
        if str(item.get("metric_id")) not in metrics:
            problems.append(f"兼容规则指向未登记的指标 {item.get('metric_id')}")
        if str(item.get("dim_id")) not in dims:
            problems.append(f"兼容规则指向未登记的维度 {item.get('dim_id')}")
        if str(item.get("compat") or "ok") not in ("ok", "no", "default_filter"):
            problems.append(f"兼容规则 compat 词表外: {item.get('compat')!r}")
    for value in bundle.dim_values:
        if str(value.get("dim_id")) not in dims:
            problems.append(f"枚举值指向未登记的维度 {value.get('dim_id')}")
        if not str(value.get("value") or "").strip():
            problems.append("枚举值 value 为空")
    for policy in bundle.row_policies:
        problems.extend(_check_predicate(policy))
    for policy in bundle.col_policies:
        role = str(policy.get("role") or "")
        if not role:
            problems.append("列策略缺 role")
        for col in list(policy.get("denied_cols") or []):
            if not _IDENT_RE.match(str(col)):
                problems.append(f"列策略 {role} 的列名非法: {col!r}")
            if str(col) in _FORBIDDEN_COLS:
                problems.append(
                    f"列策略 {role} 把作用域列 {col} 当作可剔除列: 它由服务端注入, 不在投影里"
                )
    for cal in bundle.time_semantics:
        if str(cal.get("cal_type")) not in ("natural", "fiscal"):
            problems.append(f"时间语义 cal_type 词表外: {cal.get('cal_type')!r}")
        if int(cal.get("week_start") or 1) not in (1, 7):
            problems.append("week_start 只支持 1(周一) 或 7(周日): PG date_trunc 的周起点是固定的")
    return problems


def _check_predicate(policy: dict[str, Any]) -> list[str]:
    """行策略谓词模板的静态安全检查(在写库之前, 不等编译期)。"""
    pid = str(policy.get("policy_id") or "?")
    predicate = str(policy.get("predicate") or "")
    lowered = " " + predicate.lower().replace("\n", " ") + " "
    problems: list[str] = []
    if not predicate.strip():
        problems.append(f"行策略 {pid} 谓词为空")
    for token in _POLICY_BANNED:
        if token in lowered:
            problems.append(f"行策略 {pid} 的谓词含禁止片段 {token.strip()!r}(只允许单条条件)")
    for col in _FORBIDDEN_COLS:
        # 作用域列由编译器注入, 模板里再写一次就是"两个地方判同一件事"。
        if re.search(rf"\b{col}\b", lowered):
            problems.append(f"行策略 {pid} 直接引用了作用域列 {col}, 应由 scope 注入而不是模板写")
    stripped = predicate
    for var in _POLICY_VARS:
        stripped = stripped.replace(var, "")
    for marker in re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", stripped):
        problems.append(f"行策略 {pid} 用了未知变量 {marker}(只允许 {', '.join(_POLICY_VARS)})")
    if "{alias}" not in predicate:
        problems.append(f"行策略 {pid} 的谓词没有用 {{alias}} 限定列: 多表 JOIN 下会得到 ambiguous column")
    return problems


# ---------------------------------------------------------------------------
# 读写(文件形状 = bundle 的键, 没有任何"只有数据库才知道"的东西)
# ---------------------------------------------------------------------------
_FILES = (
    ("metrics.yaml", "metrics", METRIC_KEYS),
    ("dimensions.yaml", "dimensions", DIM_KEYS),
    ("terms.yaml", "terms", TERM_KEYS),
    ("dim_values.yaml", "dim_values", VALUE_KEYS),
    ("compat.yaml", "compat", COMPAT_KEYS),
    ("time_semantics.yaml", "time_semantics", TIME_KEYS),
    ("policies.yaml", None, None),  # 一个文件两个面, 见下
)


def dump_bundle(bundle: DefsBundle, directory: Path) -> list[Path]:
    """写 YAML 真值文件(键顺序固定, 让 diff 只反映真正的定义变化)。"""
    import yaml

    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _rows(items: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        out = []
        for item in items:
            row = {}
            for key in keys:
                value = item.get(key)
                # 每行拷一份容器: 内置集里 `_EXPENSE_DIMS` 这种同一个 list 对象被多个指标共用,
                # 直接 dump 会写成 YAML 锚点(&id001/*id001), diff 里完全看不出谁改了维度名单。
                if isinstance(value, list):
                    value = list(value)
                elif isinstance(value, dict):
                    value = dict(value)
                # 布尔不参与"空字段剔除": `False in (None, "", [], {}, 0)` 在 Python 里是真的,
                # 拿它剔列会把 enabled: false(默认关掉的那条策略)洗成"没这个字段" -> 库里默认启用。
                if isinstance(value, bool):
                    row[key] = value
                    continue
                if value in (None, "", [], {}) or (value == 0 and key not in ("priority", "popularity")):
                    continue  # 空字段不落文件: 52 个指标里挂 300 个空键没人看得下去
                row[key] = value
            out.append(row)
        return out

    def _write(name: str, payload: dict[str, Any]) -> None:
        path = directory / name
        header = (
            "# 智能问数语义层真值(由 scripts/export_semantic_defs.py 生成/维护)\n"
            "# 改这里 = 改口径: 必须走 scripts/sync_semantic_defs.py 同步进库并留版本记录。\n"
        )
        path.write_text(
            header + yaml.safe_dump(
                payload, allow_unicode=True, sort_keys=False,
                # 宽度给大: 默认 80 列会折行普通量, 折行在中文上等于往句子里插一个空格
                # (yaml.safe_load 把换行读成一个空格), 于是 round-trip 不保真。
                width=4096,
            ),
            encoding="utf-8",
        )
        written.append(path)

    _write("metrics.yaml", {"metrics": _rows(bundle.metrics, METRIC_KEYS)})
    _write("dimensions.yaml", {"dimensions": _rows(bundle.dimensions, DIM_KEYS)})
    _write("terms.yaml", {"terms": _rows(bundle.terms, TERM_KEYS)})
    _write("dim_values.yaml", {"dim_values": _rows(bundle.dim_values, VALUE_KEYS)})
    _write("compat.yaml", {"compat": _rows(bundle.compat, COMPAT_KEYS)})
    _write("time_semantics.yaml", {"time_semantics": _rows(bundle.time_semantics, TIME_KEYS)})
    _write("policies.yaml", {
        "row_policies": _rows(bundle.row_policies, ROW_POLICY_KEYS),
        "col_policies": _rows(bundle.col_policies, COL_POLICY_KEYS),
    })
    return written


def load_bundle(directory: Path) -> DefsBundle:
    """读 YAML 真值文件(缺文件/缺键都按空处理, 但缺目录直接抛错)。"""
    import yaml

    directory = Path(directory)
    if not directory.is_dir():
        raise DefsError(f"语义层真值目录不存在: {directory}")

    def _read(name: str, key: str) -> list[dict[str, Any]]:
        path = directory / name
        if not path.is_file():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rows = data.get(key) or []
        if not isinstance(rows, list):
            raise DefsError(f"{name} 的 {key} 必须是列表")
        return [dict(row) for row in rows if isinstance(row, dict)]

    policies_path = directory / "policies.yaml"
    row_policies: list[dict[str, Any]] = []
    col_policies: list[dict[str, Any]] = []
    if policies_path.is_file():
        data = yaml.safe_load(policies_path.read_text(encoding="utf-8")) or {}
        row_policies = [dict(r) for r in (data.get("row_policies") or []) if isinstance(r, dict)]
        col_policies = [dict(r) for r in (data.get("col_policies") or []) if isinstance(r, dict)]
    bundle = DefsBundle(
        metrics=_read("metrics.yaml", "metrics"),
        dimensions=_read("dimensions.yaml", "dimensions"),
        terms=_read("terms.yaml", "terms"),
        dim_values=_read("dim_values.yaml", "dim_values"),
        compat=_read("compat.yaml", "compat"),
        time_semantics=_read("time_semantics.yaml", "time_semantics"),
        row_policies=row_policies,
        col_policies=col_policies,
    )
    problems = validate_bundle(bundle)
    if problems:
        raise DefsError("语义层真值不合法: " + " | ".join(problems[:12]))
    return bundle


def bundle_rows_for_table(bundle: DefsBundle, table: str) -> list[dict[str, Any]]:
    """按表名取回对应的行(同步脚本用, 避免那里再写一份名字到属性的映射)。"""
    return {
        "sem_metrics": bundle.metrics,
        "sem_dimensions": bundle.dimensions,
        "sem_terms": bundle.terms,
        "sem_dim_values": bundle.dim_values,
        "sem_metric_dim_compat": bundle.compat,
        "sem_time_semantics": bundle.time_semantics,
        "sem_row_policies": bundle.row_policies,
        "sem_col_policies": bundle.col_policies,
    }[table]
