"""个人图谱 (Personal Graph) 通道: Neo4j 实体关系图。

节点/关系设计(全部按 ``user_id`` 打标签隔离, 避免不同用户的实体在图里串到
一起, 造成"甲的同事"被当成"乙的同事"这种跨用户污染):

    (:MemoryUser {user_id})-[:MENTIONS {source}]->(:MemoryEntity {name, type})
    (:MemoryEntity)-[:REL {relation}]->(:MemoryEntity)

``MemoryUser`` 锚点是"个人图谱"区别于"一堆孤立实体"的关键: 实体不再需要
从名字出发才能定位, 而是可以从"这个用户"直接展开全部邻居(前端图谱页、
读路径的实体起点都依赖它)。

单节点内网部署, ``NEO4J_AUTH=none``(对齐 Elasticsearch 的"无认证"先例);
连不上/未启用时所有方法静默返回空结果并只记一次 WARNING, 不阻断对话 —— 与
Session Memory / Retrieval Cache 完全一致的降级策略。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)

_schema_ready = False
_driver: AsyncDriver | None = None
_driver_failed = False

# 实体类型白名单: LLM 偶尔会造出自定义类型, 收敛到 other 保证图里类型可枚举。
_ENTITY_TYPES = {"person", "department", "system", "document", "position", "topic"}


def _auth() -> tuple[str, str] | None:
    settings = get_settings()
    if settings.neo4j_user and settings.neo4j_password:
        return (settings.neo4j_user, settings.neo4j_password)
    return None  # NEO4J_AUTH=none 时留空即可


def get_driver() -> AsyncDriver | None:
    """惰性建 Neo4j 驱动单例; 图记忆与文档图谱均关闭或建驱动失败时返回 None。

    两类图能力(对话长期记忆 :MemoryEntity / 文档知识图谱 :KgDoc/:KgEntity)共用
    同一个驱动, 只要任一开关打开就建立连接, 避免维护两套连接池。
    """
    global _driver, _driver_failed
    settings = get_settings()
    if not (settings.graph_memory_enabled or settings.doc_kg_enabled):
        return None
    if _driver is None and not _driver_failed:
        try:
            _driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=_auth())
        except Exception as exc:  # noqa: BLE001 - 图记忆不可用时静默降级
            logger.warning("Neo4j driver 初始化失败, Graph 长期记忆降级为不可用: %s", exc)
            _driver_failed = True
    return _driver


async def ensure_schema() -> None:
    """建唯一约束/索引(幂等); 失败只 WARNING, 不抛出(与建 driver 同理)。"""
    global _schema_ready
    driver = get_driver()
    if driver is None or _schema_ready:
        return
    try:
        async with driver.session() as session:
            await session.run(
                "CREATE CONSTRAINT memory_entity_unique IF NOT EXISTS "
                "FOR (e:MemoryEntity) REQUIRE (e.user_id, e.name, e.type) IS UNIQUE"
            )
            await session.run(
                "CREATE CONSTRAINT memory_user_unique IF NOT EXISTS "
                "FOR (u:MemoryUser) REQUIRE u.user_id IS UNIQUE"
            )
            await session.run(
                "CREATE INDEX memory_entity_user IF NOT EXISTS FOR (e:MemoryEntity) ON e.user_id"
            )
        _schema_ready = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j schema 初始化失败, Graph 长期记忆降级为不可用: %s", exc)


def _entity_type(raw: object) -> str:
    """实体类型收敛到白名单(未知类型归 other, 空值归 entity)。"""
    value = str(raw or "").strip().lower()
    return value if value in _ENTITY_TYPES else ("other" if value else "entity")


async def upsert_entities(
    user_id: str,
    entities: Sequence[dict],
    relations: Sequence[dict],
    *,
    source: str = "turn",
) -> None:
    """写入/合并实体与实体关系, 全部 MERGE 语义(重复写入不会产生重复节点)。

    ``entities``: ``[{"name": ..., "type": ...}, ...]``
    ``relations``: ``[{"src": ..., "relation": ..., "dst": ...}, ...]``,
    src/dst 按 ``name`` 匹配同一 ``user_id`` 下的既有实体。

    额外挂到 ``(:MemoryUser)-[:MENTIONS]->(:MemoryEntity)`` 上: 这一层让"个人
    图谱"有中心可查, 前端子图与读路径都从锚点出发而不是猜实体名。
    """
    if not user_id or not (entities or relations):
        return
    driver = get_driver()
    if driver is None:
        return
    rows = [
        {"name": e["name"], "type": _entity_type(e.get("type"))}
        for e in entities
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip()
    ]
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with driver.session() as session:
            await session.run(
                "MERGE (u:MemoryUser {user_id: $user_id}) "
                "ON CREATE SET u.created_at = $now "
                "SET u.last_seen_at = $now",
                user_id=user_id,
                now=now,
            )
            if rows:
                await session.run(
                    "UNWIND $rows AS row "
                    "MERGE (e:MemoryEntity {user_id: $user_id, name: row.name, type: row.type}) "
                    "WITH e, row "
                    "MATCH (u:MemoryUser {user_id: $user_id}) "
                    "MERGE (u)-[m:MENTIONS]->(e) "
                    "SET m.source = $source, m.last_seen_at = $now",
                    user_id=user_id,
                    rows=rows,
                    source=source,
                    now=now,
                )
            if relations:
                await session.run(
                    "UNWIND $rows AS row "
                    "MERGE (a:MemoryEntity {user_id: $user_id, name: row.src}) "
                    "MERGE (b:MemoryEntity {user_id: $user_id, name: row.dst}) "
                    "MERGE (a)-[r:REL {relation: row.relation}]->(b)",
                    user_id=user_id,
                    rows=[
                        {"src": r["src"], "dst": r["dst"], "relation": r.get("relation", "related")}
                        for r in relations
                        if isinstance(r, dict) and r.get("src") and r.get("dst")
                    ],
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j upsert_entities 失败, 本次不写入图记忆: %s", exc)


async def related_facts(
    user_id: str,
    entity_names: Sequence[str],
    hops: int | None = None,
) -> list[str]:
    """从给定实体出发做多跳邻居查询, 返回渲染好的"关联事实"文本行。"""
    if not user_id or not entity_names:
        return []
    settings = get_settings()
    hops = hops or settings.graph_memory_hops
    driver = get_driver()
    if driver is None:
        return []
    cypher = (
        f"MATCH path = (a:MemoryEntity {{user_id: $user_id}})-[:REL*1..{int(hops)}]-(b:MemoryEntity) "
        "WHERE a.name IN $names AND a <> b "
        "RETURN DISTINCT [n IN nodes(path) | n.name] AS names LIMIT 10"
    )
    try:
        async with driver.session() as session:
            result = await session.run(cypher, user_id=user_id, names=list(entity_names))
            rows = await result.data()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j related_facts 查询失败, 降级为无图记忆: %s", exc)
        return []
    return [" -> ".join(r["names"]) for r in rows if r.get("names")]


async def entity_names(user_id: str, limit: int = 200) -> list[str]:
    """某用户图里已有的实体名(读路径拿它做查询文本的字面匹配起点)。"""
    driver = get_driver()
    if driver is None or not user_id:
        return []
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (e:MemoryEntity {user_id: $user_id}) RETURN e.name AS name LIMIT $limit",
                user_id=user_id,
                limit=int(limit),
            )
            rows = await result.data()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j 实体名查询失败, 本轮 Graph 通道跳过: %s", exc)
        return []
    return [r["name"] for r in rows if r.get("name")]


async def user_subgraph(user_id: str, limit: int = 60) -> dict[str, list[dict]]:
    """以用户锚点为中心取一跳子图, 返回 ``{nodes, links, paths}`` 供前端渲染。

    ``nodes`` 含实体名/类型, ``links`` 是实体之间的 REL 关系; 没图/失败一律返回
    空结构(与其余通道同一降级口径, 不能让图谱页拖垮整个接口)。
    """
    empty = {"nodes": [], "links": [], "paths": []}
    driver = get_driver()
    if driver is None or not user_id:
        return empty
    try:
        async with driver.session() as session:
            nodes_res = await session.run(
                "MATCH (u:MemoryUser {user_id: $user_id})-[:MENTIONS]->(e:MemoryEntity) "
                "RETURN e.name AS name, e.type AS type LIMIT $limit",
                user_id=user_id,
                limit=int(limit),
            )
            node_rows = await nodes_res.data()
            links_res = await session.run(
                "MATCH (u:MemoryUser {user_id: $user_id})-[:MENTIONS]->(a:MemoryEntity)"
                "-[r:REL]-(b:MemoryEntity {user_id: $user_id}) "
                "RETURN a.name AS src, r.relation AS relation, b.name AS dst LIMIT $limit",
                user_id=user_id,
                limit=int(limit),
            )
            link_rows = await links_res.data()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j user_subgraph 查询失败, 降级为空图: %s", exc)
        return empty
    names: set[str] = set()
    nodes: list[dict] = []
    for row in node_rows:
        name = row.get("name")
        if not name or name in names:
            continue
        names.add(name)
        nodes.append({"name": name, "type": row.get("type") or "entity"})
    links = [
        {"src": r["src"], "relation": r.get("relation") or "related", "dst": r["dst"]}
        for r in link_rows
        if r.get("src") in names and r.get("dst") in names
    ]
    paths = [f"{l['src']} -{l['relation']}-> {l['dst']}" for l in links]
    return {"nodes": nodes, "links": links, "paths": paths}


async def close_driver() -> None:
    """释放驱动(供 FastAPI lifespan 关闭时调用)。"""
    global _driver, _schema_ready
    if _driver is not None:
        try:
            await _driver.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("closing neo4j driver failed: %s", exc)
        _driver = None
        _schema_ready = False
