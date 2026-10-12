"""golden 问法向量回填: 让自建评测集同时成为**语义缓存**的检索面。

为什么单独一个脚本而不是灌入时顺手 embed: embedding 走 Ollama(本系统最慢的一环),
`seed_askdata_golden` 的职责是"把标准答案算准", 把 500+ 次模型调用绑进同一步会让
它既慢又像挂掉(中途失败时既没答案也没向量)。这里只做一件事: 给缺向量的行补上。

用法::

    uv run python -m scripts.embed_askdata_golden            # 只补缺的
    uv run python -m scripts.embed_askdata_golden --force    # 全部重算(换 embedding 模型时必做)
    uv run python -m scripts.embed_askdata_golden --check    # 只看现状

前置: PostgreSQL 可达, Ollama 在跑且已 pull bge-m3(与 RAG/语义层同一把模型,
维度必须等于 EMBEDDING_DIM)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import text

from app.askdata import store
from app.config import get_settings
from app.rag.embeddings import OllamaEmbedder

_BATCH = 64


def _status() -> tuple[int, int]:
    from app.db import sync as dbsync

    with dbsync.get_sync_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT COUNT(*), COUNT(embedding) FROM askdata_golden WHERE enabled = true"
        )).first()
    return int(row[0] or 0), int(row[1] or 0)


async def _embed_batch(embedder: OllamaEmbedder, texts: list[str]) -> list[list[float]]:
    """一批问法一次 embed(与语义层回填同一口径: 失败抛出来, 不静默少写向量)。"""
    vectors = await embedder.embed(texts)
    if len(vectors) != len(texts):
        raise RuntimeError(f"向量条数与问法条数不一致: {len(vectors)} vs {len(texts)}")
    return [list(vector) for vector in vectors]


def _write(case_ids: list[str], vectors: list[list[float]]) -> int:
    from app.db import sync as dbsync

    written = 0
    with dbsync.get_sync_engine().begin() as conn:
        for case_id, vector in zip(case_ids, vectors):
            literal = "[" + ",".join(f"{float(x):.7f}" for x in vector) + "]"
            conn.execute(
                text("UPDATE askdata_golden SET embedding = CAST(:vec AS vector) "
                     "WHERE case_id = :cid"),
                {"vec": literal, "cid": case_id},
            )
            written += 1
    return written


async def run(force: bool) -> int:
    settings = get_settings()
    total, done = _status()
    print(f"目标库: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}  "
          f"模型: {settings.embedding_model} @ {settings.ollama_base_url}")
    print(f"当前: 启用用例 {total} 条, 已有向量 {done} 条")
    if force:
        with_from = _all_cases()
    else:
        with_from = store.golden_without_embedding(limit=20000)
    if not with_from:
        print("没有需要回填的用例(全部已有向量)。")
        return 0
    embedder = OllamaEmbedder()
    written = 0
    pending: list[dict[str, Any]] = []
    for index, item in enumerate(with_from, 1):
        pending.append(item)
        if len(pending) < _BATCH and index != len(with_from):
            continue
        texts = [str(row["question"]) for row in pending]
        try:
            vectors = await _embed_batch(embedder, texts)
        except Exception as exc:  # noqa: BLE001 - 一批失败只跳过这一批, 下一批继续
            print(f"  [跳过 {len(pending)} 条] embed 失败: {type(exc).__name__}: {str(exc)[:160]}")
            pending = []
            continue
        written += _write([str(row["case_id"]) for row in pending], vectors)
        print(f"  已回填 {written}/{len(with_from)}")
        pending = []
    total, done = _status()
    print(f"完成: 本次写入 {written} 条, 现在 {done}/{total} 条有向量")
    if done < total:
        print("提示: 仍有缺向量的行(多半是 Ollama 中途不可达), 重跑本脚本会继续补。")
    return 1 if done < total and force else 0


def _all_cases() -> list[dict[str, Any]]:
    from app.db import sync as dbsync

    with dbsync.get_sync_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT case_id, question FROM askdata_golden WHERE enabled = true ORDER BY case_id"
        )).all()
    return [dict(row._mapping) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填智能问数 golden 问法向量(语义缓存基底)")
    parser.add_argument("--force", action="store_true", help="全部重算(换 embedding 模型时用)")
    parser.add_argument("--check", action="store_true", help="只报告现状, 不写库")
    args = parser.parse_args(argv)
    if args.check:
        total, done = _status()
        print(f"启用用例 {total} 条, 有向量 {done} 条")
        return 0 if done >= total else 1
    return asyncio.run(run(force=bool(args.force)))


if __name__ == "__main__":
    sys.exit(main())
