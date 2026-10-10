"""语义层读取面: 把 ``sem_*`` 三表灌成 :class:`MetricCatalog` 快照(带 TTL 与兜底)。

只允许 askdata MCP server 进程调用(它按层 0 的分工持有数据库凭据);
AskData_Agent 进程不 import 本模块, 于是"agent 能读语义层"与"agent 能读业务数据"
一样都要经过 MCP 工具, 少一条私路。

三条口径:
1. **TTL 缓存**: 语义层是读多写极少的元数据, 每轮问数都现查三张表既慢又没意义;
   改了口径最多 ``askdata_semantic_ttl`` 秒生效(与 MCP 工具清单 TTL 同一形状)。
2. **兜底不等于降级可用**: 库连不上时退回内置集, 但快照里 ``origin="seed"`` 会一路
   带到回答里 —— 用户与运维要知道"现在用的是代码内置口径而不是库里治理过的口径"。
3. **不缓存调用者相关的作用域**: 本模块只缓存"定义", 谁能看哪些行永远由 SQL 执行时的
   RLS 决定, 不在这里做行级预过滤(那才会漏)。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.semantic.catalog import Dimension, Metric, MetricCatalog, Term
from app.semantic.seed import TOP_DIMS, TOP_METRICS, TERM_ALIASES, build_catalog

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: MetricCatalog | None = None
_CACHE_AT = 0.0

_METRIC_COLUMNS = (
    "metric_id", "label", "domain", "unit", "agg", "source_table", "expression",
    "caliber_where", "join_sql", "time_column", "compatible_dims", "calendar",
    "allowed_roles", "caliber_ref", "description", "status",
)
_DIM_COLUMNS = (
    "dim_id", "label", "source_table", "column", "join_sql", "column_prefix",
    "enum_source", "enum_values", "time_like", "description", "status",
)
# 手拼 text() SELECT 时 `column` 必须加引号: 它是 PG 的保留字, 裸写直接语法错。
# (ORM 自拼语句会自己引号, 所以只有这里需要分开写。)
_DIM_SELECT = tuple('"column"' if c == "column" else c for c in _DIM_COLUMNS)
_TERM_COLUMNS = ("term", "term_type", "target_id", "weight", "source", "status")


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
    engine = dbsync.get_sync_engine()
    with engine.connect() as conn:
        for row in conn.execute(text(f"SELECT {', '.join(_METRIC_COLUMNS)} FROM sem_metrics")):
            data = dict(row._mapping)
            data["compatible_dims"] = tuple(data.get("compatible_dims") or ())
            metric = Metric(**data)
            metrics[metric.metric_id] = metric
        for row in conn.execute(text(f"SELECT {', '.join(_DIM_SELECT)} FROM sem_dimensions")):
            data = dict(row._mapping)
            data["enum_values"] = tuple(data.get("enum_values") or ())
            dim = Dimension(**data)
            dims[dim.dim_id] = dim
        for row in conn.execute(text(f"SELECT {', '.join(_TERM_COLUMNS)} FROM sem_terms "
                                     "WHERE status = 'active'")):
            terms.append(Term(**dict(row._mapping)))
    if not metrics:
        # 空表说明还没灌语义层: 这不是"库坏了", 但用空目录会让问数一条都答不出来,
        # 所以退到内置集并把 origin 标成 seed, 让人能从回答里看出差异。
        raise RuntimeError("sem_metrics 为空(语义层尚未灌入)")
    return MetricCatalog(
        metrics=metrics, dimensions=dims, terms=tuple(terms),
        origin="db", built_at=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# 灌入(幂等): 只补"没登记过的定义", 不覆盖人工修订过的行, 也不动 embedding 列
# ---------------------------------------------------------------------------
def _metric_rows() -> list[dict[str, Any]]:
    return [{k: m[k] for k in _METRIC_COLUMNS} for m in TOP_METRICS]


def _dim_rows() -> list[dict[str, Any]]:
    return [{k: d[k] for k in _DIM_COLUMNS} for d in TOP_DIMS]


def _term_rows() -> list[dict[str, Any]]:
    return [
        {"term": t, "term_type": kind, "target_id": target, "weight": w,
         "source": "seed", "status": "active"}
        for t, kind, target, w in TERM_ALIASES
    ]


_TABLES = (("metrics", "sem_metrics"), ("dimensions", "sem_dimensions"), ("terms", "sem_terms"))


async def _async_counts() -> dict[str, int]:
    from app.db.session import get_engine

    out: dict[str, int] = {}
    async with get_engine().connect() as conn:
        for bucket, table in _TABLES:
            out[bucket] = int((await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one() or 0)
    return out


async def ensure_seeded() -> dict[str, int]:
    """把内置指标/维度/别名幂等写进语义层(表为空或首次上线时的一次性动作)。

    用 ``pg_insert(...).on_conflict_do_nothing()`` 而不是 merge: 已经被人改过口径的行
    绝不能被启动流程重刷回去(那会把治理成果当部署副作用覆盖掉)。冲突键:
    metrics/dims 用主键, terms 用 (term, term_type, target_id) 唯一约束。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.session import get_engine

    async with get_engine().connect() as conn:
        existing = int(
            (await conn.execute(text("SELECT COUNT(*) FROM sem_metrics"))).scalar_one() or 0
        )
    if existing:
        # 已经有指标: 不再逐条 ON CONFLICT(启动期白跑 168 条语句), 也不会有
        # "部署副作用把人工修订重刷回去"的风险。新指标要上线请跑 scripts/seed_semantic_layer。
        return {"metrics": 0, "dimensions": 0, "terms": 0, "skipped": existing}

    counts = {"metrics": 0, "dimensions": 0, "terms": 0}
    before = await _async_counts()
    async with get_engine().begin() as conn:
        # 注意括号: `await conn.execute(s).rowcount` 会先对协程对象取属性而报
        # 'coroutine' object has no attribute 'rowcount' —— 必须先把结果 await 出来。
        for raw in _metric_rows():
            stmt = pg_insert(MetricTable()).values(**raw).on_conflict_do_nothing(
                index_elements=["metric_id"]
            )
            await conn.execute(stmt)
        for raw in _dim_rows():
            stmt = pg_insert(DimensionTable()).values(**raw).on_conflict_do_nothing(
                index_elements=["dim_id"]
            )
            await conn.execute(stmt)
        for raw in _term_rows():
            stmt = pg_insert(TermTable()).values(**raw).on_conflict_do_nothing(
                index_elements=["term", "term_type", "target_id"]
            )
            await conn.execute(stmt)
    # 行数用 COUNT 差值而不是 rowcount: psycopg3 对 INSERT ... ON CONFLICT DO NOTHING
    # 返 -1(未知), 拿它当"插了几行"会算出 -52 这种负数结论。
    after = await _async_counts()
    counts = {bucket: max(0, after[bucket] - before[bucket]) for bucket in counts}
    logger.info("[askdata] 语义层灌入(仅新增): %s", counts)
    return counts


def seed_sync(force_definitions: bool = False) -> dict[str, int]:
    """同步版灌入(给 ``scripts/seed_semantic_layer.py`` 与部署后的一次性初始化用)。

    默认**只新增不改旧**: 有人可能在库里把某个口径调过, 部署流程不该顺手刷回去。
    代价是"代码里改了内置定义"不会自己进库, 所以另开一个显式开关
    ``force_definitions``: 它只按主键 UPDATE 内置集里有的行, 人工新增的 id 不碰。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import sync as dbsync

    engine = dbsync.get_sync_engine()
    plan = (
        ("metrics", MetricTable(), ["metric_id"], _metric_rows()),
        ("dimensions", DimensionTable(), ["dim_id"], _dim_rows()),
        ("terms", TermTable(), ["term", "term_type", "target_id"], _term_rows()),
    )
    before = _sync_counts(engine)
    with engine.begin() as conn:
        for _bucket, table, conflict, rows in plan:
            for raw in rows:
                stmt = pg_insert(table).values(**raw).on_conflict_do_nothing(
                    index_elements=conflict
                )
                conn.execute(stmt)
        if force_definitions:
            for _bucket, table, conflict, rows in plan:
                for raw in rows:
                    updates = {k: v for k, v in raw.items() if k not in conflict}
                    if not updates:
                        continue
                    conn.execute(
                        table.update()
                        .where(*[table.c[col] == raw[col] for col in conflict])
                        .values(**updates)
                    )
            logger.warning("[askdata] force_definitions: 内置集同名定义已按代码覆盖"
                           "(人工新增的 id 不受影响)")
    # 行数用前后 COUNT 差值: psycopg3 对 ON CONFLICT DO NOTHING 返 rowcount=-1。
    after = _sync_counts(engine)
    return {bucket: max(0, after[bucket] - before[bucket]) for bucket in before}


def _sync_counts(engine) -> dict[str, int]:
    """三张语义表的行数(用来算本次真正新增了几行)。"""
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for bucket, table in _TABLES:
            out[bucket] = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() or 0)
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


def MetricTable():  # noqa: N802 - 小写函数名会让人误以为它不做 I/O
    """延迟取 ORM 表对象(避免本模块在 import 期就把 models 拉进来)。"""
    from app.db.models import MetricDef

    return MetricDef.__table__


def DimensionTable():  # noqa: N802
    from app.db.models import DimensionDef

    return DimensionDef.__table__


def TermTable():  # noqa: N802
    from app.db.models import TermAlias

    return TermAlias.__table__
