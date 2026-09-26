"""Document upload / ingestion / metadata service layer."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.models import Document, DocumentTag, Tag
from app.db.session import get_session_factory
from app.docs.parsers import modality_of, parse_blocks, supported_extensions
from app.rag.embeddings import OllamaEmbedder
from app.rag.ingest import compute_doc_id, ingest_blocks
from app.rag.vectorstore import PgVectorStore
from app.schemas import DocVisibility
from app.security.acl import format_allowed_roles

logger = logging.getLogger(__name__)

# 后台图谱构建任务的强引用: asyncio 只弱引用 task, 不持引用会被 GC 掉。
_kg_bg_tasks: set = set()

TAG_PROMPT = """你是企业知识库的分类助手。基于文档内容,给出 3~5 个中文分类标签。
优先复用已有标签: {existing}

严格输出 JSON 数组, 不要输出其他内容, 例如: ["财务","报销","制度"]

文档内容节选:
{excerpt}"""


class UploadError(ValueError):
    """Raised for invalid uploads (bad type / oversize / parse failure)."""


def _upload_dir() -> Path:
    settings = get_settings()
    path = Path(settings.upload_dir)
    if not path.is_absolute():
        path = settings.base_dir / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(filename: str) -> str:
    """Strip path components; keep only the bare file name."""
    name = Path(filename).name.strip()
    if not name or name.startswith("."):
        raise UploadError("非法文件名")
    return name


def save_upload(filename: str, data: bytes) -> tuple[str, Path, str]:
    """Validate and persist an uploaded file. Returns (doc_key, path, ext)."""
    name = _safe_filename(filename)
    ext = Path(name).suffix.lower()
    if name.lower().endswith(".transcript.txt"):
        ext = ".transcript.txt"
    if ext not in supported_extensions() and not any(
        name.lower().endswith(e) for e in (".srt", ".vtt")
    ):
        raise UploadError(f"不支持的文件类型: {ext}")
    max_bytes = get_settings().upload_max_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise UploadError(f"文件超过大小限制({get_settings().upload_max_mb}MB)")

    stem = name[: -len(ext)] if name.lower().endswith(ext) else Path(name).stem
    doc_key = compute_doc_id(stem, ext)
    dest_dir = _upload_dir() / doc_key
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    dest.write_bytes(data)
    return doc_key, dest, ext


async def check_existing(doc_key: str) -> Document | None:
    """Look up an existing document (same name+ext) by doc_key."""
    async with get_session_factory()() as session:
        result = await session.execute(select(Document).where(Document.doc_key == doc_key))
        return result.scalar_one_or_none()


async def _existing_tag_names() -> list[str]:
    async with get_session_factory()() as session:
        result = await session.execute(select(Tag.name).order_by(Tag.name))
        return [r[0] for r in result.all()]


async def suggest_tags(text: str) -> list[str]:
    """Ask the LLM for 3~5 category tags; falls back to [] on any failure."""
    from app.llm import get_chat_model

    settings = get_settings()
    existing = await _existing_tag_names()
    llm = get_chat_model(settings.llm_model, temperature=0.2)
    prompt = TAG_PROMPT.format(
        existing="、".join(existing) if existing else "(暂无)",
        excerpt=text[:3000],
    )
    try:
        resp = await llm.ainvoke(prompt)
        raw = str(resp.content)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        tags = json.loads(match.group(0)) if match else []
        return [str(t).strip() for t in tags if str(t).strip()][:5]
    except Exception as exc:
        logger.warning("tag suggestion failed: %s", exc)
        return []


def normalize_acl(
    visibility: str, owner_id: str, dept_id: str, allowed_roles: list[str] | str
) -> dict[str, str]:
    """Validate + canonicalize the document ACL into its storage form.

    Only the fields relevant to the chosen visibility are kept (e.g. a
    ``dept`` doc ignores allowed_roles), so the metadata table and the
    knowledge_chunks rows always store a single consistent representation.
    """
    try:
        vis = DocVisibility(visibility or "public").value
    except ValueError as exc:
        raise UploadError(f"非法可见性: {visibility}") from exc
    acl = {"visibility": vis, "owner_id": "", "dept_id": "", "allowed_roles": ""}
    if vis == DocVisibility.PRIVATE.value:
        acl["owner_id"] = (owner_id or "").strip()[:64]
    elif vis == DocVisibility.DEPT.value:
        dept = (dept_id or "").strip()
        if not dept:
            raise UploadError("部门可见文档必须指定 dept_id")
        acl["dept_id"] = dept[:64]
    elif vis == DocVisibility.ROLE.value:
        roles = format_allowed_roles(allowed_roles)
        if not roles:
            raise UploadError("角色可见文档必须指定 allowed_roles")
        acl["allowed_roles"] = roles[:128]
    return acl


async def ingest_confirmed(
    doc_key: str,
    filename: str,
    tags: list[str],
    uploader: str,
    visibility: str = "public",
    dept_id: str = "",
    allowed_roles: list[str] | str = "",
) -> dict[str, Any]:
    """Phase-2 ingest: parse -> chunk -> embed -> pgvector overwrite -> metadata.

    The document ACL (visibility/owner/dept/roles) is validated here, stamped
    onto every knowledge chunk row for retrieval-time metadata filtering, and
    stored on the ``documents`` row as the source of truth.

    Returns a summary dict for the API response.
    """
    path = _upload_dir() / doc_key / _safe_filename(filename)
    if not path.exists():
        raise UploadError("上传文件不存在, 请重新上传")

    acl = normalize_acl(visibility, uploader, dept_id, allowed_roles)
    modality = modality_of(path)
    _, blocks = await parse_blocks(path)
    parsed_text = "\n\n".join(b.text for b in blocks)

    store = PgVectorStore()
    embedder = OllamaEmbedder()
    title = path.stem
    chunk_count = await ingest_blocks(
        doc_key, path.name, title, str(path), modality, blocks, store, embedder, acl=acl
    )
    if not chunk_count:
        raise UploadError("文档解析后无有效内容")

    # --- document metadata (transactional) ---
    # Two pages uploading the same file race here: both SELECTs miss, both
    # INSERT, the loser hits the doc_key unique index. The explicit flush
    # surfaces the conflict before the tag work; one retry then sees the
    # winner's committed row and falls through to the update path.
    factory = get_session_factory()
    for attempt in range(3):
        try:
            async with factory() as session:
                async with session.begin():
                    existing = await session.execute(
                        select(Document).where(Document.doc_key == doc_key)
                    )
                    doc = existing.scalar_one_or_none()
                    if doc is None:
                        doc = Document(doc_key=doc_key, name=title, ext=path.suffix.lower(),
                                       modality=modality, created_by=uploader)
                        session.add(doc)
                        await session.flush()  # surface duplicate doc_key early
                    doc.file_path = str(path)
                    doc.parsed_text = parsed_text
                    doc.chunk_count = chunk_count
                    doc.size_bytes = path.stat().st_size
                    doc.modality = modality
                    # ACL 以元数据表为事实来源, 同时冗余写入向量表供检索裁剪
                    doc.visibility = acl["visibility"]
                    doc.owner_id = acl["owner_id"]
                    doc.dept_id = acl["dept_id"]
                    doc.allowed_roles = acl["allowed_roles"]

                    await session.execute(delete(DocumentTag).where(DocumentTag.doc_key == doc_key))
                    for tag_name in dict.fromkeys(t.strip() for t in tags if t.strip()):
                        tag_id = await _get_or_create_tag(session, tag_name)
                        session.add(DocumentTag(doc_key=doc_key, tag_id=tag_id))
            break
        except IntegrityError:
            if attempt == 2:
                raise
            logger.warning("doc_key %s concurrently inserted, retrying metadata write", doc_key)

    # --- refresh the ES BM25 index of the running assistant, if any ---
    try:
        from app.assistant.graph import get_orchestrator

        await get_orchestrator().refresh_knowledge()
    except Exception as exc:  # ES index rebuilds on next startup anyway
        logger.warning("es bm25 refresh after ingest failed: %s", exc)

    # --- build the document knowledge graph (LLM 抽取耗时, 后台执行不阻塞入库响应) ---
    if get_settings().doc_kg_enabled:
        try:
            import asyncio

            from app.kg.service import build_for_doc

            task = asyncio.create_task(build_for_doc(doc_key))
            _kg_bg_tasks.add(task)
            task.add_done_callback(_kg_bg_tasks.discard)
        except Exception as exc:  # noqa: BLE001 - 建图失败不影响入库结果
            logger.warning("kg build scheduled after ingest failed: %s", exc)

    return {
        "doc_key": doc_key,
        "chunk_count": chunk_count,
        "tags": tags,
        "modality": modality,
        "visibility": acl["visibility"],
    }


async def update_document_acl(
    doc_key: str,
    visibility: str,
    dept_id: str = "",
    allowed_roles: list[str] | str = "",
    operator: str = "",
) -> dict[str, Any]:
    """Change a document's visibility: metadata table (truth) + chunk ACL columns.

    The vector rows carry the ACL used for retrieval-time filtering, so both
    stores are updated in one call; the assistant's ES BM25 index rebuilds
    afterwards so the sparse channel reflects the new permissions too.
    """
    factory = get_session_factory()
    async with factory() as session:
        doc = (
            await session.execute(select(Document).where(Document.doc_key == doc_key))
        ).scalar_one_or_none()
        if doc is None:
            raise UploadError("文档不存在或已删除")
        # owner_id 固定为文档上传者, 不随本次修改漂移。
        acl = normalize_acl(visibility, doc.created_by or operator, dept_id, allowed_roles)
        doc.visibility = acl["visibility"]
        doc.owner_id = acl["owner_id"]
        doc.dept_id = acl["dept_id"]
        doc.allowed_roles = acl["allowed_roles"]
        await session.commit()

    updated = await PgVectorStore().update_acl_by_doc(
        doc_key, acl["visibility"], acl["owner_id"], acl["dept_id"], acl["allowed_roles"]
    )

    # --- refresh the ES BM25 index of the running assistant, if any ---
    try:
        from app.assistant.graph import get_orchestrator

        await get_orchestrator().refresh_knowledge()
    except Exception as exc:  # ES index rebuilds on next startup anyway
        logger.warning("es bm25 refresh after acl update failed: %s", exc)

    logger.info("document acl updated: doc_key=%s visibility=%s operator=%s", doc_key, acl["visibility"], operator)
    result: dict[str, Any] = {"doc_key": doc_key, **acl, "chunks_updated": updated}
    if updated == 0:
        # 向量表里没有该文档的任何块 (常见于表被重建/迁移后未重新入库):
        # 权限只写进了元数据表, 检索侧不会生效, 必须显式提示而不是静默"成功"。
        result["warning"] = (
            "向量库中未找到该文档的知识块, 权限变更不会生效; 请重新入库该文档。"
        )
        logger.warning(
            "acl update matched 0 vector chunks for doc_key=%s; document likely needs re-ingest",
            doc_key,
        )
    return result


async def delete_document(doc_key: str) -> dict[str, Any]:
    """Delete a document: knowledge chunks + metadata + upload files."""
    factory = get_session_factory()
    async with factory() as session:
        doc = (
            await session.execute(select(Document).where(Document.doc_key == doc_key))
        ).scalar_one_or_none()
        if doc is None:
            raise UploadError("文档不存在或已删除")
        name, file_path = doc.name, doc.file_path

        # Vector rows first; the metadata row is the source of truth, so a
        # vector failure aborts before metadata is lost (chunk leftovers can
        # be purged by a re-ingest, but a lost metadata row orphans nothing).
        await PgVectorStore().delete_by_doc(doc_key)
        await session.execute(delete(DocumentTag).where(DocumentTag.doc_key == doc_key))
        await session.execute(delete(Document).where(Document.doc_key == doc_key))
        await session.commit()

    # remove the uploaded file directory (best effort)
    if file_path:
        shutil.rmtree(Path(file_path).parent, ignore_errors=True)

    # --- drop this document's nodes from the knowledge graph (best effort) ---
    if get_settings().doc_kg_enabled:
        try:
            from app.kg import store as kg_store

            await kg_store.delete_document_graph(doc_key)
        except Exception as exc:  # noqa: BLE001 - 图谱删除失败不影响文档删除结果
            logger.warning("kg delete after document delete failed: %s", exc)

    # --- refresh the ES BM25 index of the running assistant, if any ---
    try:
        from app.assistant.graph import get_orchestrator

        await get_orchestrator().refresh_knowledge()
    except Exception as exc:  # ES index rebuilds on next startup anyway
        logger.warning("es bm25 refresh after delete failed: %s", exc)

    logger.info("document deleted: doc_key=%s name=%s", doc_key, name)
    return {"doc_key": doc_key, "name": name}


async def _get_or_create_tag(session, name: str) -> int:
    result = await session.execute(select(Tag).where(Tag.name == name))
    tag = result.scalar_one_or_none()
    if tag is None:
        tag = Tag(name=name, source="custom")
        session.add(tag)
        await session.flush()
    return int(tag.id)


async def get_meta_map(doc_keys: list[str]) -> dict[str, dict[str, Any]]:
    """Batch-load document metadata (name/tags/modality) for chat citations."""
    if not doc_keys:
        return {}
    factory = get_session_factory()
    async with factory() as session:
        docs = (
            await session.execute(select(Document).where(Document.doc_key.in_(doc_keys)))
        ).scalars().all()
        tag_rows = (
            await session.execute(
                select(DocumentTag.doc_key, Tag.name)
                .join(Tag, Tag.id == DocumentTag.tag_id)
                .where(DocumentTag.doc_key.in_(doc_keys))
            )
        ).all()
    tags_by_doc: dict[str, list[str]] = {}
    for key, tag_name in tag_rows:
        tags_by_doc.setdefault(key, []).append(tag_name)
    return {
        d.doc_key: {"name": d.name, "modality": d.modality, "tags": tags_by_doc.get(d.doc_key, [])}
        for d in docs
    }


async def list_documents() -> list[dict[str, Any]]:
    """Document list with tags for the management page."""
    factory = get_session_factory()
    async with factory() as session:
        docs = (await session.execute(select(Document).order_by(Document.updated_at.desc()))).scalars().all()
        keys = [d.doc_key for d in docs]
        meta = await get_meta_map(keys) if keys else {}
    return [
        {
            "doc_key": d.doc_key,
            "name": d.name,
            "ext": d.ext,
            "modality": d.modality,
            "chunk_count": d.chunk_count,
            "size_bytes": d.size_bytes,
            "tags": meta.get(d.doc_key, {}).get("tags", []),
            "created_by": d.created_by,
            "visibility": getattr(d, "visibility", "") or "public",
            "dept_id": getattr(d, "dept_id", "") or "",
            "allowed_roles": [r for r in (getattr(d, "allowed_roles", "") or "").strip(",").split(",") if r],
            "updated_at": d.updated_at.isoformat(timespec="seconds") if d.updated_at else "",
        }
        for d in docs
    ]


async def list_tags() -> list[dict[str, Any]]:
    """All tags for the management page."""
    factory = get_session_factory()
    async with factory() as session:
        tags = (await session.execute(select(Tag).order_by(Tag.name))).scalars().all()
    return [{"id": t.id, "name": t.name, "source": t.source} for t in tags]
