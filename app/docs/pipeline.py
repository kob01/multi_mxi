"""入库流水线阶段实现: 网关(同步回退)与 worker(异步主路)共用同一份代码。

为什么单独成模块: 阶段编排既不能被 HTTP 请求线程独占(OCR/embedding 是分钟级), 也不能
只存在于 worker 里(排障时需要在网关内跑一遍)。把阶段做成一组可重放的函数, 两条路径
调的是同一批实现, 不会出现"worker 里的逻辑与请求路径不一致"这类只在生产才复现的分叉。

四条不得违背的约束:

1. **PG 是事实源**: 业务写与 ``outbox_events`` 事件行同事务提交; Redis Streams 只做
   低延迟 handoff, 丢了由 ``db_poll`` 自愈。任何"先发事件再写库"的写法都不许出现。
2. **阶段可重放**: 每个阶段都以"从当前 stage 续跑"为前提写成幂等 —— artifact 已就绪
   就不重解析, content_hash 未变就不重 embed, upsert 本身幂等, ES 按 id 写/删幂等。
3. **先写 Mongo 再写 PG**: PG 可见的行一定能取到正文(既有不变量, 本次不动)。
4. **主链路不物理删旧块**: 发布事务只标 ``deprecated_at``, 真删交后台 GC 分批做。

任务表(``ingest_tasks``)是唯一的认领依据: ``lease_owner/lease_expires_at`` 决定谁能跑,
``attempt`` 决定跑几次算失败, ``stage`` 决定从哪儿续跑。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bodies.store import ParentTextItem, get_body_store
from app.cache.retrieval_cache import bump_doc_gen
from app.config import get_settings
from app.db.models import (
    DocUpload,
    Document,
    DocumentTag,
    IngestTask,
    OutboxEvent,
    Tag,
)
from app.db.session import get_engine, get_session_factory
from app.docs.artifacts import get_artifact_store
from app.docs.normalize import normalize_text
from app.docs.parsers import ParsedBlock, parse_blocks
from app.rag.bm25 import get_es_bm25
from app.rag.embeddings import OllamaEmbedder
from app.rag.ingest import build_parent_child, build_structure
from app.rag.vectorstore import ChunkStore, ParentStore, publish_in_session
from app.schemas import KnowledgeChunk, ParentBlock

logger = logging.getLogger(__name__)

# 状态机: 只向前推进, 失败不回退 stage(回退会把已完成的重活再干一遍)。
STAGE_PENDING = "pending"
STAGE_PARSING = "parsing"
STAGE_EMBEDDING = "embedding"
STAGE_PUBLISHING = "publishing"
STAGE_INDEXING = "indexing"
STAGE_READY = "ready"
STAGE_FAILED = "failed"
# 仍在推进中的阶段集合: 部分唯一索引与回收器都以这一组为判定口径。
ACTIVE_STAGES = (STAGE_PENDING, STAGE_PARSING, STAGE_EMBEDDING, STAGE_PUBLISHING, STAGE_INDEXING)
TERMINAL_STAGES = (STAGE_READY, STAGE_FAILED)

# outbox 主题(worker 侧投影的分派键)。
TOPIC_PARSE = "parse.requested"
TOPIC_INGEST = "ingest.requested"
TOPIC_ACL = "doc.acl_changed"
TOPIC_DELETE = "doc.deleted"

# GC 的会话级咨询锁: 多副本 worker 同时到点时只有一个真去扫陈旧行(其余直接跳过)。
# 固定值即可 —— 本实例上只有这一套文档存储, 不需要按库区分。
_GC_ADVISORY_LOCK_KEY = 2026_1011
# 单轮 GC 最多删多少批: 让一次 GC 有确定的上界, 不至于在超大陈旧集上变成无限循环。
_GC_MAX_ROUNDS = 20


class StageRetryLater(RuntimeError):
    """阶段暂时不能推进(上游 artifact 还在解析中): 让出租约, 下一轮再认领。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_task_id(doc_key: str, upload_id: str, content_sha256: str) -> str:
    """确定性 task_id: 同一篇文件的同一次确认入库永远算出同一个 id。

    这是"重复点确认入库不会排两次队"的根本手段 —— 配合 ON CONFLICT DO NOTHING, 幂等
    不需要额外的去重表或分布式锁。
    """
    material = f"{doc_key}|{upload_id}|{content_sha256}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:32]


@dataclass
class TaskContext:
    """一次任务执行的上下文(从 ``ingest_tasks`` + ``doc_uploads`` 组装)。"""

    task_id: str
    doc_key: str
    upload_id: str
    stage: str
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    lease_owner: str = ""
    upload_sha256: str = ""

    @property
    def acl(self) -> dict[str, str]:
        return self.params.get("acl") or {}

    @property
    def tags(self) -> list[str]:
        return list(self.params.get("tags") or [])

    @property
    def uploader(self) -> str:
        return str(self.params.get("uploader") or "")


@dataclass
class Workset:
    """解析产物 -> 待发布的父子块(阶段中间结果, 只在内存里传)。"""

    parents: list[ParentBlock]
    parent_items: list[ParentTextItem]
    children: list[KnowledgeChunk]
    normalized: str
    structure: list[dict]
    raw: str
    modality: str
    title: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 任务表读写

async def claim_task(task_id: str, owner: str) -> TaskContext | None:
    """按租约认领任务: 只有租约空着或已过期的人才拿得到。

    一条 ``UPDATE ... RETURNING`` 完成"判定 + 抢占"。先 SELECT 判再 UPDATE 的写法在
    两个 worker 同时认领时会都认为自己拿到了 —— 那正是同一篇文档被两条链路同时写块表
    的事故现场。
    """
    lease = get_settings().ingest_task_lease_seconds
    now = _utcnow()
    async with get_session_factory()() as session:
        async with session.begin():
            row = (
                await session.execute(
                    update(IngestTask)
                    .where(
                        IngestTask.task_id == task_id,
                        IngestTask.stage.in_(ACTIVE_STAGES),
                        or_(
                            IngestTask.lease_expires_at.is_(None),
                            IngestTask.lease_expires_at < now,
                        ),
                    )
                    .values(
                        lease_owner=owner[:64],
                        lease_expires_at=now + timedelta(seconds=lease),
                        heartbeat_at=now,
                        attempt=IngestTask.attempt + 1,
                        updated_at=now,
                    )
                    .returning(IngestTask.task_id)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        task = (
            await session.execute(select(IngestTask).where(IngestTask.task_id == task_id))
        ).scalar_one_or_none()
        upload = (
            await session.execute(
                select(DocUpload).where(DocUpload.upload_id == (task.upload_id if task else ""))
            )
        ).scalar_one_or_none() if task else None
    if task is None:
        return None
    return TaskContext(
        task_id=task.task_id,
        doc_key=task.doc_key,
        upload_id=task.upload_id,
        stage=task.stage,
        params=dict(task.params or {}),
        result=dict(task.result_json or {}),
        attempt=int(task.attempt or 0),
        lease_owner=owner[:64],
        upload_sha256=(upload.content_sha256 if upload else task.content_sha256) or "",
    )


async def set_stage(task_id: str, stage: str, owner: str) -> None:
    """推进阶段并续租(只有持租者会调它)。"""
    lease = get_settings().ingest_task_lease_seconds
    now = _utcnow()
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                update(IngestTask)
                .where(IngestTask.task_id == task_id, IngestTask.lease_owner == owner[:64])
                .values(
                    stage=stage,
                    lease_expires_at=now + timedelta(seconds=lease),
                    heartbeat_at=now,
                    updated_at=now,
                )
            )


async def save_task_result(task_id: str, patch: dict[str, Any], owner: str) -> None:
    """把阶段产物(写入块数/被标陈旧的块 id)回写任务行。

    ``indexing`` 阶段要靠这里的 ``marked`` 才能重放: 崩溃重启后如果不记在库里, 就没人
    知道 ES 里该删哪些陈旧块(稀疏通道会一直留着旧词元)。
    """
    now = _utcnow()
    async with get_session_factory()() as session:
        async with session.begin():
            task = (
                await session.execute(
                    select(IngestTask).where(
                        IngestTask.task_id == task_id, IngestTask.lease_owner == owner[:64]
                    )
                )
            ).scalar_one_or_none()
            if task is None:
                return
            merged = dict(task.result_json or {})
            merged.update(patch)
            await session.execute(
                update(IngestTask)
                .where(IngestTask.task_id == task_id)
                .values(result_json=merged, heartbeat_at=now, updated_at=now)
            )


async def finish_task(task_id: str, stage: str, owner: str, error: str = "") -> None:
    """收口到 ready/failed: 释放租约并记完成时间。"""
    now = _utcnow()
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                update(IngestTask)
                .where(IngestTask.task_id == task_id, IngestTask.lease_owner == owner[:64])
                .values(
                    stage=stage,
                    error=error[:4000],
                    lease_owner="",
                    lease_expires_at=None,
                    heartbeat_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )


async def release_lease(task_id: str, owner: str) -> None:
    """让出租约(阶段"稍后重试"或阶段失败时用): 不改 stage, 下一轮会重新认领并从断点续跑。"""
    now = _utcnow()
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                update(IngestTask)
                .where(IngestTask.task_id == task_id, IngestTask.lease_owner == owner[:64])
                .values(lease_owner="", lease_expires_at=None, updated_at=now)
            )


async def fetch_claimable_task_ids(limit: int) -> list[str]:
    """扫出可认领的任务(租约空着或已过期), 供 worker 的 db_poll 自愈循环使用。"""
    now = _utcnow()
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(IngestTask.task_id)
                .where(
                    IngestTask.stage.in_(ACTIVE_STAGES),
                    or_(
                        IngestTask.lease_expires_at.is_(None),
                        IngestTask.lease_expires_at < now,
                    ),
                )
                .order_by(IngestTask.created_at)
                .limit(limit)
            )
        ).scalars().all()
    return list(rows)


async def fetch_pending_upload_ids(limit: int) -> list[str]:
    """扫出还没人解析的上传行(``pending``)。

    与 :func:`fetch_claimable_task_ids` 同一道理: 事件投递失败(Redis 当时不通)不该
    让一篇已上传的文档永远停在"解析中"。解析任务本身可重放, 补扫不花额外成本。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(DocUpload.upload_id)
                .where(DocUpload.status == "pending")
                .order_by(DocUpload.created_at)
                .limit(limit)
            )
        ).scalars().all()
    return list(rows)


async def note_task_error(task_id: str, error: str) -> None:
    """把阶段失败原因写进任务行(不改 stage: 原因与断点是两件事)。"""
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                update(IngestTask)
                .where(IngestTask.task_id == task_id)
                .values(error=error[:4000], updated_at=_utcnow())
            )


# ---------------------------------------------------------------- 解析任务(upload)

async def set_upload_state(
    upload_id: str, status: str, *, modality: str = "", artifact_id: str = "", error: str = ""
) -> None:
    """回写上传行状态(解析成功/失败); 失败原因必须落库, 否则页面上只能一直显示"解析中"。"""
    now = _utcnow()
    vals: dict[str, Any] = {"status": status, "updated_at": now}
    if modality:
        vals["modality"] = modality[:32]
    if artifact_id:
        vals["artifact_id"] = artifact_id[:64]
    if error:
        vals["error"] = error[:4000]
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                update(DocUpload).where(DocUpload.upload_id == upload_id).values(**vals)
            )


async def claim_upload(upload_id: str) -> dict[str, Any] | None:
    """把 ``pending`` 的上传行占为 ``parsing``: status 本身就是解析任务的租约。

    返回 None 表示这轮不该我跑(被别人占用 / 已解析完 / 已终态)。解析的时长上界由
    :func:`reap_stalled_uploads` 按 ``updated_at`` 判定, 与任务表同一套口径。
    """
    now = _utcnow()
    async with get_session_factory()() as session:
        async with session.begin():
            row = (
                await session.execute(
                    update(DocUpload)
                    .where(DocUpload.upload_id == upload_id, DocUpload.status == "pending")
                    .values(status="parsing", attempt=DocUpload.attempt + 1, updated_at=now)
                    .returning(DocUpload.upload_id)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        up = (
            await session.execute(select(DocUpload).where(DocUpload.upload_id == upload_id))
        ).scalar_one_or_none()
    if up is None:
        return None
    return {
        "upload_id": up.upload_id,
        "doc_key": up.doc_key,
        "filename": up.filename,
        "ext": up.ext,
        "content_sha256": up.content_sha256,
        "stored_rel_path": up.stored_rel_path,
        "uploaded_by": up.uploaded_by,
        "attempt": int(up.attempt or 0),
    }


async def run_parse_job(upload_id: str) -> str:
    """解析一篇上传件 -> 固化 artifact + LLM 标签。

    返回 ``parsed`` / ``inprogress`` / ``skipped`` / ``failed``。OCR、大文件解析、
    LLM tags 都发生在这里, 不在 HTTP 请求里 —— 这是 P0"重活不进同步链路"的落点。
    """
    artifacts = get_artifact_store()
    async with get_session_factory()() as session:
        up = (
            await session.execute(select(DocUpload).where(DocUpload.upload_id == upload_id))
        ).scalar_one_or_none()
    if up is None:
        logger.warning("parse job: upload 行不存在 upload_id=%s", upload_id)
        return "skipped"
    if up.status == "parsed":
        return "parsed"
    if up.status in ("expired", "failed"):
        return "skipped"
    if up.status == "parsing":
        # 已有 worker 占着(或本轮重入): 什么都不做, 由调用方决定等待还是跳过。
        return "inprogress"
    claimed = await claim_upload(upload_id)
    if claimed is None:
        return "inprogress"
    try:
        from app.docs.service import resolve_upload_path, suggest_tags

        path = resolve_upload_path(claimed["stored_rel_path"])
        if not path.exists():
            raise FileNotFoundError(f"上传文件不存在: {claimed['stored_rel_path']}")
        modality, blocks = await parse_blocks(path)
        if not blocks or not "".join(b.text for b in blocks).strip():
            raise ValueError("文档解析后无有效内容")
        doc = await artifacts.save(
            upload_id,
            doc_key=claimed["doc_key"],
            content_sha256=claimed["content_sha256"],
            parser_version=get_settings().parser_version,
            modality=modality,
            filename=claimed["filename"],
            blocks=blocks,
        )
        # 标签在产物落地之后补: LLM 失败不该让整次解析失败(预览已经可用了)。
        try:
            tags = await suggest_tags(doc.get("head") or doc.get("preview") or "")
            if tags:
                await artifacts.update_tags(upload_id, tags)
        except Exception as exc:  # noqa: BLE001 - 标签是增值项, 失败只留痕
            logger.warning("artifact 标签生成失败 upload_id=%s: %s", upload_id, exc)
        await set_upload_state(upload_id, "parsed", modality=modality, artifact_id=upload_id)
        logger.info(
            "parse job done upload_id=%s doc_key=%s blocks=%s",
            upload_id, claimed["doc_key"], doc.get("block_count"),
        )
        return "parsed"
    except Exception as exc:  # noqa: BLE001 - 解析失败要落库并让页面看得到原因
        logger.error("parse job failed upload_id=%s: %s", upload_id, exc)
        await set_upload_state(upload_id, "failed", error=f"{exc.__class__.__name__}: {exc}")
        return "failed"


async def ensure_artifact(ctx: TaskContext) -> None:
    """入库前置: 让上传件有一份可复用的解析产物(必要时现在解析, 但绝不重复解析)。

    产物按 ``content_sha256 + parser_version`` 判定能否复用 —— 字节没变、解析器没变就
    直接用预览那一份, 这就是"预览与入库共用同一产物"的落点。
    """
    reusable = await get_artifact_store().is_reusable(
        ctx.upload_id, ctx.upload_sha256, get_settings().parser_version
    )
    if reusable:
        return
    state = await run_parse_job(ctx.upload_id)
    if state == "parsed":
        # 解析完还要确认产物确实可复用: 字节与当前 parser_version 不符(比如刚改过
        # 解析器)就必须重跑, 否则"改了解析代码不生效"会静默延续。
        if await get_artifact_store().is_reusable(
            ctx.upload_id, ctx.upload_sha256, get_settings().parser_version
        ):
            return
        raise RuntimeError(
            f"解析产物与当前口径不符且重解析后仍不可用: upload_id={ctx.upload_id}"
        )
    if state == "inprogress":
        raise StageRetryLater(f"upload {ctx.upload_id} 正在解析中, 稍后续跑")
    raise RuntimeError(f"解析产物不可用: upload_id={ctx.upload_id} state={state}")


# ---------------------------------------------------------------- 阶段: embedding + publishing

def _compute_chunks(
    doc_key: str,
    title: str,
    source: str,
    modality: str,
    blocks: list[ParsedBlock],
    raw: str,
    acl: dict[str, str],
) -> tuple[str, list[dict], list[ParentBlock], list[ParentTextItem], list[KnowledgeChunk], list[str]]:
    """归一化 + 结构树 + 父子块切分(一次线程调用里算完, 免得归一化结果再穿一次线)。"""
    normalized = normalize_text(raw)
    structure = build_structure(blocks, normalized)
    parents, parent_items, children, warnings = build_parent_child(
        doc_key, title, source, modality, blocks, normalized, acl=acl
    )
    return normalized, structure, parents, parent_items, children, warnings


async def build_workset(ctx: TaskContext) -> Workset:
    """读 artifact -> 归一化/结构/父子块(纯 CPU 部分下沉线程池, 不占事件循环)。"""
    modality, blocks, raw = await get_artifact_store().load_blocks(ctx.upload_id)
    if not blocks or not raw.strip():
        raise RuntimeError("解析产物无有效内容块")
    name = str(ctx.params.get("filename") or "")
    # source 存**相对 upload_dir 的路径**: 绝对路径是容器内 /data/uploads/..., 宿主侧
    # 读到它就指向不存在的位置(历史上已经踩过一次"删不掉源文件"的坑)。
    source = str(ctx.params.get("stored_rel_path") or name)
    title = Path(name).stem if name else ctx.doc_key
    normalized, structure, parents, parent_items, children, warnings = await asyncio.to_thread(
        _compute_chunks, ctx.doc_key, title, source, modality, blocks, raw, ctx.acl
    )
    return Workset(
        parents=parents, parent_items=parent_items, children=children,
        normalized=normalized, structure=structure, raw=raw, modality=modality,
        title=title, warnings=list(warnings),
    )


async def embed_children(ctx: TaskContext, work: Workset) -> dict[str, list[float]]:
    """只对 content_hash 变化的子块 embed; 未变化块从 PG 原样回填向量。"""
    store = ChunkStore()
    children = work.children
    if not children:
        return {}
    old_hashes = await store.existing_hashes(ctx.doc_key)
    if get_settings().rag_incremental_ingest:
        changed = [c for c in children if old_hashes.get(c.chunk_id) != c.content_hash]
    else:
        changed = list(children)
    embedder = OllamaEmbedder()
    vectors: dict[str, list[float]] = {}
    if changed:
        new_vecs = await embedder.embed([c.content for c in changed])
        vectors.update({c.chunk_id: v for c, v in zip(changed, new_vecs)})
    changed_ids = {c.chunk_id for c in changed}
    unchanged = [c.chunk_id for c in children if c.chunk_id not in changed_ids]
    if unchanged:
        vectors.update(await store.get_embeddings(unchanged))
    # 缺向量的块(新出现或旧向量丢失)兜底 embed, 避免发布出"有元数据无向量"的行。
    need = [c for c in children if c.chunk_id not in vectors]
    if need:
        nv = await embedder.embed([c.content for c in need])
        vectors.update({c.chunk_id: v for c, v in zip(need, nv)})
        logger.info(
            "incremental embed: %d changed + %d backfill / %d children doc_key=%s",
            len(changed), len(need), len(children), ctx.doc_key,
        )
    return vectors


async def embed_and_publish(ctx: TaskContext) -> dict[str, Any]:
    """一次完成 embedding + publishing 两个阶段(可重放: 重跑的代价只有一次 hash 差分)。

    从 ``embedding`` 或 ``publishing`` 任一阶段续跑都走这里: 发布是 upsert + 标记,
    重放不会产生第二份块; embed 会命中刚写入的行哈希, 退化成"原样回填向量"。

    进来先做一次 ``ensure_artifact``: 阶段入口不能假设上一阶段的产物还在。崩溃重领
    可能落在 ``embedding`` 而解析任务当时并未跑完(或产物已过期被回收), 不补这一步
    就会报"解析产物不存在"并把一个本可救的任务反复重试到判失败。
    """
    await set_stage(ctx.task_id, STAGE_EMBEDDING, ctx.lease_owner)
    await ensure_artifact(ctx)
    work = await build_workset(ctx)
    vectors = await embed_children(ctx, work)
    if not work.children:
        raise RuntimeError("切块结果为空, 无法入库")

    await set_stage(ctx.task_id, STAGE_PUBLISHING, ctx.lease_owner)
    # expand: 整篇正文与父块全文先落 Mongo(不删旧), 保证 PG 可见的行一定取到正文。
    bodies = get_body_store()
    await bodies.save_doc_body(
        ctx.doc_key, raw=work.raw, normalized=work.normalized, structure=work.structure,
        meta={"name": work.title, "modality": work.modality, "source": work.children[0].source},
    )
    await bodies.save_parent_texts(work.parent_items)

    written, marked = await _publish_transaction(ctx, work, vectors)
    result = {"written": written, "marked": marked, "warnings": work.warnings}
    await save_task_result(ctx.task_id, result, ctx.lease_owner)
    return result


async def _publish_transaction(
    ctx: TaskContext, work: Workset, vectors: dict[str, list[float]]
) -> tuple[int, list[str]]:
    """PG 单事务: 元数据(转 ready) + tags + 父子块发布(标陈旧) + outbox 事件。

    一个事务是重点。旧链路分三个事务(块发布 / 元数据+tags / ES+缓存刷新), 任一步失败
    都留下悬挂状态: "有向量无元数据"(检索门禁把它当孤儿向量默认拒, 页面却显示成功)或
    "元数据说私有、块表还是 public"(越权)。
    """
    factory = get_session_factory()
    now = _utcnow()
    rel = str(ctx.params.get("stored_rel_path") or "")
    acl = ctx.acl
    async with factory() as session:
        async with session.begin():
            doc = (
                await session.execute(
                    select(Document).where(Document.doc_key == ctx.doc_key).with_for_update()
                )
            ).scalar_one_or_none()
            new_document = doc is None
            if doc is None:
                filename = str(ctx.params.get("filename") or "")
                doc = Document(
                    doc_key=ctx.doc_key, name=work.title,
                    ext=Path(filename).suffix.lower()[:16],
                    modality=work.modality, created_by=ctx.uploader,
                )
                session.add(doc)
                await session.flush()
            ordered = [vectors.get(c.chunk_id) or [] for c in work.children]
            written, marked = await publish_in_session(
                session, ctx.doc_key, work.parents, work.children, ordered
            )
            doc.file_path = rel
            if ctx.params.get("size_bytes"):
                doc.size_bytes = int(ctx.params["size_bytes"])
            doc.modality = work.modality
            doc.name = work.title
            doc.parsed_text = work.raw
            doc.chunk_count = written
            doc.body_stored = True
            doc.status = "ready"
            doc.visibility = acl.get("visibility", "public")
            doc.owner_id = acl.get("owner_id", "")
            doc.dept_id = acl.get("dept_id", "")
            doc.allowed_roles = acl.get("allowed_roles", "")
            doc.updated_at = now
            await _rewrite_tags(session, ctx.doc_key, ctx.tags)
            await _insert_outbox(
                session, TOPIC_INGEST, ctx.doc_key,
                {
                    "task_id": ctx.task_id, "doc_key": ctx.doc_key,
                    "upload_id": ctx.upload_id, "published": True, "new_document": new_document,
                },
            )
    return written, marked


async def _rewrite_tags(session: AsyncSession, doc_key: str, tags: list[str]) -> None:
    await session.execute(delete(DocumentTag).where(DocumentTag.doc_key == doc_key))
    for tag_name in dict.fromkeys(t.strip() for t in tags if t.strip()):
        tag_id = await _get_or_create_tag(session, tag_name)
        session.add(DocumentTag(doc_key=doc_key, tag_id=tag_id))


async def _get_or_create_tag(session: AsyncSession, name: str) -> int:
    """标签表并发插入靠"再查一次"收敛(同名标签只应有一行)。"""
    result = await session.execute(select(Tag).where(Tag.name == name))
    tag = result.scalar_one_or_none()
    if tag is None:
        tag = Tag(name=name, source="custom")
        session.add(tag)
        await session.flush()
    return int(tag.id)


# ---------------------------------------------------------------- 阶段: indexing(投影)

async def index_doc(ctx: TaskContext, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """ES 单文档增量写 + 缓存定向失效 + 图谱构建。

    这里刻意**不**做全库 BM25 重建: 一篇文档只写它自己的现行子块、只删发布阶段标掉的
    那批块。全量重建降级为运维手段(``scripts/rebuild_bm25.py``) —— 否则文档一多,
    入库一篇就把全库重写一遍, 成本随语料量线性爆炸。
    """
    await set_stage(ctx.task_id, STAGE_INDEXING, ctx.lease_owner)
    payload = dict(result if result is not None else (ctx.result or {}))
    marked = [str(x) for x in (payload.get("marked") or [])]
    store = ChunkStore()
    bm25 = get_es_bm25()
    chunks = await store.list_doc_chunks(ctx.doc_key)
    try:
        if chunks:
            await bm25.index_doc_chunks(ctx.doc_key, chunks)
        if marked:
            await bm25.delete_chunk_ids(marked)
    except Exception as exc:  # noqa: BLE001 - 派生索引失败不该判整次入库失败
        logger.warning("ES 增量投影失败 doc_key=%s(可由单文档重建/运维脚本收敛): %s", ctx.doc_key, exc)
        payload.setdefault("warnings", []).append(f"ES 投影失败: {str(exc)[:160]}")
    await bump_doc_gen([ctx.doc_key])
    warnings = list(payload.get("warnings") or [])
    if get_settings().doc_kg_enabled:
        try:
            from app.kg.service import build_for_doc

            await build_for_doc(ctx.doc_key)
        except Exception as exc:  # noqa: BLE001 - 建图失败不影响检索可用性
            logger.warning("KG 构建失败 doc_key=%s: %s", ctx.doc_key, exc)
            warnings.append(f"KG 构建失败: {str(exc)[:160]}")
    payload["warnings"] = warnings
    payload["indexed"] = len(chunks)
    await save_task_result(ctx.task_id, payload, ctx.lease_owner)
    return payload


# ---------------------------------------------------------------- 任务驱动入口

async def advance_task(task_id: str, owner: str) -> str:
    """把任务从当前阶段推到 ready(或让出租约等下一轮)。

    网关的同步回退与 worker 的异步主路都调这一个函数, 因此"两条路径行为不一致"这类
    问题在结构上就不可能出现。
    """
    ctx = await claim_task(task_id, owner)
    if ctx is None:
        return "skipped"
    if ctx.stage in TERMINAL_STAGES:
        return ctx.stage
    try:
        if ctx.stage in (STAGE_PENDING, STAGE_PARSING):
            await set_stage(task_id, STAGE_PARSING, owner)
            await ensure_artifact(ctx)
            result = await embed_and_publish(ctx)
        elif ctx.stage in (STAGE_EMBEDDING, STAGE_PUBLISHING):
            result = await embed_and_publish(ctx)
        else:  # STAGE_INDEXING: 发布已成功, 只补投影
            result = dict(ctx.result or {})
        await index_doc(ctx, result)
    except StageRetryLater as exc:
        logger.info("任务等待上游产物: task_id=%s %s", task_id, exc)
        await release_lease(task_id, owner)
        return "waiting"
    except Exception as exc:  # noqa: BLE001 - 阶段失败: 保留 stage 供续跑, 释放租约
        logger.error("入库任务失败 task_id=%s stage=%s: %s", task_id, ctx.stage, exc)
        await note_task_error(task_id, f"{exc.__class__.__name__}: {str(exc)[:500]}")
        await release_lease(task_id, owner)
        return "failed_stage"
    await finish_task(task_id, STAGE_READY, owner)
    logger.info("入库完成 task_id=%s doc_key=%s blocks=%s", task_id, ctx.doc_key, result.get("written"))
    return STAGE_READY


def _worker_name() -> str:
    """租约持有者标识: 主机名 + pid, 同一台机器多副本也能区分开。"""
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}"[:64]


# ---------------------------------------------------------------- outbox

async def _insert_outbox(
    session: AsyncSession, topic: str, doc_key: str, payload: dict[str, Any]
) -> None:
    """在调用方事务里插一条事件行(与业务写同提交, 因此不会"错投/漏投")。"""
    await session.execute(
        pg_insert(OutboxEvent).values(
            topic=topic, doc_key=(doc_key or "")[:32], payload=payload, status="new",
            created_at=_utcnow(),
        )
    )


async def emit_event(session: AsyncSession, topic: str, doc_key: str, payload: dict[str, Any]) -> None:
    """供 service 层在自身事务里调用的公开入口(必须与业务写同一事务, 故收 session)。"""
    await _insert_outbox(session, topic, doc_key, payload)


async def fetch_unsent_events(limit: int) -> list[dict[str, Any]]:
    """取到期待投的事件(含退避后可重试的): 只读, 不改状态。"""
    now = _utcnow()
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "new",
                    or_(OutboxEvent.next_retry_at.is_(None), OutboxEvent.next_retry_at <= now),
                )
                .order_by(OutboxEvent.event_id)
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "event_id": int(r.event_id), "topic": r.topic, "doc_key": r.doc_key,
                "payload": dict(r.payload or {}), "attempts": int(r.attempts or 0),
            }
            for r in rows
        ]


async def mark_event_sent(event_id: int) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.event_id == event_id)
                .values(status="sent", sent_at=_utcnow(), error="")
            )


async def mark_event_retry(event_id: int, attempts: int, error: str) -> None:
    """投递失败: 指数退避(上限 10 分钟), 超过 ``outbox_max_attempts`` 置 failed。

    即使这里判 failed, 正确性也不依赖事件 —— 任务表才是认领依据, db_poll 会兜住;
    事件只影响"多快被跑起来", 不影响"最终会不会被跑起来"。
    """
    settings = get_settings()
    now = _utcnow()
    if attempts >= settings.outbox_max_attempts:
        vals: dict[str, Any] = {"status": "failed", "attempts": attempts, "error": error[:2000]}
    else:
        delay = min(600, 2 ** min(max(attempts, 1), 10))
        vals = {
            "attempts": attempts,
            "next_retry_at": now + timedelta(seconds=delay),
            "error": error[:2000],
        }
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                update(OutboxEvent).where(OutboxEvent.event_id == event_id).values(**vals)
            )


# ---------------------------------------------------------------- 投影(ACL / 删除)

async def project_acl_change(payload: dict[str, Any]) -> None:
    """权限变更投影: ES 该文重索引 + 缓存定向失效。

    PG 侧的块 ACL 列已由请求事务改完(那是事实源, 检索的授权判定看它); 这里只做派生
    存储的收敛 —— 顺序不能反, 否则 ES 会短暂按旧权限召回。
    """
    doc_key = str(payload.get("doc_key") or "")
    if not doc_key:
        return
    store = ChunkStore()
    bm25 = get_es_bm25()
    chunks = await store.list_doc_chunks(doc_key)
    try:
        if chunks:
            await bm25.index_doc_chunks(doc_key, chunks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ACL ES 投影失败 doc_key=%s: %s", doc_key, exc)
    await bump_doc_gen([doc_key])


async def project_doc_delete(payload: dict[str, Any]) -> None:
    """文档删除投影: ES + Mongo 正文/父块 + artifact + 上传目录 + KG 子图。

    PG 的行在请求事务里已标陈旧并删掉元数据行, 检索立刻不可见; 这里每一步都幂等且可
    重试(重放最多是"再删一次已经不存在的东西")。旧写法把这些放在 DELETE 请求里顺序
    best-effort: 任何一步失败就永久残留, 没人再补。
    """
    doc_key = str(payload.get("doc_key") or "")
    if not doc_key:
        return
    bm25 = get_es_bm25()
    try:
        await bm25.delete_by_doc(doc_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除投影 ES 失败 doc_key=%s: %s", doc_key, exc)
    bodies = get_body_store()
    try:
        await bodies.delete_parents_by_doc(doc_key)
        await bodies.delete_doc_body(doc_key)
    except Exception as exc:  # noqa: BLE001 - 残留由孤儿回收 GC 兜住
        logger.warning("删除投影 Mongo 失败 doc_key=%s: %s", doc_key, exc)
    artifacts = get_artifact_store()
    for uid in payload.get("upload_ids") or []:
        try:
            await artifacts.delete(str(uid))
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除投影 artifact 失败 upload_id=%s: %s", uid, exc)
    from app.docs.service import delete_upload_dir

    await delete_upload_dir(doc_key)
    if get_settings().doc_kg_enabled:
        try:
            from app.kg import store as kg_store

            await kg_store.delete_document_graph(doc_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除投影 KG 失败 doc_key=%s: %s", doc_key, exc)
    await bump_doc_gen([doc_key])


async def dispatch_event(topic: str, payload: dict[str, Any], owner: str | None = None) -> str:
    """worker 消费到 stream 消息后的分派(事件只是触发器, 真实状态在库里)。"""
    worker = owner or _worker_name()
    if topic == TOPIC_PARSE:
        return await run_parse_job(str(payload.get("upload_id") or ""))
    if topic == TOPIC_INGEST:
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            return "skipped"
        return await advance_task(task_id, worker)
    if topic == TOPIC_ACL:
        await project_acl_change(payload)
        return "done"
    if topic == TOPIC_DELETE:
        await project_doc_delete(payload)
        return "done"
    logger.warning("未知 outbox 主题, 直接标记已投: %s", topic)
    return "skipped"


# ---------------------------------------------------------------- 调度器: 回收 / GC / 清理

async def reap_stalled_tasks() -> dict[str, int]:
    """把跑不完的任务收口: 超过 ``max_attempts`` 的判 failed, 其余只清租约等重认领。

    ``stage`` 不回退到 pending —— 它就是断点, 回退会让已完成的 OCR/embed 再干一遍。
    没有这一步, "worker 挂在 publishing 中间"就表现为文档永远停在 ingesting 的悬挂态。
    """
    now = _utcnow()
    reclaimed = failed = 0
    async with get_session_factory()() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(IngestTask).where(
                        IngestTask.stage.in_(ACTIVE_STAGES),
                        IngestTask.lease_expires_at.isnot(None),
                        IngestTask.lease_expires_at < now,
                    )
                )
            ).scalars().all()
            for task in rows:
                last_stage = task.stage
                if int(task.attempt or 0) >= int(task.max_attempts or 3):
                    task.stage = STAGE_FAILED
                    if not task.error:
                        task.error = f"超过最大重试次数({task.attempt}), 卡在阶段 {last_stage}"
                    task.lease_owner = ""
                    task.lease_expires_at = None
                    task.finished_at = now
                    task.updated_at = now
                    failed += 1
                    await _mark_doc_failed_if_never_ready(session, task.doc_key, now)
                else:
                    task.lease_owner = ""
                    task.lease_expires_at = None
                    task.updated_at = now
                    reclaimed += 1
    reaped_parse = await reap_stalled_uploads()
    if reclaimed or failed:
        logger.info("任务回收: 重认领 %d, 判失败 %d, 解析回退 %d", reclaimed, failed, reaped_parse)
    return {"reclaimed": reclaimed, "failed": failed, "uploads": reaped_parse}


async def _mark_doc_failed_if_never_ready(session: AsyncSession, doc_key: str, now: datetime) -> None:
    """终态失败的新文档置 failed; 已在架文档保持原状态(重入库失败不该让它整篇检不到)。"""
    await session.execute(
        update(Document)
        .where(Document.doc_key == doc_key, Document.status == "ingesting")
        .values(status="failed", updated_at=now)
    )


async def reap_stalled_uploads() -> int:
    """把停在 ``parsing`` 且超时的上传行退回 ``pending``(超过重试上限则判 failed)。"""
    lease = get_settings().ingest_task_lease_seconds
    cutoff = _utcnow() - timedelta(seconds=lease)
    moved = 0
    async with get_session_factory()() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(DocUpload).where(
                        DocUpload.status == "parsing", DocUpload.updated_at < cutoff
                    )
                )
            ).scalars().all()
            for up in rows:
                if int(up.attempt or 0) >= get_settings().ingest_task_max_attempts:
                    up.status = "failed"
                    up.error = up.error or f"解析超过最大重试次数({up.attempt})"
                else:
                    up.status = "pending"
                up.updated_at = _utcnow()
                moved += 1
    return moved


async def gc_deprecated_rows() -> dict[str, int]:
    """分批物理删除过了宽限期的陈旧行, 顺带回收 Mongo 里不再被引用的正文。

    advisory lock 保证多副本 worker 只有一份真在删: 两个进程同时分批删会互相把对方的
    事务拖长(锁竞争), 反而更慢。
    """
    settings = get_settings()
    before = _utcnow() - timedelta(minutes=settings.ingest_stale_grace_minutes)
    engine = get_engine()
    async with engine.connect() as conn:
        locked = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _GC_ADVISORY_LOCK_KEY})
        ).scalar_one()
        if not locked:
            logger.debug("GC: 其他 worker 正在回收, 本轮跳过")
            return {"deleted": 0, "skipped": 1}
        try:
            store = ChunkStore()
            chunks = parents = rounds = 0
            for _ in range(_GC_MAX_ROUNDS):
                c, p = await store.delete_deprecated(before, settings.ingest_gc_batch_rows)
                chunks += c
                parents += p
                rounds += 1
                if not c and not p:
                    break
                if not await store.has_deprecated(before):
                    break
            mongo_removed = await prune_stale_bodies()
            if chunks or parents:
                logger.info(
                    "陈旧块 GC: 子块 %d / 父块 %d (%d 轮), Mongo 回收 %d",
                    chunks, parents, rounds, mongo_removed,
                )
            return {
                "deleted": chunks + parents, "chunks": chunks, "parents": parents,
                "mongo": mongo_removed, "rounds": rounds,
            }
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _GC_ADVISORY_LOCK_KEY})


async def prune_stale_bodies() -> int:
    """回收 PG 已不再持有(物理删除)的 Mongo 正文与过期解析产物。

    "还应存在正文"的基准是 ``ParentStore.list_ids(include_deprecated=True)`` +
    ``documents.doc_key``: 已标陈旧但还在宽限期内的行**也算还存在** —— 同 parent_id 的
    upsert 会把 ``deprecated_at`` 重置回现行, 正文提前删掉就等于把一篇重入库文档的
    父块上下文永久弄丢。因此正文只在 PG 行被真删后的那一轮 GC 里才清。
    """
    live_parents = await ParentStore().list_ids(include_deprecated=True)
    async with get_session_factory()() as session:
        live_docs = set((await session.execute(select(Document.doc_key))).scalars().all())
    if not live_docs and not live_parents:
        # 空库: 大概率是读失败而不是"真的什么都没有", 不做全清以免误删全量正文。
        return 0
    removed = await get_body_store().prune_orphans(live_docs, live_parents)
    async with get_session_factory()() as session:
        known_uploads = set((await session.execute(select(DocUpload.upload_id))).scalars().all())
    artifacts_removed = await _prune_orphan_artifacts(known_uploads)
    return int(sum(max(0, v) for v in removed.values())) + artifacts_removed


async def _prune_orphan_artifacts(known_upload_ids: set[str]) -> int:
    """删掉"上传行已不在库里"的解析产物(预览过但没确认入库、或行被手动删过)。

    只碰 created_at 已过一个保留窗口的产物: 新写的产物可能正被解析/入库任务用着,
    按时间卡一道比"猜它是不是孤儿"可靠。
    """
    cutoff = _utcnow() - timedelta(hours=get_settings().upload_retention_hours)
    artifacts = get_artifact_store()
    removed = 0
    for uid in await artifacts.list_orphans(known_upload_ids, older_than=cutoff):
        await artifacts.delete(uid)
        removed += 1
    return removed


async def cleanup_expired_uploads() -> dict[str, int]:
    """删过期上传临时文件 + artifact, 并把上传行置 expired。

    已入库的文档不依赖原件(正文已进 Mongo), 保留窗口只为"确认入库前反悔"与排障;
    不设期限就会让 ``data/uploads`` 单调增长 —— 它在共享卷上, 涨满会连累整个栈。
    """
    settings = get_settings()
    cutoff = _utcnow() - timedelta(hours=settings.upload_retention_hours)
    artifacts = get_artifact_store()
    from app.docs.service import resolve_upload_path

    rows: list[tuple[str, str]] = []
    async with get_session_factory()() as session:
        found = (
            await session.execute(
                select(DocUpload.upload_id, DocUpload.stored_rel_path).where(
                    DocUpload.status.in_(("pending", "parsing", "parsed", "failed")),
                    DocUpload.expires_at < cutoff,
                )
            )
        ).all()
        rows = [(r.upload_id, r.stored_rel_path or "") for r in found]
    removed = 0
    for uid, rel in rows:
        try:
            await artifacts.delete(uid)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("过期 artifact 回收失败 upload_id=%s: %s", uid, exc)
        if rel:
            try:
                path = resolve_upload_path(rel)
                if path.is_file():
                    path.unlink(missing_ok=True)
                    parent = path.parent
                    # 目录空了才删: 同名文档的其他上传件可能还在里面。
                    if parent.exists() and not any(parent.iterdir()):
                        shutil.rmtree(parent, ignore_errors=True)
            except OSError as exc:
                logger.warning("过期上传文件删除失败 %s: %s", rel, exc)
    if rows:
        now = _utcnow()
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    update(DocUpload)
                    .where(DocUpload.upload_id.in_([uid for uid, _ in rows]))
                    .values(status="expired", updated_at=now)
                )
        logger.info("上传过期清理: expired=%d artifacts=%d", len(rows), removed)
    return {"expired": len(rows), "artifacts": removed}


async def run_scheduler_once() -> dict[str, Any]:
    """运维手动跑一轮全套回收(``--once-gc``): 回收 + GC + 过期清理。"""
    return {
        "reap": await reap_stalled_tasks(),
        "gc": await gc_deprecated_rows(),
        "uploads": await cleanup_expired_uploads(),
    }
