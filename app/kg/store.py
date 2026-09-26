"""文档知识图谱的 Neo4j 读写层(全新标签命名空间, 与 :MemoryEntity 隔离)。

节点/关系设计(全局知识库级, 不按 user_id 分区):

    (:KgDoc {doc_key, name, ext, modality, updated_at})
    (:KgEntity {name, type})
    (:KgDoc)-[:MENTIONS]->(:KgEntity)
    (:KgEntity)-[:KG_REL {relation}]->(:KgEntity)

复用 ``app.memory.graph_store.get_driver()`` 的驱动单例; 驱动不可用(未开启/连不上)
时所有方法静默返回空/不写入并只记 WARNING —— 与图记忆完全一致的降级策略。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.config import get_settings
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
    """
    if not doc_key:
        return False
    driver = get_driver()
    if driver is None:
        return False
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
            if entities:
                await session.run(
                    "UNWIND $rows AS row "
                    "MERGE (e:KgEntity {name: row.name, type: row.type}) "
                    "WITH row, e "
                    "MATCH (d:KgDoc {doc_key: $doc_key}) "
                    "MERGE (d)-[:MENTIONS]->(e)",
                    doc_key=doc_key,
                    rows=[{"name": e["name"], "type": e.get("type", "entity")} for e in entities],
                )
            if relations:
                await session.run(
                    "UNWIND $rows AS row "
                    "MATCH (a:KgEntity {name: row.src}) "
                    "MATCH (b:KgEntity {name: row.dst}) "
                    "MERGE (a)-[r:KG_REL {relation: row.relation}]->(b)",
                    rows=[
                        {
                            "src": r["src"],
                            "dst": r["dst"],
                            "relation": r.get("relation", "related"),
                        }
                        for r in relations
                    ],
                )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j upsert_document_graph 失败, 本次不写入图谱: %s", exc)
        return False


async def delete_document_graph(doc_key: str) -> None:
    """删除一篇文档的图谱节点(实体节点保留供其他文档共享)。"""
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j delete_document_graph 失败: %s", exc)


def _eid(kind: str, key: str) -> str:
    """G6 节点 id: 用前缀区分文档与实体, 避免同名冲突。"""
    return f"{kind}:{key}"


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
    两种模式都只返回 ``allowed_doc_keys`` 覆盖到的文档及与之相连的实体,
    无权文档节点及其独占边一律不返回。
    """
    keys = [k for k in allowed_doc_keys if k]
    if not keys:
        return {"nodes": [], "edges": [], "truncated": False}
    settings = get_settings()
    limit = limit or settings.kg_graph_node_limit
    hops = hops or settings.kg_graph_hops
    driver = get_driver()
    if driver is None:
        return {"nodes": [], "edges": [], "truncated": False}

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
        return {"nodes": [], "edges": [], "truncated": False}

    # ---- 组装 nodes / edges ----
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    doc_ids = {r["key"] for r in doc_rows if r.get("key")}
    for r in doc_rows:
        if not r.get("key"):
            continue
        nodes.append(
            {
                "id": _eid("doc", r["key"]),
                "group": "doc",
                "label": r.get("name") or r["key"],
                "doc_key": r["key"],
                "modality": r.get("modality") or "text",
                "ext": r.get("ext") or "",
            }
        )

    entity_names: list[str] = []
    for r in entity_rows:
        name = r.get("name")
        if not name:
            continue
        entity_names.append(name)
        nodes.append(
            {
                "id": _eid("entity", name),
                "group": "entity",
                "label": name,
                "type": r.get("type") or "entity",
                "degree": int(r.get("deg") or 0),
            }
        )
        # doc -> entity 的 MENTIONS 边(仅连到可访问文档)
        for dk in r.get("docs") or []:
            if dk in doc_ids:
                edges.append(
                    {"source": _eid("doc", dk), "target": _eid("entity", name),
                     "type": "MENTIONS", "label": ""}
                )

    # ---- 实体之间的 KG_REL 边(仅保留两端都在返回实体集内的) ----
    if len(entity_names) >= 2:
        try:
            async with driver.session() as session:
                rel_rows = await (
                    await session.run(
                        "MATCH (a:KgEntity)-[r:KG_REL]->(b:KgEntity) "
                        "WHERE a.name IN $names AND b.name IN $names "
                        "RETURN a.name AS src, b.name AS dst, r.relation AS relation",
                        names=entity_names,
                    )
                ).data()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j query KG_REL 边失败, 仅返回 MENTIONS 边: %s", exc)
            rel_rows = []
        for r in rel_rows:
            edges.append(
                {"source": _eid("entity", r["src"]), "target": _eid("entity", r["dst"]),
                 "type": "KG_REL", "label": r.get("relation") or ""}
            )

    truncated = bool(entity_rows) and len(entity_rows) >= limit
    return {"nodes": nodes, "edges": edges, "truncated": truncated}


async def _focus_entities(session, focus: str, keys: list[str], hops: int, limit: int):
    """聚焦模式: 从 focus 文档的实体出发做 hops 跳邻域, 再回填可访问文档的提及关系。"""
    h = max(1, int(hops))  # 跳数内联进 Cypher(Neo4j 变长路径不支持参数化), 已强制为 int
    names_rows = await (
        await session.run(
            f"MATCH (d:KgDoc {{doc_key: $focus}})-[:MENTIONS]->(a:KgEntity) "
            f"OPTIONAL MATCH path = (a)-[:KG_REL*1..{h}]-(e:KgEntity) "
            "WITH collect(DISTINCT a.name) AS direct, "
            "     [n IN nodes(path)[1..] | n.name] AS ext "
            "RETURN direct, ext",
            focus=focus,
        )
    ).data()
    collected: list[str] = []
    for r in names_rows:
        collected.extend([n for n in (r.get("direct") or []) if n])
        collected.extend([n for n in (r.get("ext") or []) if n])
    entity_names = list(dict.fromkeys(collected))[:limit]
    if not entity_names:
        return []
    rows = await (
        await session.run(
            "MATCH (e:KgEntity) WHERE e.name IN $names "
            "OPTIONAL MATCH (d:KgDoc)-[:MENTIONS]->(e) "
            "WHERE d.doc_key IN $keys "
            "WITH e, collect(DISTINCT d.doc_key) AS docs, count(DISTINCT d) AS deg "
            "RETURN e.name AS name, e.type AS type, docs, deg "
            "ORDER BY deg DESC LIMIT $limit",
            names=entity_names, keys=keys, limit=limit,
        )
    ).data()
    return rows
