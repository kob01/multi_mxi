"""Document upload / ingestion / metadata service layer.

异步入库下的分工(与 app/docs/pipeline.py 分界):
- 本模块只处理"提交 + 查询 + 事实源事务": 落盘、建上传行、建任务、改权限/删文档,
  以及同事务写 outbox 事件;
- 重活(OCR/解析/LLM tags/embedding/发布/ES/KG)全部在 pipeline 的阶段函数里, 由
  worker 跑(网关只在 ``ingest_async_enabled=false`` 的排障路径上进它们)。
因此这里不允许再出现"在请求里循环处理整篇文档"的代码。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db.models import (
    DocUpload,
    Document,
    DocumentTag,
    IngestTask,
    Tag,
)
from app.db.session import get_session_factory
from app.docs import pipeline
from app.docs.artifacts import get_artifact_store
from app.docs.parsers import supported_extensions
from app.rag.ingest import compute_doc_id
from app.rag.vectorstore import ChunkStore
from app.schemas import DocVisibility
from app.security.acl import (
    Principal,
    can_manage_document,
    format_allowed_roles,
    is_allowed_fields,
)

logger = logging.getLogger(__name__)

# 上传行与解析产物的保留窗口由 ``upload_retention_hours`` 决定(见 cleanup_expired_uploads)：
# 已提交入库的任务读的是 artifact，不是原件，所以原件被回收不会让任务跑不下去。

TAG_PROMPT = """你是企业知识库的分类助手。基于文档内容,给出 3~5 个中文分类标签。
优先复用已有标签: {existing}

严格输出 JSON 数组, 不要输出其他内容, 例如: ["财务","报销","制度"]

文档内容节选:
{excerpt}"""


class UploadError(ValueError):
    """Raised for invalid uploads (bad type / oversize / parse failure)."""


class DocumentForbidden(PermissionError):
    """文档管理动作(改可见性/删除)的主体不是所有者也不是 admin。

    单独开一类而不是复用 UploadError: 路由层要把两者分成 403 与 404, 而且
    "无权"是合规上最该留痕的一类事件, 不能被当成"文档不存在"静默掉。
    """


def _upload_dir() -> Path:
    settings = get_settings()
    path = Path(settings.upload_dir)
    if not path.is_absolute():
        path = settings.base_dir / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_upload_path(rel_path: str) -> Path:
    """把存库的**相对路径**还原成当前环境的绝对路径。

    为什么存相对路径: 绝对路径是容器内的 ``/data/uploads/<doc_key>/x.pdf``, 宿主侧脚本
    读到它指向的就不是同一个位置(历史上造成过"文档已删但源文件还在")。doc_key 与相对
    路径都跟环境无关, 拼上现场 ``upload_dir`` 才能跨宿主/容器解释。
    """
    if not rel_path:
        return _upload_dir()
    return _upload_dir() / rel_path.lstrip("/\\")


def _safe_filename(filename: str) -> str:
    """Strip path components; keep only the bare file name."""
    name = Path(filename).name.strip()
    if not name or name.startswith("."):
        raise UploadError("非法文件名")
    return name


def _validate_name(filename: str) -> tuple[str, str]:
    """只校名字(扩展名白名单): 暂存与定稿共用一道, 不依赖已落盘的字节。"""
    name = _safe_filename(filename)
    ext = Path(name).suffix.lower()
    if name.lower().endswith(".transcript.txt"):
        ext = ".transcript.txt"
    if ext not in supported_extensions() and not any(
        name.lower().endswith(e) for e in (".srt", ".vtt")
    ):
        raise UploadError(f"不支持的文件类型: {ext}")
    return name, ext


async def stage_upload(filename: str, upload) -> tuple[str, Path, str, str, int]:
    """流式落盘一份上传: 边读边写 + 边算 sha256, 超过上限立刻断并清临时文件。

    返回 ``(doc_key, path, ext, content_sha256, size_bytes)``。内容哈希是幂等键
    (同一份字节不重解析), 分块喂 hashlib 不额外花内存。

    为什么不能 ``await file.read()`` 一次读完再校大小(旧写法): 那是先把最多
    ``upload_max_mb`` 全读进内存。50MB × 几十人同时上传 = 几 GB 峰值, 在容器内存
    上限下直接 OOM; 而且那笔内存本可以完全不花(进磁盘就行)。
    先落 ``.uploading`` 再改名: 大小/类型校失败时不会留下半个正式文件。
    """
    name, ext = _validate_name(filename)
    max_bytes = get_settings().upload_max_mb * 1024 * 1024
    stem = name[: -len(ext)] if name.lower().endswith(ext) else Path(name).stem
    doc_key = compute_doc_id(stem, ext)
    dest_dir = await asyncio.to_thread(_upload_dir)
    target_dir = dest_dir / doc_key
    final = target_dir / name
    staging = target_dir / (name + ".uploading")
    received = 0
    digest = hashlib.sha256()
    try:
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        with staging.open("wb") as f:
            while True:
                piece = await upload.read(1024 * 1024)
                if not piece:
                    break
                received += len(piece)
                if received > max_bytes:
                    raise UploadError(f"文件超过大小限制({get_settings().upload_max_mb}MB)")
                digest.update(piece)
                # 每一块都交给线程写: 循环里的 ``f.write`` 会把磁盘 IO 压在事件循环上,
                # 几十 MB 文件就是几十次阻塞(全厂人的流式回复跟着卡)。
                await asyncio.to_thread(f.write, piece)
    except BaseException:
        # 失败/取消都先清掉临时体, 再把原异常照旧冒出(路由层的错误语义不变)
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    await asyncio.to_thread(os.replace, staging, final)
    return doc_key, final, ext, digest.hexdigest(), received


def save_upload(filename: str, data: bytes) -> tuple[str, Path, str]:
    """Validate and persist an uploaded file. Returns (doc_key, path, ext).

    保留同步入参接口(脚本/测试在用); HTTP 上传路径请走 :func:`submit_upload`
    (流式落盘 + 异步解析), 不要把整个文件先读到内存。大小校验两道都留: 调用方传的内存
    缓冲区也得有上限, 否则这接口自己就是一个无界内存入口。
    """
    name, ext = _validate_name(filename)
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _find_reusable_upload(doc_key: str, content_sha256: str) -> DocUpload | None:
    """找同 doc_key + 同字节且产物仍可用的上传行(幂等去重的唯一入口)。

    只看未过期的 ``parsed`` 行: 过了期原件与 artifact 都可能已被回收, 拿它当"已解析"
    会让入库阶段找不到产物。
    """
    now = _utcnow()
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(DocUpload)
                .where(
                    DocUpload.doc_key == doc_key,
                    DocUpload.content_sha256 == content_sha256,
                    DocUpload.status == "parsed",
                    DocUpload.expires_at > now,
                )
                # 按 created_at 取最新一条: 同一份字节可能已被上传多次(旧行逐步过期),
                # "最新的哪一个"才是应该被复用的那个; 按 update/expire 类字段排会得到
                # 随回收节奏漂移的结果, 表现为去重时灵时不灵。
                .order_by(DocUpload.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def submit_upload(filename: str, upload, uploader: str) -> dict[str, Any]:
    """``POST /api/docs/upload``: 流式落盘 + 建上传行 + 同事务发解析事件, 立即返回。

    这里刻意不做解析: OCR / 解析 / LLM tags 都是分钟级重活, 放进请求里等于
    "上传一篇扫描 PDF 就把一个网关进程钉住几分钟"。预览改由 ``GET /uploads/{id}``
    轮询解析产物 —— 预览与入库读的都是那一份产物。
    """
    doc_key, path, ext, content_sha256, size_bytes = await stage_upload(filename, upload)
    settings = get_settings()
    rel = str(path.relative_to(_upload_dir()))
    reused = await _find_reusable_upload(doc_key, content_sha256)
    overwritten = await check_existing(doc_key) is not None
    if reused is not None:
        # 幂等命中: 同一份字节刚刚已经解析过了, 直接复用那个 upload_id(不重做 OCR)。
        # 只复用**未过期**的行: 过期行的原件与 artifact 可能已被清理, 拿它当"已解析"
        # 会让入库阶段找不到产物。
        return {
            "upload_id": reused.upload_id, "doc_key": doc_key,
            "filename": reused.filename, "ext": ext, "size_bytes": size_bytes,
            "state": reused.status, "overwritten": overwritten,
            "reused": True,
            "poll": f"/api/docs/uploads/{reused.upload_id}",
        }
    upload_id = uuid.uuid4().hex[:32]
    now = _utcnow()
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            session.add(
                DocUpload(
                    upload_id=upload_id, doc_key=doc_key, filename=path.name[:255], ext=ext[:16],
                    size_bytes=size_bytes, content_sha256=content_sha256,
                    stored_rel_path=rel[:512], status="pending",
                    parser_version=settings.parser_version[:16],
                    uploaded_by=(uploader or "")[:64],
                    expires_at=now + timedelta(hours=settings.upload_retention_hours),
                    created_at=now, updated_at=now,
                )
            )
            # 事件与上传行同事务: 事务回滚则事件也不存在, 不会出现"没人解析但已发事件"。
            await pipeline.emit_event(
                session, pipeline.TOPIC_PARSE, doc_key, {"upload_id": upload_id, "doc_key": doc_key}
            )
    return {
        "upload_id": upload_id, "doc_key": doc_key, "filename": path.name,
        "ext": ext, "size_bytes": size_bytes, "state": "pending",
        "overwritten": overwritten, "reused": False,
        "poll": f"/api/docs/uploads/{upload_id}",
    }


async def get_upload_state(upload_id: str, principal: Principal | None) -> dict[str, Any]:
    """上传/解析状态查询(预览轮询入口)。

    预览内容与建议标签都来自解析产物本身, 所以这里不重新解析文件 —— 页面看到的
    与真正入库的就是同一份产物。
    """
    async with get_session_factory()() as session:
        up = (
            await session.execute(select(DocUpload).where(DocUpload.upload_id == upload_id))
        ).scalar_one_or_none()
        task = (
            await session.execute(
                select(IngestTask.task_id, IngestTask.stage, IngestTask.error)
                .where(IngestTask.upload_id == upload_id)
                .order_by(IngestTask.created_at.desc())
                .limit(1)
            )
        ).first()
    if up is None:
        raise UploadError("上传记录不存在或已过期, 请重新上传")
    # 预览是文档内容: 只有上传者本人或 admin 能读(与列表同一口径的 ACL 裁剪)。
    effective = principal or Principal(user_id="")
    if not can_manage_document(effective, up.uploaded_by or ""):
        raise DocumentForbidden("仅上传者本人或管理员可查看该上传的解析结果")
    out: dict[str, Any] = {
        "upload_id": up.upload_id, "doc_key": up.doc_key, "filename": up.filename,
        "ext": up.ext, "modality": up.modality, "size_bytes": up.size_bytes,
        "status": up.status, "attempt": up.attempt, "error": up.error,
        "preview": "", "preview_len": 0, "suggested_tags": [], "warnings": [],
        "overwritten": await check_existing(up.doc_key) is not None,
        "ingest_task_id": task.task_id if task else "",
        "ingest_stage": task.stage if task else "",
    }
    if up.status == "parsed":
        doc = await get_artifact_store().get(up.artifact_id or up.upload_id)
        if doc:
            out["preview"] = doc.get("preview", "")
            out["preview_len"] = int(doc.get("preview_len") or 0)
            out["suggested_tags"] = list(doc.get("suggested_tags") or [])
            out["warnings"] = list(doc.get("warnings") or [])
            out["modality"] = doc.get("modality") or up.modality
    return out


async def submit_ingest(
    upload_id: str,
    tags: list[str],
    uploader: str,
    visibility: str = "public",
    dept_id: str = "",
    allowed_roles: list[str] | str = "",
) -> dict[str, Any]:
    """``POST /api/docs/ingest``: 同步校参 + 建任务(幂等) + 同事务发事件, 返回 202 语义。

    参数校验留在同步链路: 非法可见性/缺 dept 这类错应该立刻告诉调用者, 而不应该
    "已受理"后在后台默默失败。

    幂等与并发都靠两个约束保证:
    - ``task_id`` 由 ``(doc_key, upload_id, content_sha256)`` 决出: 连点两次得到同一行;
    - 部分唯一索引 ``ux_ingest_tasks_active_doc``: 同一文档第二个进行中任务插不进来,
      冲突时回名已有 task_id(受理语义不变, 不是 400)。
    """
    acl = normalize_acl(visibility, uploader, dept_id, allowed_roles)
    async with get_session_factory()() as session:
        up = (
            await session.execute(select(DocUpload).where(DocUpload.upload_id == upload_id))
        ).scalar_one_or_none()
    if up is None:
        raise UploadError("上传记录不存在或已过期, 请重新上传")
    if up.status == "expired":
        raise UploadError("上传已超过保留期, 请重新上传")
    if up.status == "failed":
        raise UploadError(f"解析失败: {up.error or '未知原因'}")

    settings = get_settings()
    task_id = pipeline.make_task_id(up.doc_key, up.upload_id, up.content_sha256)
    params: dict[str, Any] = {
        "tags": [t.strip() for t in tags if t.strip()],
        "acl": acl,
        "uploader": (uploader or "")[:64],
        "filename": up.filename,
        "stored_rel_path": up.stored_rel_path,
        "size_bytes": up.size_bytes,
    }
    now = _utcnow()
    factory = get_session_factory()
    duplicate = False
    try:
        async with factory() as session:
            async with session.begin():
                # 先查行内主键: 同一 (doc_key, upload_id, content_sha256) 已经排过队就直接归并。
                # 靠 ON CONFLICT DO NOTHING 也能防住重复入库, 但那样无法区分"刚新建"
                # 与"已存在", 响应体会说谎(把已 ready 的任务报成 pending)。
                existing_task = (
                    await session.execute(
                        select(IngestTask.task_id, IngestTask.stage)
                        .where(IngestTask.task_id == task_id)
                        .with_for_update()
                    )
                ).first()
                if existing_task is not None:
                    duplicate = True
                else:
                    doc = (
                        await session.execute(
                            select(Document).where(Document.doc_key == up.doc_key).with_for_update()
                        )
                    ).scalar_one_or_none()
                    if doc is None:
                        # 新文档先置 ingesting: 它本来就没有块, 挡掉零成本, 并且页面能立刻
                        # 看到"入库中"。已在架文档不转 ingesting —— 发布是单事务原子操作,
                        # 把它打成 ingesting 只会让一篇正在服务的文档在队列+embed 期间检不到。
                        session.add(
                            Document(
                                doc_key=up.doc_key, name=Path(up.filename).stem or up.filename,
                                ext=up.ext, modality=up.modality, file_path=up.stored_rel_path,
                                size_bytes=up.size_bytes, created_by=uploader or "",
                                status="ingesting", chunk_count=0, body_stored=False,
                                visibility=acl["visibility"], owner_id=acl["owner_id"],
                                dept_id=acl["dept_id"], allowed_roles=acl["allowed_roles"],
                            )
                        )
                    await session.execute(
                        pg_insert(IngestTask)
                        .values(
                            task_id=task_id, doc_key=up.doc_key, upload_id=up.upload_id,
                            content_sha256=up.content_sha256, stage=pipeline.STAGE_PENDING,
                            params=params, max_attempts=settings.ingest_task_max_attempts,
                            result_json={}, created_at=now, updated_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=[IngestTask.task_id])
                    )
                    await pipeline.emit_event(
                        session, pipeline.TOPIC_INGEST, up.doc_key,
                        {"task_id": task_id, "doc_key": up.doc_key, "upload_id": up.upload_id},
                    )
    except IntegrityError:
        # 来自部分唯一索引 ux_ingest_tasks_active_doc: 同一文档已有**另一个**进行中的任务。
        # 把它回给调用方而不是报错(重复提交不是错误, 而是"已经排过了")。
        duplicate = True
        existing = await _active_task_id(up.doc_key)
        if existing:
            task_id = existing
        logger.info("doc_key=%s 已有进行中的入库任务, 本次提交归并到 %s", up.doc_key, task_id)

    # 响应里的 stage 必须是任务**当前**阶段: 归并到已存在的行时报 pending 会诱使
    # 调用方以为排了两次队。
    result_stage = await _current_stage(task_id)
    chunk_count = 0
    if duplicate:
        state = await get_task_state(task_id, Principal(user_id=(uploader or "").strip()))
        chunk_count = int(state.get("chunk_count") or 0)
    if not settings.ingest_async_enabled:
        # 紧急回退闸: 在请求内跑完四个阶段(只用于排障/未部 worker 的环内启动)。
        await pipeline.advance_task(task_id, "gateway-inline")
        result_stage = await _current_stage(task_id)
        state = await get_task_state(task_id, Principal(user_id=(uploader or "").strip()))
        chunk_count = int(state.get("chunk_count") or 0)
    return {
        "task_id": task_id,
        "doc_key": up.doc_key,
        "upload_id": up.upload_id,
        "accepted": settings.ingest_async_enabled,
        "stage": result_stage,
        "duplicate": duplicate,
        "chunk_count": chunk_count,
        "visibility": acl["visibility"],
        "tags": params["tags"],
        "poll": f"/api/docs/tasks/{task_id}",
    }


async def _active_task_id(doc_key: str) -> str | None:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(IngestTask.task_id)
                .where(IngestTask.doc_key == doc_key, IngestTask.stage.in_(pipeline.ACTIVE_STAGES))
                .order_by(IngestTask.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _current_stage(task_id: str) -> str:
    """读任务当前阶段(响应体与轮询入口同一口径, 不复用写前的字面量)。"""
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(IngestTask.stage).where(IngestTask.task_id == task_id)
            )
        ).scalar_one_or_none() or pipeline.STAGE_PENDING


async def get_task_state(task_id: str, principal: Principal | None = None) -> dict[str, Any]:
    """入库任务状态查询(前端入库轮询 + 运维排查同一入口)。

    任务进度会说出一篇文档的存在与其块数/失败原因 —— 与列表、预览同一口径做 ACL 裁剪:
    只有文档所有者或 admin 能读(无 principal 时按未登录处理, 只能读 public 文档)。
    """
    async with get_session_factory()() as session:
        task = (
            await session.execute(select(IngestTask).where(IngestTask.task_id == task_id))
        ).scalar_one_or_none()
        if task is None:
            raise UploadError("入库任务不存在(可能已被清理)")
        doc_key = task.doc_key
        requester = str((task.params or {}).get("uploader") or "")
        result = dict(task.result_json or {})
        payload = {
            "task_id": task.task_id,
            "doc_key": doc_key,
            "upload_id": task.upload_id,
            "stage": task.stage,
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            "error": task.error,
            "chunk_count": int(result.get("written") or 0),
            "warnings": list(result.get("warnings") or []),
            "marked": len(result.get("marked") or []),
            "heartbeat_at": task.heartbeat_at.isoformat(timespec="seconds") if task.heartbeat_at else "",
            "finished_at": task.finished_at.isoformat(timespec="seconds") if task.finished_at else "",
            "updated_at": task.updated_at.isoformat(timespec="seconds") if task.updated_at else "",
        }
    # 提交人总是看得到自己那一单: 本次提交的权限此刻可能还没发布完(新文档行已按
    # 提交的 ACL 建好), 拿它去裁进度的话, 给 dept/role 文档入库的人反而看不到自己刚提的任务。
    effective = principal or Principal(user_id="")
    if requester and effective.user_id and requester == effective.user_id:
        return payload
    doc = await check_existing(doc_key)
    if doc is not None:
        if not is_allowed_fields(
            getattr(doc, "visibility", "") or "public",
            getattr(doc, "owner_id", "") or (doc.created_by or ""),
            getattr(doc, "dept_id", "") or "",
            getattr(doc, "allowed_roles", "") or "",
            effective,
        ):
            raise DocumentForbidden("无权查看该文档的入库任务进度")
    return payload


async def delete_upload_dir(doc_key: str) -> None:
    """删除上传目录(由文档删除投影调用; 以当前 ``upload_dir`` + doc_key 重新定位)。

    不直接信 ``documents.file_path``: 它可能是容器内绝对路径, 宿主侧直跑时与真实位置
    不一致, rmtree 会静默失败而表现为"文档已删但源文件还在"。历史绝对路径也补删一次。
    """
    target = _upload_dir() / doc_key
    await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
    if target.exists():
        logger.warning("upload dir still present after delete (占用/权限?): %s", target)


# 旧 ``ingest_confirmed``(请求内跑完整条入库链路)已删除: 它的职责现在分成三处 ——
# ``submit_ingest``(同事务建任务+事件) / ``app/docs/pipeline.py`` 的阶段函数 / worker 的
# 认领与回收。把那些代码留在服务层会变成"第二条没人跑也没人测的入库路径"。


async def update_document_acl(
    doc_key: str,
    visibility: str,
    dept_id: str = "",
    allowed_roles: list[str] | str = "",
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Change a document's visibility: 一个事务改完元数据行 + 块表 ACL 列 + outbox 事件。

    为什么必须同事务: 向量行携带的是检索时前置裁剪用的 ACL, 与 ``documents`` 元数据分属
    两个事务时, "元数据已提交为 private、块表还是 public" 这段窗口里全文仍然能被
    全员检到 —— 那是越权而不是不一致。现在两边同提交, 不存在中间态。

    ES 侧的决策字段(权限词元)与缓存失效不在请求里做: 它们只是事实源的投影,
    由 worker 消费 ``doc.acl_changed`` 重索引该文档 + 定向失效。旧写法这里调
    ``refresh_knowledge()`` 触发全库 BM25 重建 —— 改一次权限就把全库重写一遍。

    闸门: 改可见性会同步改写该文全部向量块的 ACL 列, 把 private/dept 改成 public
    就是把文档发给全员检索 —— 所以只有所有者本人或 admin 能做(无 principal 也拒,
    不存在"谁都没登录所以全权限"的口子)。owner_id 固定为文档上传者, 不随本次修改
    漂移到客户端自报的调用者身上。
    """
    factory = get_session_factory()
    store = ChunkStore()
    async with factory() as session:
        async with session.begin():
            doc = (
                await session.execute(
                    select(Document).where(Document.doc_key == doc_key).with_for_update()
                )
            ).scalar_one_or_none()
            if doc is None:
                raise UploadError("文档不存在或已删除")
            if principal is None or not can_manage_document(principal, doc.created_by or ""):
                raise DocumentForbidden(
                    f"仅文档所有者({doc.created_by or '无主'})或管理员可变更可见性"
                )
            acl = normalize_acl(
                visibility, doc.created_by or principal.user_id, dept_id, allowed_roles
            )
            doc.visibility = acl["visibility"]
            doc.owner_id = acl["owner_id"]
            doc.dept_id = acl["dept_id"]
            doc.allowed_roles = acl["allowed_roles"]
            doc.updated_at = _utcnow()
            # 块表 ACL 列复用同一事务(不自己开事务、不自己提交)。
            updated_chunks, updated_parents = await store.update_acl_by_doc(
                doc_key, acl["visibility"], acl["owner_id"], acl["dept_id"],
                acl["allowed_roles"], session=session,
            )
            await pipeline.emit_event(
                session, pipeline.TOPIC_ACL, doc_key,
                {"doc_key": doc_key, **acl, "operator": principal.user_id},
            )

    logger.info(
        "document acl updated: doc_key=%s visibility=%s operator=%s",
        doc_key, acl["visibility"], principal.user_id,
    )
    result: dict[str, Any] = {
        "doc_key": doc_key, **acl,
        "chunks_updated": updated_chunks, "parents_updated": updated_parents,
        "projected": True,
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


async def delete_document(doc_key: str, principal: Principal | None = None) -> dict[str, Any]:
    """Delete a document: 一个事务标陈旧 + 删元数据/标签 + outbox 事件, 其余交 worker 投影。

    为什么主链路不真删块行: 一篇大文档可以上万行, 在 DELETE 请求里真删会长时间持锁,
    与它并发的检索会卡在锁上。现在同事务把父子块标 ``deprecated_at``(行级不可见立即
    生效)、删元数据、发 ``doc.deleted`` 事件; ES / Mongo / artifact / 上传目录 / KG 都是
    派生存储, 由 worker 幂等重放。旧写法把这些依次 best-effort 放在请求里: 任何一步
    失败就永久残留, 而且没人会再补。

    闸门: 删除不可恢复, 与改可见性同一口径(所有者本人或 admin)。
    """
    factory = get_session_factory()
    store = ChunkStore()
    async with factory() as session:
        async with session.begin():
            doc = (
                await session.execute(
                    select(Document).where(Document.doc_key == doc_key).with_for_update()
                )
            ).scalar_one_or_none()
            if doc is None:
                raise UploadError("文档不存在或已删除")
            if principal is None or not can_manage_document(principal, doc.created_by or ""):
                raise DocumentForbidden(
                    f"仅文档所有者({doc.created_by or '无主'})或管理员可删除该文档"
                )
            name = doc.name
            upload_rows = (
                await session.execute(
                    select(DocUpload.upload_id).where(DocUpload.doc_key == doc_key)
                )
            ).scalars().all()
            # 父子块标陈旧(与删元数据同事务): 检索侧立刻不可见, 真删交 GC。
            await store.mark_deprecated_by_doc(session, doc_key)
            await session.execute(delete(DocumentTag).where(DocumentTag.doc_key == doc_key))
            await session.execute(delete(Document).where(Document.doc_key == doc_key))
            await pipeline.emit_event(
                session, pipeline.TOPIC_DELETE, doc_key,
                {
                    "doc_key": doc_key, "name": name,
                    "upload_ids": [str(u) for u in upload_rows],
                    "operator": principal.user_id,
                },
            )
            # 上传行也标 expired: 它们的原件与产物由投影统一回收, 不再留在"可入库"集合里。
            if upload_rows:
                await session.execute(
                    update(DocUpload)
                    .where(DocUpload.upload_id.in_([str(u) for u in upload_rows]))
                    .values(status="expired", updated_at=_utcnow())
                )

    logger.info("document deleted: doc_key=%s name=%s (投影由 worker 消费 doc.deleted)", doc_key, name)
    return {"doc_key": doc_key, "name": name, "projected": True}


async def docs_not_ready(doc_ids: Sequence[str]) -> set[str]:
    """返回 status != 'ready' 的 doc_id 集合(发布态门禁, 供检索出口一次性批量判)。

    防"正在入库的首篇文档"被检索到: ``submit_ingest`` 对**新文档**先置 ingesting,
    发布事务才转 ready, 故未就绪文档的块即使已进入新表也不会进入回答。已在架文档
    重入库时不转 ingesting(发布是单事务原子操作, 旧行到新行的交接靠 ``deprecated_at``
    标记同瞬完成), 所以不会出现"重入库期间整篇检不到"的可用性倒退。
    调用方在 assemble_parents 后的出口复核里一次性传入命中 doc_id, 不逐块查。
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


async def list_documents(principal: Principal | None = None) -> list[dict[str, Any]]:
    """Document list with tags for the management page, 按调用者 ACL 裁剪。

    原先这里返回全库文档及其 visibility/dept_id/allowed_roles/created_by: 管理页
    本应只展示"你能看到的那些", 否则工号列表 + 权限结构本身就是一次元数据泄露,
    而且列表里有、回答里永远没有会对不上口径。判定与检索侧共用
    :func:`app.security.acl.is_allowed_fields`, 不会出现两套口径漂移。
    无 principal 时按"未登录员工"处理: 只返回 public 文档(而不是全量)。
    """
    effective = principal or Principal(user_id="")
    factory = get_session_factory()
    async with factory() as session:
        docs = (await session.execute(select(Document).order_by(Document.updated_at.desc()))).scalars().all()
        visible = [
            d for d in docs
            if is_allowed_fields(
                getattr(d, "visibility", "") or "public",
                getattr(d, "owner_id", "") or (d.created_by or ""),
                getattr(d, "dept_id", "") or "",
                getattr(d, "allowed_roles", "") or "",
                effective,
            )
        ]
        keys = [d.doc_key for d in visible]
        meta = await get_meta_map(keys) if keys else {}
    return [
        {
            "doc_key": d.doc_key,
            "name": d.name,
            "ext": d.ext,
            "modality": d.modality,
            "chunk_count": d.chunk_count,
            "size_bytes": d.size_bytes,
            # 入库是异步的: 页面得区分"在途"与"已就绪", 否则刚提交的文档看起来像丢了。
            "status": d.status or "ready",
            "tags": meta.get(d.doc_key, {}).get("tags", []),
            "created_by": d.created_by,
            "visibility": getattr(d, "visibility", "") or "public",
            "dept_id": getattr(d, "dept_id", "") or "",
            "allowed_roles": [r for r in (getattr(d, "allowed_roles", "") or "").strip(",").split(",") if r],
            "updated_at": d.updated_at.isoformat(timespec="seconds") if d.updated_at else "",
        }
        for d in visible
    ]


async def list_tags() -> list[dict[str, Any]]:
    """All tags for the management page."""
    factory = get_session_factory()
    async with factory() as session:
        tags = (await session.execute(select(Tag).order_by(Tag.name))).scalars().all()
    return [{"id": t.id, "name": t.name, "source": t.source} for t in tags]
