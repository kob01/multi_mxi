"""长期记忆的 Graph 通道: Neo4j 实体关系图。

节点/关系设计(全部按 ``user_id`` 打标签隔离, 避免不同用户的实体在图里串到
一起, 造成"甲的同事"被当成"乙的同事"这种跨用户污染):

    (:MemoryEntity {user_id, name, type})
    (:MemoryEntity)-[:REL {relation}]->(:MemoryEntity)

单节点内网部署, ``NEO4J_AUTH=none``(对齐 Elasticsearch 的"无认证"先例);
连不上/未启用时所有方法静默返回空结果并只记一次 WARNING, 不阻断对话 —— 与
Session Memory / Retrieval Cache 完全一致的降级策略。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)

_schema_ready = False
_driver: AsyncDriver | None = None
_driver_failed = False


def _auth() -> tuple[str, str] | None:
    settings = get_settings()
    if settings.neo4j_user and settings.neo4j_password:
        return (settings.neo4j_user, settings.neo4j_password)
    return None  # NEO4J_AUTH=none 时留空即可


def get_driver() -> AsyncDriver | None:
    """惰性建 Neo4j 驱动单例; ``graph_memory_enabled=false`` 或建驱动失败返回 None。"""
    global _driver, _driver_failed
    settings = get_settings()
    if not settings.graph_memory_enabled:
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
        _schema_ready = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j schema 初始化失败, Graph 长期记忆降级为不可用: %s", exc)


async def upsert_entities(
    user_id: str,
    entities: Sequence[dict],
    relations: Sequence[dict],
) -> None:
    """写入/合并实体与实体关系, 全部 MERGE 语义(重复写入不会产生重复节点)。

    ``entities``: ``[{"name": ..., "type": ...}, ...]``
    ``relations``: ``[{"src": ..., "relation": ..., "dst": ...}, ...]``,
    src/dst 按 ``name`` 匹配同一 ``user_id`` 下的既有实体。
    """
    if not user_id or not (entities or relations):
        return
    driver = get_driver()
    if driver is None:
        return
    try:
        async with driver.session() as session:
            if entities:
                await session.run(
                    "UNWIND $rows AS row "
                    "MERGE (e:MemoryEntity {user_id: $user_id, name: row.name, type: row.type})",
                    user_id=user_id,
                    rows=[{"name": e["name"], "type": e.get("type", "entity")} for e in entities],
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
