"""文档知识图谱的 Neo4j 读写层(全新标签命名空间, 与 :MemoryEntity 隔离)。

节点/关系设计(全局知识库级, 不按 user_id 分区):

    (:KgDoc {doc_key, name, ext, modality, updated_at})
    (:KgEntity {name, type})
    (:KgDoc)-[:MENTIONS]->(:KgEntity)
    (:KgEntity)-[:KG_REL {relation, docs, evidence, created_at, last_seen_at}]->(:KgEntity)

``KG_REL.docs`` 存"哪些文档断言了这条关系", 它是两个能力的前提:

- **边级 ACL**: 无权文档断言的关系不能出现在有权用户的子图里(否则只要两端实体被
  任一有权文档提及过, 这条边的存在性就泄露了), 查询出口按 ``docs`` 与可访问集合求交裁剪;
- **随篇回收**: 重入库/重抽时把本篇从 ``docs`` 里摘掉, 摘空的边直接删, 边才能与
  本篇的最新抽取结果一致(旧写法只重清 MENTIONS 出边, KG_REL 只增不减)。

实体身份与关系词全部先过 :mod:`app.kg.vocab` 再落库: 同名多型会笛卡尔错连,
不受控的关系词会在同一对实体上挂出多条几何重合的重复边。

复用 ``app.memory.graph_store.get_driver()`` 的驱动单例; 驱动不可用(未开启/连不上)
时所有方法静默返回空/不写入并只记 WARNING —— 与图记忆完全一致的降级策略。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.config import get_settings
from app.kg import vocab
from app.memory.graph_store import get_driver

logger = logging.getLogger(__name__)

_kg_schema_ready = False


async def ensure_schema() -> None:
    """幂等建 KgDoc/KgEntity 的唯一约束与索引; 失败只 WARNING, 不抛出。"""
    global _kg_schema_ready
    driver = get_driver()
    if driver is None or _kg_schema_ready:
        return
    try:
        async with driver.session() as session:
            await session.run(
                "CREATE CONSTRAINT kgdoc_key_unique IF NOT EXISTS "
                "FOR (d:KgDoc) REQUIRE d.doc_key IS UNIQUE"
            )
            await session.run(
                "CREATE CONSTRAINT kgentity_unique IF NOT EXISTS "
                "FOR (e:KgEntity) REQUIRE (e.name, e.type) IS UNIQUE"
            )
            await session.run(
                "CREATE INDEX kgentity_name IF NOT EXISTS FOR (e:KgEntity) ON (e.name)"
            )
            # 不另外建 (name, type) 组合索引: ``kgentity_unique`` 已经包了这一层,
            # 再建一次只有一个"has no effect"的 NOTICE(实测刷在启动日志里)。
            # 关系词分布统计/迁移脚本按 relation 筛边时需要这个索引
            await session.run(
                "CREATE INDEX kgrel_relation IF NOT EXISTS FOR ()-[r:KG_REL]-() ON (r.relation)"
            )
        _kg_schema_ready = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j 文档图谱 schema 初始化失败, 图谱功能降级为不可用: %s", exc)


async def upsert_document_graph(
    doc_key: str,
    name: str,
    ext: str,
    modality: str,
    entities: Sequence[dict],
    relations: Sequence[dict],
) -> bool:
    """写入/合并一篇文档的图谱, 全部 MERGE 语义; 重入库先清旧出边保证幂等。

    返回是否真正执行了写入(驱动不可用时返回 False)。

    关系行必须带 ``src_type``/``dst_type``: 只按 name MATCH 会在同名多型实体上连出
    笛卡尔积错边。调用方(extract)已经给齐了类型, 这里再做一次兜底归一。

    本轮不再断言的旧边会随篇回收(从 ``docs`` 摘掉本篇, 摘空即删); 但抽取**完全为空**
    时 service 不会调用本函数 —— 那是模型常见故障形状, 不能因为一次抽取失败就把
    这篇已有的图谱清空(需要硬清走 /documents/{doc_key}/rebuild 或迁移脚本)。
    """
    if not doc_key:
        return False
    driver = get_driver()
    if driver is None:
        return False
    vocab_on = bool(get_settings().kg_relation_vocab_enabled)
    entity_rows = [
        {
            "name": vocab.normalize_entity_name(e.get("name")),
            "type": vocab.normalize_entity_type(e.get("type")),
        }
        for e in entities
        if isinstance(e, dict) and vocab.normalize_entity_name(e.get("name"))
    ]
    relation_rows: list[dict] = []
    for r in relations:
        if not isinstance(r, dict):
            continue
        src = vocab.normalize_entity_name(r.get("src"))
        dst = vocab.normalize_entity_name(r.get("dst"))
        if not src or not dst or src == dst:
            continue
        relation, flip = vocab.normalize_relation(r.get("relation"), enabled=vocab_on)
        src_type = vocab.normalize_entity_type(r.get("src_type"))
        dst_type = vocab.normalize_entity_type(r.get("dst_type"))
        if flip:
            src, dst = dst, src
            src_type, dst_type = dst_type, src_type
        relation_rows.append(
            {
                "src": src,
                "src_type": src_type,
                "dst": dst,
                "dst_type": dst_type,
                "relation": relation,
                "evidence": str(r.get("evidence") or "").strip()[:40],
            }
        )
    try:
        await ensure_schema()
        async with driver.session() as session:
            await session.run(
                "MERGE (d:KgDoc {doc_key: $doc_key}) "
                "SET d.name = $name, d.ext = $ext, d.modality = $modality, "
                "    d.updated_at = datetime()",
                doc_key=doc_key, name=name, ext=ext, modality=modality,
            )
            # 先清除该文档旧的 MENTIONS 出边, 再按最新抽取结果重建(避免重入库残留脏边)
            await session.run(
                "MATCH (d:KgDoc {doc_key: $doc_key})-[r:MENTIONS]->() DELETE r",
                doc_key=doc_key,
            )
            resolved: dict[str, str] = {}
            if entity_rows:
                rows_out = await (
                    await session.run(
                        "UNWIND $rows AS row "
                        "OPTIONAL MATCH (existing:KgEntity {name: row.name}) "
                        "WITH row, collect(DISTINCT existing.type) AS knownTypes "
                        # 同一个名字在图里已经挂过类型时沿用旧类型(多个则按优先序取一个):
                        # 为一个名字开第二个节点, 同一个实体会在图上裂成两个点, 边各连一半,
                        # 看起来就是"该连的没连上"。
                        "WITH row, [t IN $priority WHERE t IN knownTypes] AS ordered "
                        "WITH row, CASE WHEN size(ordered) > 0 THEN ordered[0] ELSE row.type END AS etype "
                        "MERGE (e:KgEntity {name: row.name, type: etype}) "
                        "WITH row, e, etype "
                        "MATCH (d:KgDoc {doc_key: $doc_key}) "
                        "MERGE (d)-[:MENTIONS]->(e) "
                        "RETURN row.name AS name, etype AS type",
                        doc_key=doc_key,
                        rows=entity_rows,
                        priority=list(vocab.TYPE_PRIORITY),
                    )
                ).data()
                resolved = {r["name"]: r["type"] for r in rows_out if r.get("name")}
            # 端点类型以"图里真正落下来的那个"为准: 沿用旧类型时本篇算出的 src_type/dst_type
            # 已经过时, 不对齐就会 MATCH 不到节点而静默丢边。
            for row in relation_rows:
                row["src_type"] = resolved.get(row["src"], row["src_type"])
                row["dst_type"] = resolved.get(row["dst"], row["dst_type"])
            # 本轮本篇真正断言的边; 下面的回收语句按它取反, 所以必须与写入行同口径
            asserted = [
                [row["src"], row["src_type"], row["relation"], row["dst"], row["dst_type"]]
                for row in relation_rows
            ]
            if relation_rows:
                await session.run(
                    "UNWIND $rows AS row "
                    "MATCH (a:KgEntity {name: row.src, type: row.src_type}) "
                    "MATCH (b:KgEntity {name: row.dst, type: row.dst_type}) "
                    "MERGE (a)-[r:KG_REL {relation: row.relation}]->(b) "
                    "ON CREATE SET r.docs = [$doc_key], r.evidence = row.evidence, "
                    "                  r.created_at = datetime() "
                    "SET r.docs = CASE WHEN $doc_key IN coalesce(r.docs, []) "
                    "                  THEN r.docs ELSE coalesce(r.docs, []) + $doc_key END, "
                    "    r.evidence = CASE WHEN r.evidence IS NULL OR r.evidence = '' "
                    "                      THEN row.evidence ELSE r.evidence END, "
                    "    r.last_seen_at = datetime()",
                    doc_key=doc_key, rows=relation_rows,
                )
            # 随篇回收 A: 本篇本轮不再断言、但还有其他文档撑着的边, 只摘掉本篇。
            await session.run(
                "MATCH (a:KgEntity)-[r:KG_REL]->(b:KgEntity) "
                "WHERE $doc_key IN coalesce(r.docs, []) "
                "  AND NOT [a.name, a.type, r.relation, b.name, b.type] IN $asserted "
                "WITH r, [x IN r.docs WHERE x <> $doc_key] AS rest "
                "WHERE rest <> [] SET r.docs = rest",
                doc_key=doc_key, asserted=asserted,
            )
            # 随篇回收 B: 只剩本篇一个来源且本篇已不再断言的边直接删。
            await session.run(
                "MATCH (a:KgEntity)-[r:KG_REL]->(b:KgEntity) "
                "WHERE coalesce(r.docs, []) = [$doc_key] "
                "  AND NOT [a.name, a.type, r.relation, b.name, b.type] IN $asserted "
                "DELETE r",
                doc_key=doc_key, asserted=asserted,
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j upsert_document_graph 失败, 本次不写入图谱: %s", exc)
        return False


async def delete_document_graph(doc_key: str) -> None:
    """删除一篇文档的图谱节点(实体节点保留供其他文档共享)。

    文档节点删掉后, 它断言过的 ``KG_REL`` 必须从 ``docs`` 里摘除(摘空即删), 否则实体
    之间会留下"谁也没说过"的无主边 —— 它们会一直出现在子图里。
    """
    if not doc_key:
        return
    driver = get_driver()
    if driver is None:
        return
    try:
        async with driver.session() as session:
            await session.run(
                "MATCH (d:KgDoc {doc_key: $doc_key}) DETACH DELETE d", doc_key=doc_key
            )
            await session.run(
                "MATCH ()-[r:KG_REL]->() WHERE $doc_key IN coalesce(r.docs, []) "
                "WITH r, [x IN r.docs WHERE x <> $doc_key] AS rest "
                "WHERE rest <> [] SET r.docs = rest",
                doc_key=doc_key,
            )
            # 只剩本篇一个来源的边上面那条(条件 rest <> []) 筛不到, 必须单独删掉,
            # 否则它会带着指向已删文档的 docs 永远留在图里。
            await session.run(
                "MATCH ()-[r:KG_REL]->() WHERE coalesce(r.docs, []) = [$doc_key] DELETE r",
                doc_key=doc_key,
            )
            # docs IS NULL 是溯源上线前的历史边, 不能当成"被删空的边"误删
            await session.run(
                "MATCH ()-[r:KG_REL]->() WHERE r.docs IS NOT NULL AND r.docs = [] DELETE r"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j delete_document_graph 失败: %s", exc)


def _eid(kind: str, key: str) -> str:
    """G6 节点 id: 用前缀区分文档与实体, 避免同名冲突。"""
    return f"{kind}:{key}"


def _entity_id(name: str, type_: object) -> str:
    """实体节点 id 必须带上类型。

    ``:KgEntity`` 的唯一键是 ``(name, type)``; id 只用 name 会把同一名字的不同实体
    并成同一个 G6 节点, 并点之后剩下的边就变成自环或凭空多出的连线。
    """
    return _eid("entity", f"{name}::{type_ or 'entity'}")


async def query_subgraph(
    allowed_doc_keys: Sequence[str],
    focus: str | None = None,
    hops: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """返回 ACL 裁剪后的子图(nodes/edges), 供前端 G6 渲染。

    - 概览(``focus`` 为空): 展示可访问文档 + 其提及的实体(按度数取前 ``limit``)。
    - 聚焦(``focus`` 为某可访问 doc_key): 从该文档实体出发展开 ``hops`` 跳邻域,
      再回填可访问文档, 用于"围绕某文档看关联"。
    两种模式都只返回 ``allowed_doc_keys`` 覆盖到的文档及与之相连的实体。

    边也是被裁剪对象, 不是只要两端节点可见就能返回: ``KG_REL`` 按 ``docs`` 与可访问
    集合求交, 交集为空就不给(无溯源的历史边同样不给, 由迁移脚本回填)。否则一篇
    无权文档断言的关系, 会因为两端实体被别的有权文档提及过而跟着泄出去。
    被这条路子挡下的边数用 ``hidden_edges`` 返回, 给前端提示"旧边待回填"。
    """
    keys = [k for k in allowed_doc_keys if k]
    if not keys:
        return {"nodes": [], "edges": [], "truncated": False, "hidden_edges": 0}
    settings = get_settings()
    limit = limit or settings.kg_graph_node_limit
    hops = hops or settings.kg_graph_hops
    driver = get_driver()
    if driver is None:
        return {"nodes": [], "edges": [], "truncated": False, "hidden_edges": 0}

    try:
        async with driver.session() as session:
            doc_rows = await (
                await session.run(
                    "MATCH (d:KgDoc) WHERE d.doc_key IN $keys "
                    "RETURN d.doc_key AS key, d.name AS name, d.ext AS ext, "
                    "       d.modality AS modality",
                    keys=keys,
                )
            ).data()

            if focus and focus in keys:
                entity_rows = await _focus_entities(session, focus, keys, hops, limit)
            else:
                entity_rows = await (
                    await session.run(
                        "MATCH (d:KgDoc) WHERE d.doc_key IN $keys "
                        "MATCH (d)-[:MENTIONS]->(e:KgEntity) "
                        "WITH e, collect(DISTINCT d.doc_key) AS docs, count(DISTINCT d) AS deg "
                        "WHERE deg >= $min_deg "
                        "RETURN e.name AS name, e.type AS type, docs, deg "
                        "ORDER BY deg DESC LIMIT $limit",
                        keys=keys, limit=limit, min_deg=settings.kg_min_entity_degree,
                    )
                ).data()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j query_subgraph 失败, 返回空图: %s", exc)
        return {"nodes": [], "edges": [], "truncated": False, "hidden_edges": 0}

    # ---- 组装 nodes / edges ----
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    doc_meta: dict[str, dict[str, Any]] = {}
    for r in doc_rows:
        key = r.get("key")
        if not key or key in doc_meta:
            continue
        doc_meta[key] = r
    doc_names = {key: (row.get("name") or key) for key, row in doc_meta.items()}
    for key, row in doc_meta.items():
        nodes.append(
            {
                "id": _eid("doc", key),
                "group": "doc",
                "label": doc_names[key],
                "doc_key": key,
                "modality": row.get("modality") or "text",
                "ext": row.get("ext") or "",
            }
        )

    entity_pairs: list[list[str]] = []
    for r in entity_rows:
        name = r.get("name")
        if not name:
            continue
        type_ = r.get("type") or "entity"
        entity_pairs.append([name, type_])
        nodes.append(
            {
                "id": _entity_id(name, type_),
                "group": "entity",
                "label": name,
                "type": type_,
                "degree": int(r.get("deg") or 0),
            }
        )
        # doc -> entity 的 MENTIONS 边(仅连到可访问文档)
        for dk in r.get("docs") or []:
            if dk in doc_names:
                edges.append(
                    {"source": _eid("doc", dk), "target": _entity_id(name, type_),
                     "type": "MENTIONS", "label": "", "docs": [dk], "doc_count": 1,
                     "doc_names": [doc_names[dk]], "evidence": ""}
                )

    # ---- 实体之间的 KG_REL 边(两端都在返回实体集内, 且至少有一篇可访问文档断言过) ----
    hidden_edges = 0
    if len(entity_pairs) >= 2:
        try:
            async with driver.session() as session:
                rel_rows = await (
                    await session.run(
                        "MATCH (a:KgEntity)-[r:KG_REL]->(b:KgEntity) "
                        "WHERE [a.name, a.type] IN $pairs AND [b.name, b.type] IN $pairs "
                        "RETURN a.name AS src, a.type AS src_type, "
                        "       b.name AS dst, b.type AS dst_type, "
                        "       r.relation AS relation, r.evidence AS evidence, "
                        "       [k IN coalesce(r.docs, []) WHERE k IN $keys] AS allowed_docs",
                        pairs=entity_pairs, keys=keys,
                    )
                ).data()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j query KG_REL 边失败, 仅返回 MENTIONS 边: %s", exc)
            rel_rows = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for r in rel_rows:
            allowed_docs = [d for d in (r.get("allowed_docs") or []) if d]
            if not allowed_docs:
                hidden_edges += 1
                continue
            src_type = r.get("src_type") or "entity"
            dst_type = r.get("dst_type") or "entity"
            relation = r.get("relation") or ""
            dedupe = (r["src"], src_type, relation, r["dst"], dst_type)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            edges.append(
                {"source": _entity_id(r["src"], src_type), "target": _entity_id(r["dst"], dst_type),
                 "type": "KG_REL", "label": relation,
                 "docs": allowed_docs, "doc_count": len(allowed_docs),
                 "doc_names": [doc_names.get(d, d) for d in allowed_docs[:3]],
                 "evidence": r.get("evidence") or ""}
            )

    truncated = bool(entity_rows) and len(entity_rows) >= limit
    return {"nodes": nodes, "edges": edges, "truncated": truncated, "hidden_edges": hidden_edges}


async def _focus_entities(session, focus: str, keys: list[str], hops: int, limit: int):
    """聚焦模式: 从 focus 文档的实体出发做 hops 跳邻域, 再回填可访问文档的提及关系。

    两个细节都是为了让展开出来的子图自洽可连:

    - 邻域按 ``(name, type)`` 整对收集: 只收 name 会让回填后的实体与边用不同的
      身份拼接, 同一名字的不同类型被并成一个点, 剩下的边就成了自环/悬空连线。
    - 路径上的每条 ``KG_REL`` 也要求至少被一篇可访问文档断言: 节点可见不等于边可见,
      不拦住就会顺着无权文档断言的边爬到不该关联的实体上。
    """
    h = max(1, int(hops))  # 跳数内联进 Cypher(Neo4j 变长路径不支持参数化), 已强制为 int
    names_rows = await (
        await session.run(
            f"MATCH (d:KgDoc {{doc_key: $focus}})-[:MENTIONS]->(a:KgEntity) "
            f"OPTIONAL MATCH path = (a)-[:KG_REL*1..{h}]-(e:KgEntity) "
            "WHERE all(rel IN relationships(path) WHERE "
            "      any(k IN coalesce(rel.docs, []) WHERE k IN $keys)) "
            "RETURN [a.name, a.type] AS direct, "
            "       [n IN nodes(path)[1..] | [n.name, n.type]] AS ext",
            focus=focus, keys=keys,
        )
    ).data()
    collected: list[tuple[str, str]] = []
    for r in names_rows:
        direct = r.get("direct") or []
        if len(direct) == 2 and direct[0]:
            collected.append((direct[0], direct[1] or "entity"))
        for pair in r.get("ext") or []:
            if isinstance(pair, list) and len(pair) == 2 and pair[0]:
                collected.append((pair[0], pair[1] or "entity"))
    entity_pairs = [[name, type_] for name, type_ in dict.fromkeys(collected)][:limit]
    if not entity_pairs:
        return []
    rows = await (
        await session.run(
            "MATCH (e:KgEntity) WHERE [e.name, e.type] IN $pairs "
            "OPTIONAL MATCH (d:KgDoc)-[:MENTIONS]->(e) "
            "WHERE d.doc_key IN $keys "
            "WITH e, collect(DISTINCT d.doc_key) AS docs, count(DISTINCT d) AS deg "
            "RETURN e.name AS name, e.type AS type, docs, deg "
            "ORDER BY deg DESC LIMIT $limit",
            pairs=entity_pairs, keys=keys, limit=limit,
        )
    ).data()
    return rows
