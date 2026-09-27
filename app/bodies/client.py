"""MongoDB async client (motor): 正文与结构的存放层。

设计取向与 ``app/db/session.py``(asyncpg) / ``app/rag/bm25.py``(AsyncElasticsearch)
/ ``app/memory/graph_store.py``(neo4j async) 一致:
- 进程级单例, ``get_mongo_client()`` 惰性构造且**构造零 I/O**(网络错误延后到
  第一条命令时才暴露, 由调用方按读/写路径分别降级);
- ``mongo_enabled=false`` 时读路径返回空、写路径由门面层抛错(入库不能静默丢正文);
- 只按 ``_id``/``doc_key``/``parent_id`` 精确取, 不提供任何按内容/正则/聚合的查询
  接口 —— 否则等于在 PG/ES 的 ACL 裁剪之外开一个无权限的全文检索口子(不变量 3)。
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings

logger = logging.getLogger(__name__)

# 三个集合: 整篇正文 / 超阈值溢出分片 / 父块全文。
COLL_DOC_BODY = "doc_bodies"
COLL_DOC_BODY_PARTS = "doc_body_parts"
COLL_PARENT_TEXT = "parent_texts"

_client: AsyncIOMotorClient | None = None


def mongo_database_url() -> str:
    """解析 Mongo 连接串。

    MONGO_URL 优先(支持内嵌 ``user:pw@host``); 若配了 MONGO_USER/MONGO_PASSWORD
    且 URL 不含凭证, 用 ``quote_plus`` 转义后拼接并带 ``authSource=admin``
    —— 复用 ``async_database_url`` 里"密码含 @ / : 等保留字符必须转义"的教训。
    """
    s = get_settings()
    url = s.mongo_url
    if s.mongo_user and s.mongo_password and "://" in url and "@" not in url.split("://", 1)[1]:
        scheme, rest = url.split("://", 1)
        cred = f"{quote_plus(s.mongo_user)}:{quote_plus(s.mongo_password)}"
        sep = "&" if "?" in rest else "?"
        return f"{scheme}://{cred}@{rest}{sep}authSource=admin"
    return url


def get_mongo_client() -> AsyncIOMotorClient:
    """惰性创建进程级 client(构造零 I/O)。"""
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncIOMotorClient(
            mongo_database_url(),
            maxPoolSize=s.mongo_max_pool_size,
            serverSelectionTimeoutMS=s.mongo_server_selection_ms,
            connectTimeoutMS=s.mongo_connect_timeout_ms,
        )
    return _client


def get_db():
    """返回配置的默认数据库句柄。"""
    return get_mongo_client()[get_settings().mongo_database]


async def init_body_schema() -> None:
    """幂等建集合 + 索引(全部 background=True, 索引已存在时吞 OperationFailure)。

    读路径永远走 ``_id``(doc_bodies/parent_texts 的 ``_id`` 直接是 doc_key/parent_id);
    这里建的二级索引只服务两类非 _id 场景:
    - ``doc_bodies.updated_at``: 迁移排查(按时间扫描最近变更);
    - ``doc_body_parts (doc_key, part_no)`` 唯一复合: 溢出分片按 doc_key 升序拼;
    - ``parent_texts.doc_id``: ``delete_many({doc_id})`` 的主路径(重入库/删除)。
    """
    if not get_settings().mongo_enabled:
        logger.info("mongo_enabled=false, 跳过 body schema 初始化")
        return
    db = get_db()
    try:
        from pymongo.errors import OperationFailure

        await db[COLL_DOC_BODY].create_index("updated_at", background=True)
        await db[COLL_DOC_BODY_PARTS].create_index(
            [("doc_key", 1), ("part_no", 1)], unique=True, background=True
        )
        await db[COLL_PARENT_TEXT].create_index("doc_id", background=True)
    except OperationFailure as exc:
        # 索引已存在 / 并发创建竞态: 幂等吞掉。
        logger.warning("init_body_schema 索引创建告警(可忽略): %s", exc)


async def ping() -> bool:
    """探活: 不抛异常, 返回 bool。受 serverSelectionTimeoutMS 约束快速失败。"""
    if not get_settings().mongo_enabled:
        return False
    try:
        await get_mongo_client().admin.command("ping")
        return True
    except Exception as exc:  # noqa: BLE001 - 探活永不抛出
        logger.warning("mongo ping failed: %s", exc)
        return False


async def close_mongo() -> None:
    """释放连接池(供 lifespan 关闭时调用)。"""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("closing mongo client failed: %s", exc)
        _client = None


def mongo_available() -> bool:
    """是否已初始化 client(对齐 db_available() 语义; 不代表网络已连通)。"""
    return _client is not None
