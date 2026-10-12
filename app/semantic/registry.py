"""语义层读取面: 把 ``sem_*`` 表灌成 :class:`MetricCatalog` 快照(带 TTL 与兜底)。

只允许 askdata MCP server 进程调用(它按层 0 的分工持有数据库凭据);
AskData_Agent 进程不 import 本模块, 于是"agent 能读语义层"与"agent 能读业务数据"
一样都要经过 MCP 工具, 少一条私路。

四条口径:
1. **TTL 缓存**: 语义层是读多写极少的元数据, 每轮问数都现查几张表既慢又没意义;
   改了口径最多 ``askdata_semantic_ttl`` 秒生效(与 MCP 工具清单 TTL 同一形状)。
2. **兜底不等于降级可用**: 库连不上时退回内置集, 但快照里 ``origin="seed"`` 会一路
   带到回答里 —— 用户与运维要知道"现在用的是代码内置口径而不是库里治理过的口径"。
3. **不缓存调用者相关的作用域**: 本模块只缓存"定义", 谁能看哪些行永远由 SQL 执行时的
   RLS 决定, 不在这里做行级预过滤(那才会漏)。
4. **细分面缺失不静默**: 枚举值/兼容规则/策略这几张表读不出来时快照仍然可用, 但
   会把表名记进 ``catalog.missing`` 并一路带到留痕 —— 策略层"没生效"与"生效但没命中"
   是两件完全不同的事, 看板上必须分得开。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.semantic.catalog import (
    ColPolicy,
    DimCompat,
    DimValue,
    Dimension,
    Metric,
    MetricCatalog,
    RowPolicy,
    Term,
    TimeSemantic,
)
from app.semantic.seed import build_catalog

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: MetricCatalog | None = None
_CACHE_AT = 0.0

_METRIC_COLUMNS = (
    "metric_id", "label", "domain", "unit", "agg", "source_table", "expression",
    "caliber_where", "join_sql", "time_column", "compatible_dims", "calendar",
    "allowed_roles", "caliber_ref", "owner", "description", "status", "version",
)
_DIM_COLUMNS = (
    "dim_id", "label", "source_table", "column", "join_sql", "column_prefix",
    "enum_source", "enum_values", "hierarchy", "time_like", "description", "status",
    "version",
)
# 手拼 text() SELECT 时 `column` 必须加引号: 它是 PG 的保留字, 裸写直接语法错。
# (ORM 自拼语句会自己引号, 所以只有这里需要分开写。)
_DIM_SELECT = tuple('"column"' if c == "column" else c for c in _DIM_COLUMNS)
_TERM_COLUMNS = ("term", "term_type", "target_id", "weight", "source", "status")
_VALUE_COLUMNS = ("dim_id", "value", "aliases", "popularity", "status")
_COMPAT_COLUMNS = ("metric_id", "dim_id", "compat", "default_filter", "note")
_TIME_COLUMNS = (
    "cal_type", "week_start", "fiscal_year_start_month", "compare_base_rule",
    "timezone", "description",
)
_ROW_POLICY_COLUMNS = (
    "policy_id", "role", "table_name", "predicate", "metric_id", "priority", "enabled", "note",
)
_COL_POLICY_COLUMNS = ("role", "table_name", "denied_cols", "enabled", "note")


def _ttl() -> float:
    return max(1.0, float(get_settings().askdata_semantic_ttl or 300))


def get_catalog(force_refresh: bool = False) -> MetricCatalog:
    """取当前语义层快照(过期则重读; 读库失败退回内置集并留 WARNING)。"""
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    with _LOCK:
        if _CACHE is not None and not force_refresh and now - _CACHE_AT <= _ttl():
            return _CACHE
        catalog = _CACHE
        try:
            catalog = _load_from_db()
            _CACHE_AT = now
            logger.info("[askdata] 语义层快照已刷新(db): %d 指标 / %d 维度",
                        len(catalog.active_metrics), len(catalog.active_dimensions))
        except Exception as exc:  # noqa: BLE001 - 语义层读不出来时用内置口径, 但不能当成库里的版本
            if catalog is None:
                catalog = build_catalog(origin="seed", built_at=now)
            logger.warning(
                "[askdata] 语义层读取失败, 本轮使用%s口径(origin=%s): %s",
                "缓存的" if _CACHE is not None else "代码内置的", catalog.origin, exc,
            )
        _CACHE = catalog
        return catalog


def invalidate() -> None:
    """让下一次读取强制重算(语义层被修订/补词后调用, 不等 TTL)。"""
    global _CACHE, _CACHE_AT
    with _LOCK:
        _CACHE = None
        _CACHE_AT = 0.0


def _load_from_db() -> MetricCatalog:
    from app.db import sync as dbsync

    metrics: dict[str, Metric] = {}
    dims: dict[str, Dimension] = {}
    terms: list[Term] = []
    compat: list[DimCompat] = []
    time_semantics: dict[str, TimeSemantic] = {}
    row_policies: list[RowPolicy] = []
    col_policies: list[ColPolicy] = []
    values_by_dim: dict[str, list[DimValue]] = {}
    missing: list[str] = []
    engine = dbsync.get_sync_engine()
    with engine.connect() as conn:
        for row in conn.execute(text(f"SELECT {', '.join(_METRIC_COLUMNS)} FROM sem_metrics")):
            data = dict(row._mapping)
            data["compatible_dims"] = tuple(data.get("compatible_dims") or ())
            data["version"] = int(data.get("version") or 1)
            metric = Metric(**data)
            metrics[metric.metric_id] = metric
        for row in conn.execute(text(f"SELECT {', '.join(_DIM_SELECT)} FROM sem_dimensions")):
            data = dict(row._mapping)
            data["enum_values"] = tuple(data.get("enum_values") or ())
            data["hierarchy"] = tuple(data.get("hierarchy") or ())
            data["version"] = int(data.get("version") or 1)
            dim = Dimension(**data)
            dims[dim.dim_id] = dim
        for row in conn.execute(text(f"SELECT {', '.join(_TERM_COLUMNS)} FROM sem_terms "
                                     "WHERE status = 'active'")):
            terms.append(Term(**dict(row._mapping)))
        # 细分面逐表独立兜: 一张表读失败(或行形状不对)只影响那一路信息, 不能把整个
        # 快照拖回内置集(那会把库里已治理的口径换成代码内置的, 一个迁移中的小错
        # 造成一次大降级)。注意**转换也在 try 里**: 只包 SELECT 的话, 一个 dataclass
        # 字段不匹配会一路抛穿到 get_catalog, 把"只缺一张表"放大成"全退到兜底集"。
        for table, columns, extra in (
            ("sem_dim_values", _VALUE_COLUMNS, "WHERE status = 'active'"),
            ("sem_metric_dim_compat", _COMPAT_COLUMNS, ""),
            ("sem_time_semantics", _TIME_COLUMNS, ""),
            ("sem_row_policies", _ROW_POLICY_COLUMNS, "WHERE enabled = true"),
            ("sem_col_policies", _COL_POLICY_COLUMNS, "WHERE enabled = true"),
        ):
            clause = f" {extra}" if extra else ""
            bucket_values: dict[str, list[DimValue]] = {}
            bucket_compat: list[DimCompat] = []
            bucket_time: dict[str, TimeSemantic] = {}
            bucket_row: list[RowPolicy] = []
            bucket_col: list[ColPolicy] = []
            try:
                rows = conn.execute(text(
                    f"SELECT {', '.join(columns)} FROM {table}{clause}"  # noqa: S608
                )).all()
                if table == "sem_dim_values":
                    for row in rows:
                        data = dict(row._mapping)
                        data["aliases"] = tuple(data.get("aliases") or ())
                        data["popularity"] = int(data.get("popularity") or 0)
                        # status 只用来筛行(SELECT 已 WHERE status='active'), 快照形状不带它。
                        data.pop("status", None)
                        bucket_values.setdefault(str(data["dim_id"]), []).append(DimValue(**data))
                elif table == "sem_metric_dim_compat":
                    for row in rows:
                        bucket_compat.append(DimCompat(**dict(row._mapping)))
                elif table == "sem_time_semantics":
                    for row in rows:
                        item = TimeSemantic(**dict(row._mapping))
                        bucket_time[item.cal_type] = item
                elif table == "sem_row_policies":
                    for row in rows:
                        data = dict(row._mapping)
                        data["priority"] = int(data.get("priority") or 100)
                        # enabled 同上: 只用来筛行, RowPolicy 形状里没有它。
                        data.pop("enabled", None)
                        bucket_row.append(RowPolicy(**data))
                else:
                    for row in rows:
                        data = dict(row._mapping)
                        data["denied_cols"] = tuple(data.get("denied_cols") or ())
                        data.pop("enabled", None)
                        bucket_col.append(ColPolicy(**data))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[askdata] 语义层细分表 %s 读不出/形状不合(本轮快照不带这一面): %s",
                               table, exc)
                missing.append(table)
                continue
            # 只有整张表都转换成功才合进快照(留一半比留零个更容易骗人)。
            for dim_id, items in bucket_values.items():
                values_by_dim.setdefault(dim_id, []).extend(items)
            compat.extend(bucket_compat)
            time_semantics.update(bucket_time)
            row_policies.extend(bucket_row)
            col_policies.extend(bucket_col)
    if not metrics:
        # 空表说明还没灌语义层: 这不是"库坏了", 但用空目录会让问数一条都答不出来,
        # 所以退到内置集并把 origin 标成 seed, 让人能从回答里看出差异。
        raise RuntimeError("sem_metrics 为空(语义层尚未灌入)")
    for dim_id, values in values_by_dim.items():
        dim = dims.get(dim_id)
        if dim is None:
            continue
        dims[dim_id] = Dimension(
            **{**dim.__dict__, "dim_values": tuple(values)}
        )
    return MetricCatalog(
        metrics=metrics, dimensions=dims, terms=tuple(terms),
        compat=tuple(compat), time_semantics=time_semantics,
        row_policies=tuple(row_policies), col_policies=tuple(col_policies),
        missing=tuple(missing),
        origin="db", built_at=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# 灌入(幂等): 真值来自 YAML(没有真值文件时退到内置种子), 只补"没登记过的定义"
# ---------------------------------------------------------------------------
def defs_dir() -> Path:
    """语义层真值目录(相对路径按仓根解析: app/semantic/registry.py 往上三层)。"""
    raw = (get_settings().semantic_defs_dir or "").strip()
    if not raw:
        return Path()
    path = Path(raw)
    return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path


def load_definitions():
    """取要灌进库的定义集: YAML 真值优先, 没目录/没装 pyyaml 时退内置种子。

    退种子不是降级: 两条路径的内容同一个源头(seed.py 就是第一版 YAML 的生成源),
    只是少了人在 YAML 上的后续修订。退的时候必顶录一条 INFO, 因为"改7个 YAML 不生效"
    的第一排查项就是目录路径配错了。
    """
    from app.semantic import defs as semantic_defs

    directory = defs_dir()
    if directory.is_dir():
        try:
            return semantic_defs.load_bundle(directory), f"yaml:{directory.name}"
        except Exception as exc:  # noqa: BLE001 - 真值文件本身不合法不能静默插库
            logger.error("[askdata] 语义层真值目录 %s 读不出/不合法, 本轮退内置种子: %s", directory, exc)
    return semantic_defs.bundle_from_seed(), "seed"


# ``(入库名, 表名, 行来源属性, 列名单, 冲突键)``: 一张表一份声明, 不再每表写一段循环。
_TABLE_SPECS = (
    ("metrics", "sem_metrics", "metrics", _METRIC_COLUMNS, ("metric_id",)),
    ("dimensions", "sem_dimensions", "dimensions", _DIM_COLUMNS, ("dim_id",)),
    ("terms", "sem_terms", "terms", _TERM_COLUMNS, ("term", "term_type", "target_id")),
    ("dim_values", "sem_dim_values", "dim_values", _VALUE_COLUMNS, ("dim_id", "value")),
    ("compat", "sem_metric_dim_compat", "compat", _COMPAT_COLUMNS, ("metric_id", "dim_id")),
    ("time_semantics", "sem_time_semantics", "time_semantics", _TIME_COLUMNS, ("cal_type",)),
    ("row_policies", "sem_row_policies", "row_policies", _ROW_POLICY_COLUMNS, ("policy_id",)),
    ("col_policies", "sem_col_policies", "col_policies", _COL_POLICY_COLUMNS, ("role", "table_name")),
)


def _rows_for(bundle, attribute: str, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    """把真值行剪成"只有这张表认识的列"(YAML 里多写的字段不能带进 INSERT, 否则直接报错)。"""
    out: list[dict[str, Any]] = []
    for item in getattr(bundle, attribute):
        row = {key: item.get(key) for key in columns if key in item}
        if not row:
            continue
        out.append(row)
    return out


async def _async_counts() -> dict[str, int]:
    from app.db.session import get_engine

    out: dict[str, int] = {}
    async with get_engine().connect() as conn:
        for _name, table, _attr, _cols, _keys in _TABLE_SPECS:
            out[table] = int((await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one() or 0)
    return out


async def ensure_seeded() -> dict[str, int]:
    """把语义层定义幂等写进库(每张表只在"自己为空"时跑一次)。

    用 ``pg_insert(...).on_conflict_do_nothing()`` 而不是 merge: 已经被人改过口径的行
    绝不能被启动流程重刷回去(那会把治理成果当部署副作用覆盖掉)。
    逐表判空而不是只看 sem_metrics: 存量库升级时老三表有行而新细分表为空,
    一次性启动逻辑必须能把新表灌上(不靠"顺便有人跑过脚本")。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.session import get_engine

    bundle, source = load_definitions()
    counts: dict[str, int] = {}
    before = await _async_counts()
    async with get_engine().begin() as conn:
        for _name, table, attribute, columns, conflict in _TABLE_SPECS:
            if before.get(table):
                continue  # 已经有行: 启动流程不碰(要覆盖请显跑 scripts/sync_semantic_defs)
            for raw in _rows_for(bundle, attribute, columns):
                stmt = pg_insert(table_obj(table)).values(**raw).on_conflict_do_nothing(
                    index_elements=list(conflict)
                )
                await conn.execute(stmt)
    # 行数用 COUNT 差值而不是 rowcount: psycopg3 对 INSERT ... ON CONFLICT DO NOTHING
    # 返 -1(未知), 拿它当"插了几行"会算出 -52 这种负数结论。
    after = await _async_counts()
    counts = {table: max(0, after[table] - before[table]) for table in after if after[table] != before[table]}
    if counts:
        logger.info("[askdata] 语义层灌入(来=%s, 仅新增): %s", source, counts)
    return counts


def seed_sync(force_definitions: bool = False) -> dict[str, int]:
    """同步版灌入(给部署后的一次性初始化与脚本用)。

    默认**只新增不改旧**: 有人可能在库里把某个口径调过, 部署流程不该顺手刷回去。
    代价是"真值里改了定义"不会自己进库, 所以另开一个显式开关 ``force_definitions``:
    它只按主键 UPDATE 真值里有的行(人工新增的 id 不碰); 但正式路径是
    ``scripts/sync_semantic_defs.py``(它带版本递增与 sem_change_log)。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import sync as dbsync

    bundle, source = load_definitions()
    engine = dbsync.get_sync_engine()
    plan = (
        (table, table_obj(table), keys, _rows_for(bundle, attribute, columns))
        for _name, table, attribute, columns, keys in _TABLE_SPECS
    )
    before = _sync_counts(engine)
    with engine.begin() as conn:
        for _table_name, table, conflict, rows in plan:
            for raw in rows:
                stmt = pg_insert(table).values(**raw).on_conflict_do_nothing(
                    index_elements=list(conflict)
                )
                conn.execute(stmt)
        if force_definitions:
            for _table_name, table, conflict, rows in plan:
                for raw in rows:
                    updates = {k: v for k, v in raw.items() if k not in conflict}
                    if not updates:
                        continue
                    conn.execute(
                        table.update()
                        .where(*[table.c[col] == raw[col] for col in conflict])
                        .values(**updates)
                    )
            logger.warning("[askdata] force_definitions: 真值同名定义已按文件覆盖"
                           "(人工新增的 id 不受影响; 来源=%s)", source)
    # 行数用前后 COUNT 差值: psycopg3 对 ON CONFLICT DO NOTHING 返 rowcount=-1。
    after = _sync_counts(engine)
    return {table: max(0, after[table] - before[table]) for table in before}


def _sync_counts(engine) -> dict[str, int]:
    """语义层各表的行数(用来算本次真正新增了几行)。"""
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for _name, table, _attr, _cols, _keys in _TABLE_SPECS:
            out[table] = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() or 0)
    return out


def semantic_is_empty() -> bool:
    """语义层是否还没灌过(启动时用来决定要不要跑一次灌入)。"""
    from app.db import sync as dbsync

    try:
        with dbsync.get_sync_engine().connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM sem_metrics")).scalar_one() or 0) == 0
    except Exception as exc:  # noqa: BLE001 - 查不出来时按"已灌过"处理, 不做启动期写操作
        logger.warning("[askdata] 语义层存在性检查失败(跳过自动灌入): %s", exc)
        return False


def table_obj(name: str):
    """按表名取 ORM 表对象(延迟 import: 避免本模块在 import 期就把 models 拉进来)。

    公开给同步脚本用: 真值同步需要按表名做 upsert, 而"哪些表属于语义层"这件事
    只应该在本模块列一份(再列一份在脚本里就会漂移)。
    """
    from app.db import models

    return {
        "sem_metrics": models.MetricDef,
        "sem_dimensions": models.DimensionDef,
        "sem_terms": models.TermAlias,
        "sem_dim_values": models.DimValueDef,
        "sem_metric_dim_compat": models.MetricDimCompatDef,
        "sem_time_semantics": models.TimeSemanticDef,
        "sem_row_policies": models.RowPolicyDef,
        "sem_col_policies": models.ColPolicyDef,
    }[name].__table__
