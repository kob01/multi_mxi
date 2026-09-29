"""个人图谱 (Personal Graph) 通道: Neo4j 实体关系图。

节点/关系设计(全部按 ``user_id`` 打标签隔离, 避免不同用户的实体在图里串到
一起, 造成"甲的同事"被当成"乙的同事"这种跨用户污染):

    (:MemoryUser {user_id})-[:MENTIONS {source}]->(:MemoryEntity {name, type})
    (:MemoryEntity)-[:REL {relation, valid_at, invalid_at, as_of}]->(:MemoryEntity)

``:REL`` 带双时态(``valid_at`` 现实何时成立 / ``invalid_at`` 现实何时不再成立, 另有一
个 ``as_of`` 与画像侧同口径地表示"这句陈述排在什么时候"): 人事关系会变(调部门、
换汇报对象), 旧关系不能与当前关系并列喂给模型, 也不能直接删(时间旅行式提问需要它),
口径见 ``upsert_entities`` 与 ``app/memory/temporal.py``。

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
from datetime import datetime, timedelta, timezone

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import get_settings
from app.memory.temporal import as_datetime

logger = logging.getLogger(__name__)

_schema_ready = False
_driver: AsyncDriver | None = None
_driver_failed = False

# 实体类型白名单: LLM 偶尔会造出自定义类型, 收敛到 other 保证图里类型可枚举。
_ENTITY_TYPES = {"person", "department", "system", "document", "position", "topic"}

# 单值关系(函数式): 同一个 (主体, 关系) 现实上只能成立一个客体, 新事实要把旧边
# 打失效而不是并存 —— 否则"现在汇报给谁"这类问题会把新旧两个答案一起喂给模型。
# 只列真正函数式的关系词: "负责"/"审批" 天然多值, 不进白名单(宁可漏失效不可错失效)。
_SINGLE_VALUED_RELATIONS = {
    "任职于", "就职于", "属于", "所在部门", "汇报", "汇报给", "直属上级", "上级",
    "所在地", "现居", "职位", "担任", "居住于",
}


def _iso_utc(value: datetime | None) -> str | None:
    """datetime -> 秒级 UTC ISO 字符串; 图里按字符串比大小, 格式必须统一。

    统一到 UTC 并削掉微秒是为了让 ``valid_at``/``invalid_at`` 的字典序就是时间序:
    否则 ``+08:00`` 与 ``+00:00`` 两种写法比出的大小是错的(同一瞬间两个不相等的字符串)。
    """
    if value is None:
        return None
    stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _relation_row(raw: dict, *, now_dt: datetime, grace_days: int) -> dict:
    """一条提取出的关系 -> Cypher 参数行: 补生效时间, 并算出"谁顶掉谁"用的 ``as_of``。

    两个时间各管各的事:

    - ``valid_at`` 是现实轴(展示与失效点用), 就是用户说的那个起始时间;
    - ``as_of`` 是新旧比较用的排期: 用户明说了时间且已在宽限期之外 → 这确实在讲过去,
      按起始时间排; 没提时间、或提的是"从上个月起"这类近期起始 → 讲的是**持续到当下**
      的状态, 按当下排。

    宽限期与画像侧共用 ``profile_current_grace_days`` —— 同一个事实如果在画像里算"现在
    变了"而在图里算"过去的旧说法", 两个召回通道就会给模型矛盾答案。
    """
    relation = str(raw.get("relation") or "related").strip() or "related"
    given = as_datetime(raw.get("valid_at"))
    stamp = given or now_dt
    historical = given is not None and given < now_dt - timedelta(days=max(1, int(grace_days)))
    return {
        "src": str(raw.get("src") or ""),
        "dst": str(raw.get("dst") or ""),
        "relation": relation,
        "valid_at": _iso_utc(stamp) or "",
        "as_of": _iso_utc(stamp if historical else now_dt) or "",
        "single": relation in _SINGLE_VALUED_RELATIONS,
    }


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
    """写入/合并实体与实体关系, 节点全部 MERGE(重复写入不会产生重复节点)。

    ``entities``: ``[{"name": ..., "type": ...}, ...]``
    ``relations``: ``[{"src": ..., "relation": ..., "dst": ..., "valid_at": 可选}, ...]``,
    src/dst 按 ``name`` 匹配同一 ``user_id`` 下的既有实体。

    额外挂到 ``(:MemoryUser)-[:MENTIONS]->(:MemoryEntity)`` 上: 这一层让"个人
    图谱"有中心可查, 前端子图与读路径都从锚点出发而不是猜实体名。

    REL 边带双时态(``valid_at`` 现实何时成立 / ``invalid_at`` 现实何时不再成立,
    ``created_at``/``expired_at`` 是系统录入轴), 口径同 Graphiti 的 fact invalidation:

    - 单值关系且新事实的排期(``as_of``)不早于旧边 → 旧边不删, 只把 ``invalid_at`` 写成
      新事实的起始时间(用户讲"我 8 月 1 日就离职了", 旧关系现实中就从那天起不成立);
    - 新事实的排期早于既有有效边(用户在补陈年旧事) → 旧边不动, 新边**写入即处于
      失效态**, 这样"当前有哪些关系"的查询天然不会被旧说法污染;
    - 排期按``profile_current_grace_days``区分"讲过去"与"持续到当下": "从上个月起
      我改汇报给张总"是变更而不是陈迹, 应该顶掉旧关系(与画像侧同一口径)。

    三种情形都不丢历史: 时间旅行式提问("我以前汇报给谁")仍然答得上来。
    """
    if not user_id or not (entities or relations):
        return
    driver = get_driver()
    if driver is None:
        return
    now_dt = datetime.now(timezone.utc)
    grace_days = get_settings().profile_current_grace_days
    rows = [
        {"name": e["name"], "type": _entity_type(e.get("type"))}
        for e in entities
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip()
    ]
    relation_rows = [
        _relation_row(r, now_dt=now_dt, grace_days=grace_days)
        for r in relations
        if isinstance(r, dict) and r.get("src") and r.get("dst")
    ]
    now = _iso_utc(now_dt) or ""
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
            if relation_rows:
                # 1) 先关掉被取代的旧有效边(仅限单值关系, 且旧边的排期不晚于新边):
                #    失效点落在新事实的现实起始时间上, 旧边不删、只关窗。
                await session.run(
                    "UNWIND $rows AS row "
                    "MATCH (a:MemoryEntity {user_id: $user_id, name: row.src})"
                    "-[old:REL {relation: row.relation}]->(other:MemoryEntity) "
                    "WHERE row.single AND other.name <> row.dst AND old.invalid_at IS NULL "
                    "  AND (old.as_of IS NULL OR old.as_of <= row.as_of) "
                    "SET old.invalid_at = row.valid_at, old.expired_at = $now",
                    user_id=user_id,
                    rows=relation_rows,
                    now=now,
                )
                # 2) 再写新边, 并按"同关系上是否还挂着排期更晚的有效边"定它的生死:
                #    有 → 这句在讲过去, 新边直接以那个时间为失效点; 没有 → 新边就是当前态
                #    (包含"用户又说了一遍当前关系"的重述), 顺手把旧失效标记抹掉。
                await session.run(
                    "UNWIND $rows AS row "
                    "MATCH (a:MemoryEntity {user_id: $user_id, name: row.src}) "
                    "MERGE (b:MemoryEntity {user_id: $user_id, name: row.dst}) "
                    "WITH a, b, row "
                    "OPTIONAL MATCH (a)-[prev:REL {relation: row.relation}]->(other:MemoryEntity) "
                    "WHERE other.name <> b.name AND prev.invalid_at IS NULL "
                    "  AND prev.as_of IS NOT NULL AND prev.as_of > row.as_of "
                    "WITH a, b, row, min(prev.as_of) AS superseded_by "
                    "MERGE (a)-[r:REL {relation: row.relation}]->(b) "
                    "ON CREATE SET r.valid_at = row.valid_at, r.as_of = row.as_of, r.created_at = $now "
                    "SET r.last_seen_at = $now, "
                    "    r.invalid_at = CASE WHEN NOT row.single THEN r.invalid_at "
                    "                         WHEN superseded_by IS NOT NULL THEN coalesce(r.invalid_at, superseded_by) "
                    "                         ELSE NULL END, "
                    "    r.expired_at = CASE WHEN NOT row.single THEN r.expired_at "
                    "                         WHEN superseded_by IS NOT NULL THEN $now "
                    "                         ELSE NULL END",
                    user_id=user_id,
                    rows=relation_rows,
                    now=now,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j upsert_entities 失败, 本次不写入图记忆: %s", exc)


async def related_facts(
    user_id: str,
    entity_names: Sequence[str],
    hops: int | None = None,
    *,
    include_expired: bool = False,
) -> list[str]:
    """从给定实体出发做多跳邻居查询, 返回渲染好的"关联事实"文本行。

    默认只走未失效的边(``invalid_at IS NULL``): 否则"现在汇报给谁"会把旧的汇报对象
    一起描出来。问"我以前负责什么/2020 年在哪个部门"时传 ``include_expired=True`` 才会
    把失效边一并回放。无此属性的历史边(双时态上线前写的)算有效, 向后兼容。
    """
    if not user_id or not entity_names:
        return []
    settings = get_settings()
    hops = hops or settings.graph_memory_hops
    driver = get_driver()
    if driver is None:
        return []
    live = (
        "" if include_expired
        else "AND all(rel IN relationships(path) WHERE rel.invalid_at IS NULL) "
    )
    cypher = (
        f"MATCH path = (a:MemoryEntity {{user_id: $user_id}})-[:REL*1..{int(hops)}]-(b:MemoryEntity) "
        f"WHERE a.name IN $names AND a <> b {live}"
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

    ``nodes`` 含实体名/类型, ``links`` 是实体之间的 REL 关系(带生效/失效时间与
    ``state`` 标记, 供前端区分"当前关系"与"已失效的历史关系"); 没图/失败一律返回
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
                # 有向匹配: 无向 ``-[r:REL]-`` 会把同一条边按两个方向各返一次,
                # 前端列表就会看到"我->李总"与"李总->我"两行重复(带状态列后更明显)。
                # 图里该用户的全部实体都挂了 MENTIONS, 所以有向匹配不会漏边。
                "MATCH (u:MemoryUser {user_id: $user_id})-[:MENTIONS]->(a:MemoryEntity)"
                "-[r:REL]->(b:MemoryEntity {user_id: $user_id}) "
                "RETURN a.name AS src, r.relation AS relation, b.name AS dst, "
                "       r.valid_at AS valid_at, r.invalid_at AS invalid_at LIMIT $limit",
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
        {
            "src": r["src"],
            "relation": r.get("relation") or "related",
            "dst": r["dst"],
            # 时间只到日(前端列表展示用); 无时间的边是双时态上线前写的, 当作当前有效。
            "valid_at": str(r.get("valid_at") or "")[:10],
            "invalid_at": str(r.get("invalid_at") or "")[:10],
            "state": "expired" if r.get("invalid_at") else "active",
        }
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
