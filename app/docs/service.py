"""Document upload / ingestion / metadata service layer."""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.bodies.store import get_body_store
from app.db.models import DocChunkRow, DocParentRow, Document, DocumentTag, Tag
from app.db.session import get_session_factory
from app.docs.normalize import normalize_text
from app.docs.parsers import modality_of, parse_blocks, supported_extensions
from app.rag.embeddings import OllamaEmbedder
from app.rag.ingest import build_structure, compute_doc_id, ingest_blocks
from app.rag.vectorstore import ChunkStore, ParentStore
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


async def _set_doc_status(doc_key: str, status: str) -> None:
    """Best-effort 更新 documents.status(入库失败时标 failed, 供检索门禁生效)。"""
    try:
        async with get_session_factory()() as session:
            await session.execute(
                update(Document).where(Document.doc_key == doc_key).values(status=status)
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - 状态刷新失败不掩盖原始异常
        logger.warning("set doc status=%s failed for %s: %s", status, doc_key, exc)


async def ingest_confirmed(
    doc_key: str,
    filename: str,
    tags: list[str],
    uploader: str,
    visibility: str = "public",
    dept_id: str = "",
    allowed_roles: list[str] | str = "",
) -> dict[str, Any]:
    """Phase-2 ingest: 元数据先行 + 内容后发(修掉孤儿向量)。

    顺序(不得违背, 见方案 2.6):
      1. 解析 + 归一化(raw -> normalized -> structure);
      2. PG 事务1: upsert documents 行, status='ingesting', chunk_count=0, body_stored=false
         (新文档也先插行, 不再"先建向量后补元数据");
      3. Mongo save_doc_body(失败 -> status='failed' 后抛 UploadError, PG 不留无元数据行);
      4. ingest_blocks(父块正文入 Mongo + 父子块入 PG);
      5. PG 事务2: chunk_count=N, body_stored=true, status='ready' + tags 重写;
      6. 仅当转为 ready 后才 refresh_knowledge + 后台建图。
    """
    path = _upload_dir() / doc_key / _safe_filename(filename)
    if not path.exists():
        raise UploadError("上传文件不存在, 请重新上传")

    acl = normalize_acl(visibility, uploader, dept_id, allowed_roles)
    modality = modality_of(path)
    _, blocks = await parse_blocks(path)
    raw = "\n\n".join(b.text for b in blocks)
    if not raw.strip():
        raise UploadError("文档解析后无有效内容")
    normalized = normalize_text(raw)
    structure = build_structure(blocks, normalized)
    title = path.stem

    factory = get_session_factory()
    # --- 事务1: 元数据先行, 置 ingesting(幂等 upsert, 并发插入重试) ---
    for attempt in range(3):
        try:
            async with factory() as session:
                async with session.begin():
                    doc = (
                        await session.execute(
                            select(Document).where(Document.doc_key == doc_key)
                        )
                    ).scalar_one_or_none()
                    if doc is None:
                        doc = Document(doc_key=doc_key, name=title, ext=path.suffix.lower(),
                                       modality=modality, created_by=uploader)
                        session.add(doc)
                        await session.flush()
                    doc.file_path = str(path)
                    doc.size_bytes = path.stat().st_size
                    doc.modality = modality
                    doc.status = "ingesting"
                    doc.chunk_count = 0
                    doc.body_stored = False
                    doc.visibility = acl["visibility"]
                    doc.owner_id = acl["owner_id"]
                    doc.dept_id = acl["dept_id"]
                    doc.allowed_roles = acl["allowed_roles"]
            break
        except IntegrityError:
            if attempt == 2:
                raise
            logger.warning("doc_key %s concurrently inserted, retrying pre-ingest metadata", doc_key)

    # --- 步骤3: 整篇正文入 Mongo(失败标 failed 并抛, PG 不会留下 ready 的无正文行) ---
    bodies = get_body_store()
    try:
        await bodies.save_doc_body(
            doc_key, raw=raw, normalized=normalized, structure=structure,
            meta={"name": title, "modality": modality, "source": str(path)},
        )
    except Exception as exc:  # noqa: BLE001 - Mongo 写失败转 502(经 UploadError)
        await _set_doc_status(doc_key, "failed")
        logger.error("save_doc_body 失败 doc_key=%s: %s", doc_key, exc)
        raise UploadError(f"正文存储不可用, 入库失败: {exc}") from exc

    # --- 步骤4: 父子块发布(父块正文入 Mongo + 父子块入 PG, 增量 embed) ---
    store = ChunkStore()
    embedder = OllamaEmbedder()
    try:
        chunk_count = await ingest_blocks(
            doc_key, path.name, title, str(path), modality, blocks, store, embedder,
            acl=acl, parent_store=ParentStore(), bodies=bodies, normalized_text=normalized,
        )
    except Exception as exc:  # noqa: BLE001
        await _set_doc_status(doc_key, "failed")
        logger.error("ingest_blocks 失败 doc_key=%s: %s", doc_key, exc)
        raise UploadError(f"知识块发布失败: {exc}") from exc
    if not chunk_count:
        await _set_doc_status(doc_key, "failed")
        raise UploadError("文档解析后无有效内容")

    # --- 事务2: 发布态转 ready + chunk_count + tags 重写(迁移期仍写 parsed_text 供回退) ---
    for attempt in range(3):
        try:
            async with factory() as session:
                async with session.begin():
                    doc = (
                        await session.execute(
                            select(Document).where(Document.doc_key == doc_key)
                        )
                    ).scalar_one_or_none()
                    if doc is None:  # 极端: 事务1 后被并发删除
                        raise UploadError("文档元数据丢失, 请重新入库")
                    doc.parsed_text = raw
                    doc.chunk_count = chunk_count
                    doc.body_stored = True
                    doc.status = "ready"
                    await session.execute(delete(DocumentTag).where(DocumentTag.doc_key == doc_key))
                    for tag_name in dict.fromkeys(t.strip() for t in tags if t.strip()):
                        tag_id = await _get_or_create_tag(session, tag_name)
                        session.add(DocumentTag(doc_key=doc_key, tag_id=tag_id))
            break
        except IntegrityError:
            if attempt == 2:
                raise
            logger.warning("doc_key %s tag concurrently inserted, retrying", doc_key)

    # --- 步骤6: 仅发布成功后刷新检索缓存/ES 索引与后台建图 ---
    try:
        from app.assistant.graph import get_orchestrator

        await get_orchestrator().refresh_knowledge()
    except Exception as exc:  # ES index rebuilds on next startup anyway
        logger.warning("es bm25 refresh after ingest failed: %s", exc)

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

    updated_chunks, updated_parents = await ChunkStore().update_acl_by_doc(
        doc_key, acl["visibility"], acl["owner_id"], acl["dept_id"], acl["allowed_roles"]
    )

    # --- refresh the ES BM25 index of the running assistant, if any ---
    try:
        from app.assistant.graph import get_orchestrator

        await get_orchestrator().refresh_knowledge()
    except Exception as exc:  # ES index rebuilds on next startup anyway
        logger.warning("es bm25 refresh after acl update failed: %s", exc)

    logger.info("document acl updated: doc_key=%s visibility=%s operator=%s", doc_key, acl["visibility"], operator)
    result: dict[str, Any] = {
        "doc_key": doc_key, **acl,
        "chunks_updated": updated_chunks, "parents_updated": updated_parents,
    }
    if updated_chunks == 0:
        # 新子块表里没有该文档的任何块 (常见于表被重建/迁移后未重新入库):
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
    """Delete a document: PG 父子行+元数据同事务 -> 提交成功后再删 Mongo 正文。

    顺序: 先删子块/父块(PG), 再删元数据(PG), 同事务提交; 只有 PG 提交成功后才
    清 Mongo。PG 是事实来源, Mongo 残留由 --prune 回收(删除中途崩溃只会产生
    无 ACL 风险的孤儿文本, 不会产生丢元数据的孤儿向量)。
    """
    factory = get_session_factory()
    async with factory() as session:
        doc = (
            await session.execute(select(Document).where(Document.doc_key == doc_key))
        ).scalar_one_or_none()
        if doc is None:
            raise UploadError("文档不存在或已删除")
        name, file_path = doc.name, doc.file_path

    # 父子块与元数据同事务删除(同一 session, 不留"删了子块没删元数据"的中间态)。
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(DocChunkRow).where(DocChunkRow.doc_id == doc_key)
            )
            await session.execute(
                delete(DocParentRow).where(DocParentRow.doc_id == doc_key)
            )
            await session.execute(delete(DocumentTag).where(DocumentTag.doc_key == doc_key))
            await session.execute(delete(Document).where(Document.doc_key == doc_key))

    # PG 提交成功后再清 Mongo(失败仅告警, 残留由 --prune 回收)。
    bodies = get_body_store()
    try:
        await bodies.delete_parents_by_doc(doc_key)
        await bodies.delete_doc_body(doc_key)
    except Exception as exc:  # noqa: BLE001 - Mongo 删除失败不回滚已提交的 PG 删除
        logger.warning("mongo body cleanup after delete failed doc_key=%s: %s", doc_key, exc)

    # remove the uploaded file directory (best effort).
    # 以"当前 upload_dir + doc_key"重新定位规范目录再删, 不直接信任 DB 里存的
    # file_path: 它可能是容器内绝对路径(/data/uploads/...), 宿主机直跑时与真实
    # 位置(<项目>/data/uploads/<doc_key>)不一致, rmtree 会因路径不存在而静默失败,
    # 表现为"文档已删但源文件还在"。doc_key 跨环境稳定, 故用它重算路径最可靠。
    upload_doc_dir = _upload_dir() / doc_key
    shutil.rmtree(upload_doc_dir, ignore_errors=True)
    # 兼容历史数据: file_path 若指向规范目录之外(如迁移前残留), 也补删一次。
    if file_path:
        legacy_dir = Path(file_path).parent
        if legacy_dir != upload_doc_dir:
            shutil.rmtree(legacy_dir, ignore_errors=True)
    if upload_doc_dir.exists():
        logger.warning(
            "upload dir still present after delete (占用/权限?): %s", upload_doc_dir
        )

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


async def docs_not_ready(doc_ids: Sequence[str]) -> set[str]:
    """返回 status != 'ready' 的 doc_id 集合(发布态门禁, 供检索出口一次性批量判)。

    防"正在入库的半篇文档"被检索到: ingest_confirmed 先置 ingesting, 完成才转 ready,
    故未就绪文档的块即使已进入新表也不会进入回答。调用方在 assemble_parents 后的
    出口复核里一次性传入命中 doc_id, 不逐块查。
    """
    if not doc_ids:
        return set()
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(Document.doc_key, Document.status).where(
                    Document.doc_key.in_(list(dict.fromkeys(doc_ids)))
                )
            )
        ).all()
    ready = {r.doc_key for r in rows if (r.status or "ready") == "ready"}
    # 没有元数据行的 doc_id 也视为未就绪(孤儿向量, 默认拒); 仅已存在且 ready 的放行。
    known = {r.doc_key for r in rows}
    blocked = {d for d in doc_ids if d not in ready}
    blocked |= {d for d in doc_ids if d not in known}
    return blocked


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
