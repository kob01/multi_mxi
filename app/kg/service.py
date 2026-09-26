"""文档知识图谱编排层: 抽取触发、回填、ACL 过滤查询。

- 写入侧: 由 ``app.docs.service`` 在入库/删除后调用 ``build_for_doc`` / store 删除。
- 读取侧: ``get_graph`` 先按当前用户身份从 PostgreSQL ``documents``(ACL 事实来源)
  算出可访问的 ``doc_key`` 集合, 再交给 Neo4j 裁剪子图 —— 权限只在查询出口即时生效,
  因此改文档可见性无需回写图谱。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Document, DocumentTag, Tag
from app.db.session import get_session_factory
from app.kg import store
from app.kg.extract import extract_doc_graph
from app.schemas import KnowledgeChunk
from app.security.acl import Principal, is_allowed

logger = logging.getLogger(__name__)


def _doc_chunk(doc: Document) -> KnowledgeChunk:
    """把一条 Document 元数据包装成 KnowledgeChunk, 复用 acl.is_allowed 做逐条判定。

    is_allowed 只读 visibility/owner_id/dept_id/allowed_roles 四个 ACL 字段, 内容与
    标识字段留空即可 —— 这样文档级权限判定与检索侧走同一套事实来源(单点定义)。
    """
    return KnowledgeChunk(
        chunk_id="",
        doc_id=doc.doc_key,
        title=doc.name or "",
        content="",
        source="",
        visibility=doc.visibility or "public",
        owner_id=doc.owner_id or "",
        dept_id=doc.dept_id or "",
        allowed_roles=doc.allowed_roles or "",
    )


async def _load_doc_with_tags(doc_key: str) -> tuple[Document | None, list[str]]:
    factory = get_session_factory()
    async with factory() as session:
        doc = (
            await session.execute(select(Document).where(Document.doc_key == doc_key))
        ).scalar_one_or_none()
        if doc is None:
            return None, []
        tag_rows = (
            await session.execute(
                select(Tag.name)
                .join(DocumentTag, DocumentTag.tag_id == Tag.id)
                .where(DocumentTag.doc_key == doc_key)
            )
        ).scalars().all()
    return doc, list(tag_rows)


async def build_for_doc(doc_key: str) -> dict[str, Any]:
    """为一篇已入库文档抽取实体关系并写入图谱; 关闭开关/缺数据/失败均安全返回。"""
    if not get_settings().doc_kg_enabled:
        return {"doc_key": doc_key, "built": False, "reason": "disabled"}
    doc, tags = await _load_doc_with_tags(doc_key)
    if doc is None:
        return {"doc_key": doc_key, "built": False, "reason": "not_found"}
    text = doc.parsed_text or ""
    kg = await extract_doc_graph(doc.name or "", tags, text)
    if kg.is_empty:
        logger.info("文档 %s 未抽出实体关系, 跳过建图", doc_key)
        return {"doc_key": doc_key, "built": False, "reason": "empty"}
    ok = await store.upsert_document_graph(
        doc_key, doc.name or "", doc.ext or "", doc.modality or "text",
        kg.entities, kg.relations,
    )
    return {
        "doc_key": doc_key,
        "built": ok,
        "entities": len(kg.entities),
        "relations": len(kg.relations),
    }


async def rebuild_all(limit_docs: int | None = None) -> dict[str, Any]:
    """回填: 逐篇为已入库文档重建图谱(顺序执行, 单篇失败不影响其余)。"""
    if not get_settings().doc_kg_enabled:
        return {"built": 0, "total": 0, "reason": "disabled"}
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(Document.doc_key).order_by(Document.updated_at.desc())
        if limit_docs:
            stmt = stmt.limit(limit_docs)
        keys = (await session.execute(stmt)).scalars().all()
    built = 0
    for key in keys:
        try:
            res = await build_for_doc(key)
            if res.get("built"):
                built += 1
        except Exception as exc:  # noqa: BLE001 - 回填逐篇容错, 单篇失败继续
            logger.warning("回填文档图谱失败 doc_key=%s: %s", key, exc)
    return {"built": built, "total": len(keys)}


async def get_graph(
    principal: Principal,
    focus: str | None = None,
    hops: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """按当前用户身份返回 ACL 过滤后的图谱子图(供前端 G6 渲染)。"""
    settings = get_settings()
    if not settings.doc_kg_enabled:
        return {"nodes": [], "edges": [], "truncated": False, "enabled": False}
    # ACL 事实来源是 PostgreSQL documents; 先算可访问 doc_key 再交给 Neo4j 裁剪。
    factory = get_session_factory()
    async with factory() as session:
        docs = (await session.execute(select(Document))).scalars().all()
    allowed_keys = [d.doc_key for d in docs if is_allowed(_doc_chunk(d), principal)]
    if focus and focus not in allowed_keys:
        # 聚焦文档本身无权访问 -> 视为无效 focus, 退回概览
        focus = None
    graph = await store.query_subgraph(allowed_keys, focus=focus, hops=hops, limit=limit)
    graph["enabled"] = True
    return graph
