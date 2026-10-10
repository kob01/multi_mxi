"""语义层灌入 + 向量回填(部署后跑一次, 新增指标后再跑一次)。

与启动期的 ``init_schema`` 自动灌入分工不同: 那一步只写**行**(保证没有库也能问数),
本脚本额外把别名/指标/维度的文本 embed 成 pgvector 向量 —— 向量这一路召回没有它就只能
靠词典精确匹配与枚举反查两路(表现为"换个说法就不认识了", 不报错, 所以必须显式跑一次)。

用法::

    uv run python -m scripts.seed_semantic_layer            # 灌行 + 只补空向量
    uv run python -m scripts.seed_semantic_layer --force     # 全部重算向量(改了口径文案时)
    uv run python -m scripts.seed_semantic_layer --sync-definitions  # 代码里改了内置口径时同步进库
    uv run python -m scripts.seed_semantic_layer --check      # 只看现状, 不写

前置: PostgreSQL 可达(宿主轨读 .env, 容器内用 ``docker exec`` 跑), Ollama 在跑且
已 pull bge-m3(embedding 与 RAG 同一把模型, 维度必须与 EMBEDDING_DIM 一致)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.rag.embeddings import OllamaEmbedder
from app.semantic import registry
from app.semantic.seed import TOP_DIMS, TOP_METRICS, TERM_ALIASES

_BATCH = 64


def _rows_status() -> dict[str, Any]:
    from app.db import sync as dbsync

    with dbsync.get_sync_engine().connect() as conn:
        def _one(sql: str) -> tuple[int, int]:
            row = conn.execute(text(sql)).first()
            return int(row[0] or 0), int(row[1] or 0)

        metrics_total, metrics_embedded = _one(
            "SELECT COUNT(*), COUNT(embedding) FROM sem_metrics")
        dims_total, dims_embedded = _one(
            "SELECT COUNT(*), COUNT(embedding) FROM sem_dimensions")
        terms_total, terms_embedded = _one(
            "SELECT COUNT(*), COUNT(embedding) FROM sem_terms")
        drafts = int(conn.execute(text(
            "SELECT COUNT(*) FROM sem_terms WHERE status = 'draft'"
        )).scalar_one() or 0)
    return {
        "sem_metrics": (metrics_total, metrics_embedded),
        "sem_dimensions": (dims_total, dims_embedded),
        "sem_terms": (terms_total, terms_embedded),
        "draft_terms": drafts,
    }


def _texts_for_metrics() -> list[tuple[str, str]]:
    return [(m["metric_id"], f"{m['label']} {m['unit']} {m['description']}") for m in TOP_METRICS]


def _texts_for_dims() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for d in TOP_DIMS:
        samples = ", ".join(str(v) for v in (d.get("enum_values") or [])[:8])
        out.append((d["dim_id"], f"{d['label']} {d.get('description', '')} 取值: {samples}".strip()))
    return out


def _texts_for_terms() -> list[tuple[str, str]]:
    # term 文本带上目标名, 让"报销花了多少"这种说法与目标指标在同一语义邻域。
    labels = {m["metric_id"]: m["label"] for m in TOP_METRICS}
    labels.update({d["dim_id"]: d["label"] for d in TOP_DIMS})
    return [(f"{term}\u0000{kind}\u0000{target}",
             f"{term}（{labels.get(target, target)}）")
            for term, kind, target, _w in TERM_ALIASES]


async def _embed(pairs: list[tuple[str, str]], force: bool, table: str, key_cols: tuple[str, ...]) -> int:
    """把 ``(键, 文本)`` embed 后写回 embedding 列; 返回写入行数。

    先算向量再开事务写: 把一次几十秒的批量 embedding 夹在事务里会长时间持锁,
    而这段代码跑在与其他容器同一个 PG 上。
    """
    from app.db import sync as dbsync

    if not pairs:
        return 0
    embedder = OllamaEmbedder()
    settings = get_settings()
    engine = dbsync.get_sync_engine()
    todo: list[list[str]] = []
    texts: list[str] = []
    with engine.connect() as conn:
        for key, item_text in pairs:
            values = list(key.split("\u0000")) if "\u0000" in key else [key]
            if not force and _has_embedding(conn, table, key_cols, values):
                continue
            todo.append(values)
            texts.append(item_text)
    if not texts:
        return 0
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH):
        vectors.extend(await embedder.embed(texts[start:start + _BATCH]))
    dim = int(settings.embedding_dim)
    written = 0
    with engine.begin() as conn:
        for values, vector in zip(todo, vectors):
            if len(vector) != dim:
                raise RuntimeError(
                    f"向量维度不匹配: 模型给出 {len(vector)} 维, EMBEDDING_DIM={dim} "
                    f"(换 embedding 模型必须同步改配置并全量重建向量列)"
                )
            literal = "[" + ",".join(f"{float(x):.7f}" for x in vector) + "]"
            where = " AND ".join(f"{col} = :k{i}" for i, col in enumerate(key_cols))
            params = {f"k{i}": v for i, v in enumerate(values)}
            params["vec"] = literal
            conn.execute(
                text(f"UPDATE {table} SET embedding = CAST(:vec AS vector) WHERE {where}"), params
            )
            written += 1
    return written


def _has_embedding(conn, table: str, key_cols: tuple[str, ...], values: list[str]) -> bool:
    where = " AND ".join(f"{col} = :k{i}" for i, col in enumerate(key_cols))
    sql = f"SELECT embedding IS NOT NULL FROM {table} WHERE {where}"  # noqa: S608 - 表/列名来自本文件常量
    row = conn.execute(text(sql), {f"k{i}": v for i, v in enumerate(values)}).first()
    return bool(row and row[0])


async def run(force: bool, sync_definitions: bool = False) -> int:
    settings = get_settings()
    print(f"目标库: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}  "
          f"模型: {settings.embedding_model} @ {settings.ollama_base_url}")
    inserted = registry.seed_sync(force_definitions=bool(sync_definitions))
    print(f"语义层行灌入(只新增{', 内置集同名定义按代码覆盖' if sync_definitions else ', 不覆盖人工修订'}): {inserted}")
    status = _rows_status()
    for table, value in status.items():
        if table == "draft_terms":
            continue
        total, embedded = value
        print(f"  {table}: {total} 行, 已有向量 {embedded}")
    if force:
        print("--force: 全部重算向量")
    else:
        print("只补空向量(改过口径文案要重算请加 --force)")
    try:
        n1 = await _embed(_texts_for_metrics(), force, "sem_metrics", ("metric_id",))
        n2 = await _embed(_texts_for_dims(), force, "sem_dimensions", ("dim_id",))
        n3 = await _embed(_texts_for_terms(), force, "sem_terms", ("term", "term_type", "target_id"))
    except Exception as exc:  # noqa: BLE001 - 把失败原因说清, 别让人去猜是库还是 Ollama 的问题
        print(f"\n向量回填失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("请确认: 1) Ollama 在跑且已 pull 该模型; 2) 数据库可达; "
              "3) 宿主轨 .env 的 PG_*/EMBEDDING_* 正确", file=sys.stderr)
        return 1
    print(f"向量写入: 指标 {n1} / 维度 {n2} / 别名 {n3}")
    after = _rows_status()
    print(f"回填后: {after}")
    registry.invalidate()
    print("提示: askdata-mcp 进程内的快照会在 TTL 后自动重读; 要立刻生效可重启该容器。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语义层灌入与向量回填")
    parser.add_argument("--force", action="store_true", help="全部重算向量(不只补空)")
    parser.add_argument("--sync-definitions", action="store_true",
                        help="按代码内置集覆盖同名指标/维度/别名定义(改了口径表达式时用); "
                             "人工新增的 id 不受影响")
    parser.add_argument("--check", action="store_true", help="只看现状, 不写库")
    args = parser.parse_args(argv)
    if args.check:
        for table, value in _rows_status().items():
            print(f"{table}: {value}")
        return 0
    return asyncio.run(run(force=bool(args.force), sync_definitions=bool(args.sync_definitions)))


if __name__ == "__main__":
    sys.exit(main())
