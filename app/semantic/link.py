"""语义链接: 把"人话"接到语义层对象上(三路召回 + 融合打分)。

三路各自的性质不同, 所以都给分数而不是只给"命中/没命中":

1. **同义词典精确匹配**(score 1.0×weight): 确定性, 能覆盖 80% 的高频问法;
2. **向量召回**(score 上限 0.8): 处理没登记过的说法("报销花了多少" vs "费用发生额"),
   Ollama 不可用时这一路直接为空 —— 空不等于"没有候选", 精确路的结果仍然有效;
3. **枚举值反查**(score 0.9): "华北/研发部" 这类值反查它属于哪个维度, 直接生成过滤条件。
   枚举值靠库反查时**带着调用者作用域查**, 于是部门级角色看到的候选里不会长出
   别的部门的名字(那既是一次元数据泄露, 也会诱导模型造出越权的过滤条件)。

融合后仍不够确定就交给澄清门: 本模块**不代替用户做决定**, 只把候选与分数交出去。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text

from app.config import get_settings
from app.docs.normalize import normalize_text
from app.semantic.catalog import Dimension, Metric, MetricCatalog

logger = logging.getLogger(__name__)

SCORE_EXACT = 1.0
SCORE_ENUM = 0.9
VECTOR_CEILING = 0.8
MAX_ENUM_VALUES = 300
_ENUM_TTL = 300.0

_LOCK = threading.Lock()
_ENUM_CACHE: dict[str, tuple[float, list[str]]] = {}

# 标识符只允许库里真实存在的那种形状(与 app/db/policy.checked_ident 同口径)。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# 归一时保留词间单空格: 中文连续串里做子串匹配用不到空格, 但英文术语要留住。
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Candidate:
    """一条链接候选(指标/维度/枚举归属)。"""

    kind: str            # metric / dim / enum
    target_id: str
    label: str
    score: float
    channel: str         # exact / vector / enum
    caliber: str = ""
    matched: str = ""    # 命中的说法原文(长短包含判据用它)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.target_id,
            "label": self.label,
            "score": round(float(self.score), 3),
            "channel": self.channel,
            "caliber": self.caliber,
            # time_aware: 这个口径要不要时间区间(年度快照型不需要) —— 澄清门用它
            # 决定"没说时间"算不算缺失, 否则会对快照指标喷一句没意义的追问。
            "time_aware": bool(self.extra.get("time_aware", True)) if self.kind == "metric" else None,
            **({"value": self.extra.get("value")} if self.kind == "enum" else {}),
        }


@dataclass
class LinkResult:
    """链接阶段的完整产出(状态机与澄清面板都只吃这个形状)。"""

    candidates: list[Candidate] = field(default_factory=list)
    enum_filters: dict[str, str] = field(default_factory=dict)   # dim_id -> 命中的枚举值
    best_metric: Metric | None = None
    vector_ok: bool = False

    @property
    def metric_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates if c.kind == "metric"]

    @property
    def dim_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates if c.kind == "dim"]

    def to_payload(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.candidates]


def _norm(value: str) -> str:
    return _SPACE_RE.sub(" ", normalize_text(value or "")).strip().lower()


def link(
    query: str,
    *,
    catalog: MetricCatalog,
    scope: Any = None,
    top_n: int = 8,
    min_vector_similarity: float | None = None,
) -> LinkResult:
    """三路召回收敛成一份候选清单(按分数降序, 同一目标只保留最高分)。"""
    haystack = _norm(query)
    result = LinkResult()
    if not haystack:
        return result
    fused: dict[tuple[str, str], Candidate] = {}

    # ---------------- 路 1: 词典精确匹配(含指标名/维度名/别名) ----------------
    pairs: list[tuple[str, str, str, float]] = []  # (说法, kind, target_id, weight)
    for term in catalog.terms:
        if term.status == "active":
            pairs.append((term.term, term.term_type, term.target_id, term.weight))
    for metric in catalog.active_metrics:
        pairs.append((metric.label, "metric", metric.metric_id, 1.0))
    for dim in catalog.active_dimensions:
        pairs.append((dim.label, "dim", dim.dim_id, 0.95))

    # 长说法优先: "高风险合同数" 与 "合同" 同时命中时, 短的那条不该抢走位置。
    pairs.sort(key=lambda p: len(_norm(p[0])), reverse=True)
    for phrase, kind, target_id, weight in pairs:
        needle = _norm(phrase)
        if not needle or len(needle) < 2 or needle not in haystack:
            continue
        candidate = _build(kind, target_id, catalog, score=SCORE_EXACT * float(weight),
                          channel="exact", matched=phrase)
        if candidate is None:
            continue
        _absorb(fused, candidate)

    # ---------------- 路 2: 向量召回 sem_terms ----------------
    vector_candidates, vector_ok = _vector_channel(haystack, catalog, min_vector_similarity)
    result.vector_ok = vector_ok
    for candidate in vector_candidates:
        _absorb(fused, candidate)

    # ---------------- 路 3: 枚举值反查 ----------------
    for dim in catalog.active_dimensions:
        for value in _dim_values(dim, catalog, scope):
            needle = _norm(value)
            if len(needle) < 2 or needle not in haystack:
                continue
            candidate = Candidate(
                kind="enum", target_id=dim.dim_id, label=dim.label,
                score=SCORE_ENUM, channel="enum", extra={"value": value},
            )
            _absorb(fused, candidate)
            # 同一个维度命中多个值时保留先命中的(长的说法在前面已排过序)。
            result.enum_filters.setdefault(dim.dim_id, value)

    ranked = sorted(fused.values(), key=lambda c: (-c.score, -len(c.label), c.target_id))
    ranked = _drop_substring_matches(ranked)
    result.candidates = ranked[: max(1, top_n)]
    top_metric = next((c for c in result.candidates if c.kind == "metric"), None)
    if top_metric is not None and top_metric.score >= SCORE_EXACT * 0.95:
        result.best_metric = catalog.get_metric(top_metric.target_id)
    return result


def _absorb(fused: dict[tuple[str, str], Candidate], candidate: Candidate) -> None:
    key = (candidate.kind, candidate.target_id)
    current = fused.get(key)
    if current is None or candidate.score > current.score:
        fused[key] = candidate


def _drop_substring_matches(candidates: list[Candidate]) -> list[Candidate]:
    """最长匹配优先: 命中的说法是另一条已命中说法的子串时, 丢掉短的那条。

    不这样做会假出"两个口径都是 1.00, 分不清": 用户说"平均合同金额", 精确路
    同时也命中了它包着的"合同金额", 于是把一个本来很明确的说法判成了歧义。
    只作用在精确路(向量/枚举分本身不是 1.0, 不参与这个判据), 并且**只在同类型内**比:
    维度说法("部门")常常是指标说法("各部门报销额")的子串, 跨类型去重会把
    下钻维度错删掉 —— 那是另一个方向的错(静默丢维度比假歧义更难发现)。
    """
    dropped: set[tuple[str, str]] = set()
    for kind in {c.kind for c in candidates}:
        exact = [c for c in candidates if c.kind == kind and c.channel == "exact" and c.matched]
        if len(exact) < 2:
            continue
        for short in exact:
            for long in exact:
                if short is long:
                    continue
                a, b = _norm(short.matched), _norm(long.matched)
                if a and b and a != b and a in b:
                    dropped.add((short.kind, short.target_id))
                    break
    if not dropped:
        return candidates
    return [c for c in candidates if (c.kind, c.target_id) not in dropped]


def _build(kind: str, target_id: str, catalog: MetricCatalog, *, score: float, channel: str,
           matched: str = "") -> Candidate | None:
    if kind == "metric":
        metric = catalog.get_metric(target_id)
        if metric is None:
            return None
        return Candidate(
            kind="metric", target_id=metric.metric_id, label=metric.label, score=score,
            channel=channel, caliber=metric.caliber_note(), matched=matched,
            extra={"unit": metric.unit, "domain": metric.domain, "time_aware": metric.has_time},
        )
    if kind == "dim":
        dim = catalog.get_dim(target_id)
        if dim is None:
            return None
        return Candidate(kind="dim", target_id=dim.dim_id, label=dim.label, score=score,
                         channel=channel, matched=matched)
    return None


def _vector_channel(
    haystack: str, catalog: MetricCatalog, min_similarity: float | None
) -> tuple[list[Candidate], bool]:
    """pgvector cosine 命中 sem_terms(向量缺失或 Ollama 不可用时静默为空)。"""
    threshold = float(
        min_similarity
        if min_similarity is not None
        else get_settings().askdata_link_min_similarity
    )
    query_vector = _embed_query_sync(haystack)
    if not query_vector:
        return [], False
    from app.db import sync as dbsync

    literal = "[" + ",".join(f"{float(x):.7f}" for x in query_vector) + "]"
    sql = (
        "SELECT term, term_type, target_id, weight, 1 - (embedding <=> CAST(:q AS vector)) AS similarity "
        "FROM sem_terms WHERE embedding IS NOT NULL AND status = 'active' "
        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {"q": literal, "k": max(5, int(get_settings().askdata_link_top_k))}).all()
    except Exception as exc:  # noqa: BLE001 - 向量通道不可用只让这一路为空
        logger.warning("[askdata] 语义层向量召回不可用(降级为词典+枚举两路): %s", exc)
        return [], False
    out: list[Candidate] = []
    for row in rows:
        similarity = float(row._mapping.get("similarity") or 0.0)
        if similarity < threshold:
            continue
        weight = float(row._mapping.get("weight") or 1.0)
        candidate = _build(
            str(row._mapping.get("term_type") or ""), str(row._mapping.get("target_id") or ""),
            catalog, score=VECTOR_CEILING * similarity * weight, channel="vector",
        )
        if candidate is not None:
            out.append(candidate)
    return out, True


def _embed_query_sync(query: str) -> list[float]:
    """同步取查询向量(本模块的调用方是 FastMCP 的同步工具, 跑在 threadpool 里)。

    快速失败: 拿不到向量就返回空列表, 让链接退成"词典 + 枚举"两路而不是整条问数挂掉。
    """
    s = get_settings()
    payload = {"model": s.embedding_model, "input": [query]}
    try:
        with httpx.Client(timeout=httpx.Timeout(float(s.embedding_query_timeout), connect=3.0)) as client:
            resp = client.post(f"{s.ollama_base_url.rstrip('/')}/api/embed", json=payload)
            resp.raise_for_status()
        embeddings = resp.json().get("embeddings") or []
        if not embeddings:
            return []
        return [float(x) for x in embeddings[0]]
    except Exception as exc:  # noqa: BLE001 - 向量服务不可用不是问数的失败
        logger.warning("[askdata] 查询向量不可用(语义召回降级): %s", str(exc)[:160])
        return []


def _dim_values(dim: Dimension, catalog: MetricCatalog, scope: Any) -> list[str]:
    """维度的取值清单: 静态枚举直接用, 动态枚举按调用者作用域反查(带 TTL 缓存)。"""
    if dim.enum_values:
        return list(dim.enum_values)
    if not dim.source_table or not dim.column:
        return []
    dept_scope = dim.dim_id + "|" + _scope_key(scope)
    now = time.monotonic()
    with _LOCK:
        hit = _ENUM_CACHE.get(dept_scope)
        if hit and now - hit[0] <= _ENUM_TTL:
            return hit[1]
    values: list[str] = []
    try:
        values = _query_enum_values(dim, scope)
    except Exception as exc:  # noqa: BLE001 - 反查失败只是少一路召回
        logger.warning("[askdata] 维度 %s 枚举反查失败(跳过这一路): %s", dim.dim_id, exc)
    with _LOCK:
        _ENUM_CACHE[dept_scope] = (now, values)
        if len(_ENUM_CACHE) > 256:  # 有界: 作用域不会无限多, 撞上上限说明 key 形状在漂
            _ENUM_CACHE.clear()
    return values


def _scope_key(scope: Any) -> str:
    if scope is None:
        return "noscope"
    tenant = getattr(scope, "tenant_id", "") or ""
    text_fn = getattr(scope, "dept_scope_text", None)
    depts = text_fn() if callable(text_fn) else ""
    return f"{tenant}|{depts}"


def _query_enum_values(dim: Dimension, scope: Any) -> list[str]:
    """按作用域 DISTINCT 反查维度取值(切只读角色 + 注会话作用域, 与取数同一道闸)。

    没有作用域就直接不查: 默认拒而不是"查全量再筛" —— 后者会把别人部门的名字
    当成候选递到模型嘴边。
    """
    from app.db import sync as dbsync
    from app.db.ast_guard import READ_MODE, validate_sql
    from app.db.rls import read_role

    if scope is None:
        return []
    table, column, select_col, join = _enum_shape(dim)
    sql = (
        f"SELECT DISTINCT {select_col} AS v FROM {table} dx "
        + (f"{join} " if join else "")
        + f"WHERE {scope.scope_clause('dx')} AND {select_col} IS NOT NULL "
        f"AND {select_col} <> '' LIMIT {MAX_ENUM_VALUES}"
    )
    checked = validate_sql(sql, allowed_tables=_allowed(), mode=READ_MODE, normalize=False)
    payload, _scan = dbsync.execute_scoped_select(
        checked, scope.scope_params(), db_role=read_role(),
        session_settings=scope.session_settings(), max_scan_rows=None,
    )
    rows = payload[0]["rows"] if payload else []
    return [str(row.get("v")) for row in rows if row.get("v") is not None]


def _enum_shape(dim: Dimension) -> tuple[str, str, str, str]:
    """反查枚举所需的 ``(表, 列, 选定列, JOIN)``。

    部门类维度落在关联表上(hr_employees), 所以不能直接拼 ``dx.department`` ——
    需要把 seed 里按事实表别名写好的 JOIN 改写成本查询的 ``dx``, 并把取位列换成
    关联表前缀。拿不到正确形状就宁可不查(宁缺不猜)。
    """
    from app.semantic.seed import TABLE_ALIAS

    table = _checked_ident(dim.source_table, where="维度来源表")
    column = _checked_ident(dim.column, where="维度列")
    base = TABLE_ALIAS.get(dim.source_table, "")
    select_col = f"dx.{column}"
    join = dim.join_sql
    if join and base:
        join = join.replace(f"{base}.", "dx.")
    if dim.column_prefix and dim.column_prefix != base:
        prefix = _checked_ident(dim.column_prefix, where="维度列前缀")
        select_col = f"{prefix}.{column}"
        if not join:
            raise ValueError(f"维度 {dim.dim_id} 要关联表但没有登记 join")
    return table, column, select_col, join


def _checked_ident(name: str, *, where: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"{where} 不是合法标识符: {name!r}")
    return name


def _allowed() -> set[str]:
    from app.analytics.reports import ALLOWED_TABLES

    return set(ALLOWED_TABLES) | {"sem_terms", "sem_metrics", "sem_dimensions"}
