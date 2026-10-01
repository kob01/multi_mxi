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
from app.bodies.store import get_body_store
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


async def _load_doc_meta(doc_key: str) -> tuple[Any | None, list[str]]:
    """列白名单加载建图所需元数据(**排除 parsed_text**), 避免顺手拉全篇正文。"""
    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                select(Document.name, Document.ext, Document.modality).where(
                    Document.doc_key == doc_key
                )
            )
        ).first()
        if row is None:
            return None, []
        tag_rows = (
            await session.execute(
                select(Tag.name)
                .join(DocumentTag, DocumentTag.tag_id == Tag.id)
                .where(DocumentTag.doc_key == doc_key)
            )
        ).scalars().all()
    return row, list(tag_rows)


async def build_for_doc(doc_key: str) -> dict[str, Any]:
    """为一篇已入库文档抽取实体关系并写入图谱; 关闭开关/缺数据/失败均安全返回。

    正文从 Mongo ``doc_bodies`` 取 head(head 始终内联, 抽取只用开头), 不再从 PG
    ``documents.parsed_text`` 拉全篇 —— 避免建图路径顺手把整篇正文读进内存。
    """
    if not get_settings().doc_kg_enabled:
        return {"doc_key": doc_key, "built": False, "reason": "disabled"}
    doc, tags = await _load_doc_meta(doc_key)
    if doc is None:
        return {"doc_key": doc_key, "built": False, "reason": "not_found"}
    bodies = get_body_store()
    text = await bodies.get_doc_body(doc_key, head_only=True)
    if not text:
        # head 缺失(旧数据/溢出未写 head): 退回 normalized 前缀, 不阻断建图。
        text = (await bodies.get_doc_body(doc_key, field="normalized"))[: get_settings().kg_extraction_max_chars]
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
    """按当前用户身份返回 ACL 过滤后的图谱子图(供前端 G6 渲染)。

    返回的 ``edges`` 已经过边级裁剪: 没有任何可访问文档断言过的 ``KG_REL`` 不给出
    (无溯源的历史边同样不给), 这部分数量在 ``hidden_edges`` 里, 前端据此提示跑迁移。
    """
    settings = get_settings()
    if not settings.doc_kg_enabled:
        return {"nodes": [], "edges": [], "truncated": False, "enabled": False}
    # ACL 事实来源是 PostgreSQL documents; 先算可访问 doc_key 再交给 Neo4j 裁剪。
    # 列白名单只取 ACL 四列 + doc_key(不拉 parsed_text, 避免整表扫描顺手读全篇正文)。
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    Document.doc_key, Document.visibility, Document.owner_id,
                    Document.dept_id, Document.allowed_roles,
                )
            )
        ).all()
    allowed_keys = [
        r.doc_key
        for r in rows
        if is_allowed(
            KnowledgeChunk(
                chunk_id="", doc_id=r.doc_key, title="", content="", source="",
                visibility=r.visibility or "public", owner_id=r.owner_id or "",
                dept_id=r.dept_id or "", allowed_roles=r.allowed_roles or "",
            ),
            principal,
        )
    ]
    if focus and focus not in allowed_keys:
        # 聚焦文档本身无权访问 -> 视为无效 focus, 退回概览
        focus = None
    graph = await store.query_subgraph(allowed_keys, focus=focus, hops=hops, limit=limit)
    graph["enabled"] = True
    return graph
