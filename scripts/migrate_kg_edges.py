"""存量文档图谱边的迁移: 补溯源、归一关系词、清理无主实体。

**为什么必须先跑本脚本再看图**: 查询出口现在按 ``KG_REL.docs``(哪些文档断言过这条
关系)做边级 ACL, 没有任何可访问文档断言过的边一律不返回 —— 溯源上线前写入的历史边
``docs`` 是空的, 不回填就会整片隐身, 看起来像"图谱坏了"。

执行顺序:

    uv run python -m scripts.migrate_kg_edges --dry-run     # 先看清单, 不写库
    uv run python -m scripts.migrate_kg_edges --apply       # 回填 docs + 归一关系词
    uv run python -m scripts.build_doc_kg                   # 或页面「回填存量文档」按钮

三步分别做什么:

1. **回填 ``docs``**: 用"同一篇文档同时提及了这条边的两端实体"作为断言来源(共现启发)。
   共现不出来的边保持 ``docs = []``, 它们在图上不可见, 只能靠重抽重新建立溯源。
2. **关系词归一**: 按 :mod:`app.kg.vocab` 把"隶属于/归属于"并到"属于", 把写了互逆词
   ("包含/被依赖")的边翻成规范方向。先 MERGE 新边再删旧边, 中间没有丢数据的窗口。
3. **清理孤儿实体**: 重建后不再被任何文档提及的 ``KgEntity`` 是纯垃圾节点(它们没有
   MENTIONS 就没有任何溯源), 留着只会在图上挂空壳。
4. **摘除悬空溯源**: 把指向已删除文档的 ``docs`` 条目摘掉(摘空的边删除)。存量里这类
   条目一定存在: 旧版本 ``delete_document_graph`` 不会同步清理边的溯源。

``--wipe-relations`` 是最干净的路线: 直接删光 ``KG_REL`` 交给重抽, 适合存量本来就少、
又不想推理词归一是否翻错方向的场合。

``--reset-entities`` 多清一层: 连 ``KgEntity`` 一起删(它们与边一样全是 LLM 抽取的派生
数据, 文档节点与正文才是事实来源)。同一个名字在词表上线前可能被不同的文档存成了不同
类型(于是一个实体在图上裂成两个点), 在 Cypher 里搬边合并它们风险大于收益, 不如整体重导。

需先启用图谱(``DOC_KG_ENABLED=true``)且 Neo4j 可达; 不可用时打印提示后退出。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
from typing import Any

from app.config import get_settings
from app.kg import vocab, store
from app.memory.graph_store import get_driver


async def _fetch_relations(session) -> list[dict[str, Any]]:
    """读全部 KG_REL 边(带端点 elementId 与溯源), 归一判断在 Python 侧做。"""
    return await (
        await session.run(
            "MATCH (a:KgEntity)-[r:KG_REL]->(b:KgEntity) "
            "RETURN elementId(a) AS src, elementId(b) AS dst, "
            "       a.name AS src_name, a.type AS src_type, "
            "       b.name AS dst_name, b.type AS dst_type, "
            "       elementId(r) AS eid, r.relation AS relation, "
            "       r.docs AS docs, r.evidence AS evidence"
        )
    ).data()


async def _backfill_docs(session, *, apply: bool) -> tuple[int, int]:
    """给没有溯源的历史边回填 ``docs`` = 同时提及两端实体的文档集合。"""
    cypher = (
        "MATCH (a:KgEntity)-[r:KG_REL]->(b:KgEntity) "
        "WHERE r.docs IS NULL "
        "WITH r, "
        "     [(d:KgDoc)-[:MENTIONS]->(a) | d.doc_key] AS docs_a, "
        "     [(d:KgDoc)-[:MENTIONS]->(b) | d.doc_key] AS docs_b "
        "WITH r, [x IN docs_a WHERE x IN docs_b AND x IS NOT NULL] AS shared "
    )
    if apply:
        result = await session.run(cypher + "SET r.docs = shared RETURN count(r) AS n")
    else:
        result = await session.run(
            cypher + "RETURN size(shared) AS hit, count(r) AS n"
        )
    rows = await result.data()
    if apply:
        return int(rows[0]["n"] if rows else 0), 0
    filled = sum(int(r.get("n") or 0) for r in rows if int(r.get("hit") or 0) > 0)
    empty = sum(int(r.get("n") or 0) for r in rows if int(r.get("hit") or 0) == 0)
    return filled, empty


def _plan_rewrites(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """算出需要改写的边(关系词变了, 或互逆词要翻方向)。

    归一后多条旧边可能撞成同一个规范三元组: 按规范键先合并 ``docs``/``evidence``,
    落库时由 MERGE 并到同一条边上。每组区分两种旧边:

    - **目标边本身**(旧边的端点与关系词已经就是规范形式): 不能删, MERGE 命中的就是它;
    - **其余旧边**: 写完新边(或写完目标边)后按 elementId 精确删除。

      不这么区分就会出事: "A-属于->B"(已是规范)与"B-包含->A"(翻转后也是 A-属于->B)
      归到同一组, 假设把整组旧边全删, MERGE 刚合并好的那条会被一起删掉。
    """
    merged: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
    for row in rows:
        relation, flip = vocab.normalize_relation(row.get("relation"), enabled=True)
        src, dst = (row["dst"], row["src"]) if flip else (row["src"], row["dst"])
        src_name, dst_name = (
            (row["dst_name"], row["src_name"]) if flip else (row["src_name"], row["dst_name"])
        )
        src_type, dst_type = (
            (row["dst_type"], row["src_type"]) if flip else (row["src_type"], row["dst_type"])
        )
        is_target = (not flip) and relation == (row.get("relation") or "") and src == row["src"]
        docs = [d for d in (row.get("docs") or []) if d]
        key = (src, relation, dst)
        bucket = merged.get(key)
        if bucket is None:
            merged[key] = {
                "src": src,
                "dst": dst,
                "src_name": src_name,
                "src_type": src_type,
                "dst_name": dst_name,
                "dst_type": dst_type,
                "relation": relation,
                "docs": docs,
                "evidence": row.get("evidence") or "",
                "target_eid": row["eid"] if is_target else "",
                "delete_eids": [] if is_target else [row["eid"]],
            }
            continue
        bucket["docs"] = list(dict.fromkeys(bucket["docs"] + docs))
        if not bucket["evidence"] and row.get("evidence"):
            bucket["evidence"] = row["evidence"]
        if is_target:
            bucket["target_eid"] = row["eid"]
        else:
            bucket["delete_eids"].append(row["eid"])
    # 本来就是规范形式、且没有其他旧边汇入的组不需要任何改写, 直接跳过
    return [item for item in merged.values() if item["delete_eids"]]


async def _apply_rewrites(session, rewrites: list[dict[str, Any]]) -> int:
    """先 MERGE 规范边、再删旧边; 旧边删除按 elementId 精确命中, 不会误伤别的边。"""
    for item in rewrites:
        await session.run(
            "MATCH (a:KgEntity) WHERE elementId(a) = $src "
            "MATCH (b:KgEntity) WHERE elementId(b) = $dst "
            "MERGE (a)-[n:KG_REL {relation: $relation}]->(b) "
            "ON CREATE SET n.docs = $docs, n.evidence = $evidence, n.created_at = datetime() "
            "ON MATCH SET n.docs = [x IN coalesce(n.docs, []) WHERE NOT x IN $docs] + $docs, "
            "             n.evidence = CASE WHEN coalesce(n.evidence, '') = '' "
            "                                THEN $evidence ELSE n.evidence END",
            src=item["src"], dst=item["dst"], relation=item["relation"],
            docs=item["docs"], evidence=item["evidence"],
        )
        await session.run(
            "UNWIND $eids AS eid "
            "MATCH ()-[r:KG_REL]->() WHERE elementId(r) = eid DELETE r",
            eids=item["delete_eids"],
        )
    return len(rewrites)


async def _prune_orphans(session, *, apply: bool) -> int:
    """统计(并可选删除)不再被任何文档提及的实体节点。"""
    cypher = (
        "MATCH (e:KgEntity) WHERE NOT (:KgDoc)-[:MENTIONS]->(e) "
    )
    if apply:
        rows = await (await session.run(cypher + "WITH e DETACH DELETE e RETURN count(e) AS n")).data()
        return int(rows[0]["n"] if rows else 0)
    rows = await (await session.run(cypher + "RETURN count(e) AS n")).data()
    return int(rows[0]["n"] if rows else 0)


async def _prune_stale_provenance(session, *, apply: bool) -> tuple[int, int]:
    """摘掉指向已删除文档的溯源条目, 摘空的边删掉; 返回 ``(待摘边数, 待删边数)``。

    旧版本的删文档流程只删 ``KgDoc`` 节点, 不动边上的 ``docs``, 所以存量里一定会有悬空
    条目。这些边在边级 ACL 下本来就永远不可见(交集为空), 留着只是噪声。
    """
    matched = (
        "MATCH ()-[r:KG_REL]->() WHERE r.docs IS NOT NULL "
        "WITH r, [k IN r.docs WHERE EXISTS { MATCH (d:KgDoc {doc_key: k}) }] AS live "
        "WHERE size(live) <> size(r.docs) "
    )
    if apply:
        rows = await (await session.run(matched + "SET r.docs = live RETURN count(r) AS n")).data()
        pruned = int(rows[0]["n"] if rows else 0)
        dropped = await (
            await session.run(
                "MATCH ()-[r:KG_REL]->() WHERE r.docs IS NOT NULL AND r.docs = [] "
                "DELETE r RETURN count(r) AS n"
            )
        ).data()
        return pruned, int(dropped[0]["n"] if dropped else 0)
    rows = await (await session.run(matched + "RETURN count(r) AS n")).data()
    empty = await (
        await session.run(
            "MATCH ()-[r:KG_REL]->() WHERE r.docs IS NOT NULL AND r.docs = [] RETURN count(r) AS n"
        )
    ).data()
    return int(rows[0]["n"] if rows else 0), int(empty[0]["n"] if empty else 0)


async def _report(session, title: str) -> None:
    rows = await (
        await session.run(
            "MATCH ()-[r:KG_REL]->() "
            "RETURN count(r) AS edges, "
            "       count(DISTINCT r.relation) AS relations, "
            "       sum(CASE WHEN r.docs IS NULL OR r.docs = [] THEN 1 ELSE 0 END) AS unprovenanced"
        )
    ).data()
    row = rows[0] if rows else {}
    print(
        f"[kg-migrate] {title}: 边 {row.get('edges') or 0} 条 / "
        f"关系词 {row.get('relations') or 0} 种 / 无溯源 {row.get('unprovenanced') or 0} 条"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate document knowledge graph relations")
    dry = parser.add_mutually_exclusive_group()
    dry.add_argument("--dry-run", action="store_true", help="只统计并打印将要做的改动(默认)")
    dry.add_argument("--apply", action="store_true", help="真正写库")
    parser.add_argument(
        "--wipe-relations",
        action="store_true",
        help="只删 KG_REL 边, 交给重抽",
    )
    parser.add_argument(
        "--reset-entities",
        action="store_true",
        help="连 KgEntity 一并删掉(实体层全由重抽派生), 用于清除同名多型的裂点",
    )
    args = parser.parse_args()
    apply = bool(args.apply)
    if args.wipe_relations and args.reset_entities:
        raise SystemExit("--wipe-relations 与 --reset-entities 选一个: 后者已经包含前者的效果")

    settings = get_settings()
    if not settings.doc_kg_enabled:
        raise SystemExit("doc_kg_enabled=false: 请先设 DOC_KG_ENABLED=true 再运行本脚本")
    driver = get_driver()
    if driver is None:
        raise SystemExit("Neo4j 驱动不可用(连不上或未开启), 本脚本无事可做")

    await store.ensure_schema()
    async with driver.session() as session:
        if args.reset_entities:
            twins = await (
                await session.run(
                    "MATCH (e:KgEntity) WITH e.name AS n, collect(e.type) AS ts "
                    "WHERE size(ts) > 1 RETURN count(n) AS twins"
                )
            ).data()
            twin_count = int(twins[0]["twins"] if twins else 0)
            print(f"[kg-migrate] 同名多型的实体名: {twin_count} 个")
            if apply:
                rows = await (
                    await session.run("MATCH (e:KgEntity) DETACH DELETE e RETURN count(e) AS n")
                ).data()
                print(
                    f"[kg-migrate] 已删除 KgEntity {int(rows[0]['n'] if rows else 0)} 个"
                    "(与其上的 MENTIONS/KG_REL 一并消失)"
                )
                print("[kg-migrate] 必须接着重抽: uv run python -m scripts.build_doc_kg")
            else:
                print("[kg-migrate] dry-run: 加 --apply 才会真的删实体")
            return

        if args.wipe_relations:
            if apply:
                rows = await (await session.run("MATCH ()-[r:KG_REL]->() DELETE r RETURN count(r) AS n")).data()
                print(f"[kg-migrate] 已删除 KG_REL {int(rows[0]['n'] if rows else 0)} 条")
                print("[kg-migrate] 接着跑: uv run python -m scripts.build_doc_kg")
            else:
                await _report(session, "wipe 前")
                print("[kg-migrate] dry-run: 加 --apply 才会真的删边")
            return

        await _report(session, "迁移前")

        filled, empty = await _backfill_docs(session, apply=apply)
        print(
            f"[kg-migrate] docs 回填: 能按共现补上溯源 {filled} 条"
            + (f", 补不到(将隐身, 需重抽) {empty} 条" if not apply else "")
        )

        rows = await _fetch_relations(session)
        rewrites = _plan_rewrites(rows)
        print(
            f"[kg-migrate] 关系词归一: 需要改写/合并 {len(rewrites)} 组 "
            f"(涉及 {sum(len(r['delete_eids']) for r in rewrites)} 条旧边)"
        )
        for item in rewrites[:10 if not apply else 3]:
            print(
                f"    {item['src_name']}({item['src_type']}) "
                f"-{item['relation']}-> {item['dst_name']}({item['dst_type']})"
            )
        if apply and rewrites:
            done = await _apply_rewrites(session, rewrites)
            print(f"[kg-migrate] 已改写 {done} 组规范边并删除对应旧边")

        # 悬空溯源要先于孤儿实体: 否则带着悬空条目的边会因为端点实体被当成孤儿一并删掉,
        # 这一步就永远报 0, 看不出存量里到底有多少边在指向已删文档。
        stale_pruned, stale_dropped = await _prune_stale_provenance(session, apply=apply)
        print(
            f"[kg-migrate] 悬空溯源(指向已删文档): 需摘除条目 {stale_pruned} 条边"
            f", 摘空后应删 {stale_dropped} 条边" + ("已处理" if apply else "")
        )

        orphans = await _prune_orphans(session, apply=apply)
        print(f"[kg-migrate] 无 MENTIONS 的孤儿实体: {orphans} 个" + ("(已删除)" if apply else ""))

        await _report(session, "迁移后" if apply else "迁移前(dry-run 结束后不变)")
        if not apply:
            print("[kg-migrate] dry-run 完成: 确认无误后加 --apply 执行")
        else:
            print("[kg-migrate] 完成: 接着跑 uv run python -m scripts.build_doc_kg 让溯源与关系词真正对齐")


if __name__ == "__main__":
    asyncio.run(main())
