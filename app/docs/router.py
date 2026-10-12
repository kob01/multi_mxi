"""Document management API: upload(异步解析) -> 预览轮询 -> ingest(异步入库) -> 任务轮询。

权限口径(与 /api/memory 一致): 本系统没有 token, 身份由请求里的
``uploader``/``operator`` + ``role`` + ``department`` 声明, 因此服务层只能做两件事:
改可见性/删除限"所有者本人或 admin", 列表按调用者的文档 ACL 裁剪。它挡住的是
误操作与伪造角色越权改写, 不是伪造工号本身(那需要真正的认证)。

为什么两个写接口都是 202 + 轮询: OCR/解析/LLM tags/embedding 都是分钟级重活, 把它们
卡在 HTTP 请求里会同时出现三个问题: 网关进程被钉住、请求超时后重试会重复入库、
并发几篇就把下游(OCR/Ollama)压到雪崩。现在提交只建任务, 进度的唯一事实源是任务表。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.docs import service
from app.schemas import Role
from app.security.acl import Principal
from app.security.audit import get_audit_logger, new_trace_id

router = APIRouter(prefix="/api/docs", tags=["docs"])


def _principal(user_id: str, role: str = "employee", department: str = "") -> Principal:
    """把客户端自报的身份三元组成 :class:`Principal`(非法角色回落到 employee)。"""
    try:
        role_enum = Role((role or "employee").strip().lower())
    except ValueError:
        role_enum = Role.EMPLOYEE
    return Principal(user_id=(user_id or "").strip(), role=role_enum, department=(department or "").strip())


def _require_identity(operator: str) -> str:
    """管理类动作必须带操作者工号(缺它就无法判定你是不是所有者)。"""
    user_id = (operator or "").strip()
    if not user_id or user_id == "anonymous":
        raise HTTPException(status_code=400, detail="operator 必填且不能为 anonymous")
    return user_id


class IngestRequest(BaseModel):
    """Phase-2 confirmation payload from the upload page.

    ``upload_id`` 取代旧的 ``(doc_key, filename)`` 二元组: 入库要绑到具体的那一次上传与其
    解析产物, 而不是"同名文件的最新一份" —— 后者会让预览与入库读到不同产物。
    """

    upload_id: str
    doc_key: str = ""            # 兼容旧前端: 传了就与上传行校验一致
    tags: list[str] = Field(default_factory=list)
    uploader: str = "anonymous"
    # 文档级权限: public/dept/role/private (dept 需 dept_id, role 需 allowed_roles)
    visibility: str = "public"
    dept_id: str = ""
    allowed_roles: list[str] = Field(default_factory=list)


class AclRequest(BaseModel):
    """Document visibility change payload (owner/admin operation)."""

    visibility: str
    dept_id: str = ""
    allowed_roles: list[str] = Field(default_factory=list)
    operator: str = ""
    role: str = "employee"
    department: str = ""


@router.post("/upload", status_code=202)
async def upload_doc(file: UploadFile = File(...), uploader: str = Form("anonymous")) -> dict:
    """Phase 1(异步): 流式落盘 + 建上传行 + 同事务发解析事件, 立即 202。

    预览不再在本请求里生成: 前端拿 ``poll`` 地址轮询 :func:`get_upload`。
    上传走流式落盘(:func:`service.stage_upload`), 不落内存(旧写法一次读完整个文件,
    多人同时传几十 MB 就是几 GB 峰值内存)。
    """
    trace_id = new_trace_id()
    try:
        result = await service.submit_upload(file.filename or "unnamed", file, uploader)
    except service.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_audit_logger().log(
        trace_id, "docs", "upload_received",
        {"doc_key": result["doc_key"], "upload_id": result["upload_id"],
         "filename": result["filename"], "ext": result["ext"],
         "reused": result.get("reused", False), "overwritten": result["overwritten"],
         "uploader": uploader},
    )
    return result


@router.get("/uploads/{upload_id}")
async def get_upload(
    upload_id: str,
    operator: str = "", user_id: str = "",
    role: str = "employee", department: str = "",
) -> dict:
    """解析状态与预览(轮询入口): 预览内容直接读解析产物, 不重新解析文件。"""
    principal = _principal(operator or user_id, role, department)
    try:
        return await service.get_upload_state(upload_id, principal)
    except service.UploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    operator: str = "", user_id: str = "",
    role: str = "employee", department: str = "",
) -> dict:
    """入库任务状态(轮询入口): 阶段/重试次数/错误/块数/告警。

    与预览同一个权限口径: 进度会说话出一篇文档的存在与它的块数/失败原因。
    """
    try:
        return await service.get_task_state(
            task_id, _principal(operator or user_id, role, department)
        )
    except service.UploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/ingest", status_code=202)
async def ingest_doc(req: IngestRequest) -> dict:
    """Phase 2(异步): 同步校参 + 建入库任务, 返回 202 + task_id。

    参数错误(非法可见性/缺 dept/上传已过期或解析失败)仍然是 400: 这类错应该当场
    告诉调用者, 而不是"已受理"后在后台默默失败。
    """
    trace_id = new_trace_id()
    try:
        result = await service.submit_ingest(
            req.upload_id, req.tags, req.uploader,
            visibility=req.visibility, dept_id=req.dept_id, allowed_roles=req.allowed_roles,
        )
    except service.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if req.doc_key and req.doc_key != result["doc_key"]:
        raise HTTPException(status_code=400, detail="doc_key 与 upload_id 不对应")
    get_audit_logger().log(
        trace_id, "docs", "ingest_accepted",
        {"doc_key": result["doc_key"], "task_id": result["task_id"],
         "upload_id": result["upload_id"], "tags": result["tags"],
         "duplicate": result["duplicate"], "stage": result["stage"],
         "uploader": req.uploader, "visibility": result["visibility"]},
    )
    return result


@router.put("/{doc_key}/acl")
async def update_doc_acl(doc_key: str, req: AclRequest) -> dict:
    """Change a document's visibility (metadata table + knowledge chunk rows).

    只有文档所有者或 admin 能改(见 service.can_manage_document 的口径)。
    """
    trace_id = new_trace_id()
    operator = _require_identity(req.operator)
    principal = _principal(operator, req.role, req.department)
    try:
        result = await service.update_document_acl(
            doc_key, req.visibility, req.dept_id, req.allowed_roles, principal
        )
    except service.UploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.DocumentForbidden as exc:
        get_audit_logger().log(
            trace_id, "docs", "document_acl_denied",
            {"doc_key": doc_key, "operator": operator, "role": principal.role.value,
             "reason": str(exc)},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:  # e.g. database / pgvector unavailable
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    get_audit_logger().log(
        trace_id, "docs", "document_acl_updated",
        {"doc_key": doc_key, "operator": req.operator,
         "visibility": result["visibility"], "dept_id": result["dept_id"],
         "allowed_roles": result["allowed_roles"], "chunks_updated": result["chunks_updated"]},
    )
    return result


@router.delete("/{doc_key}")
async def delete_doc(
    doc_key: str, operator: str = "", role: str = "employee", department: str = ""
) -> dict:
    """Remove a document: knowledge chunk rows + metadata + upload files.

    删除不可恢复, 同样只允许所有者本人或 admin 做。
    """
    trace_id = new_trace_id()
    actor = _require_identity(operator)
    principal = _principal(actor, role, department)
    try:
        result = await service.delete_document(doc_key, principal)
    except service.UploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.DocumentForbidden as exc:
        get_audit_logger().log(
            trace_id, "docs", "document_delete_denied",
            {"doc_key": doc_key, "operator": actor, "role": principal.role.value,
             "reason": str(exc)},
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:  # e.g. database / pgvector unavailable
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    get_audit_logger().log(
        trace_id, "docs", "document_deleted",
        {"doc_key": doc_key, "operator": actor, "role": principal.role.value},
    )
    return result


@router.get("")
async def list_docs(
    user_id: str = "", operator: str = "", role: str = "employee", department: str = ""
) -> list[dict]:
    """Document list for the management page, 按调用者能看到的范围裁剪。"""
    return await service.list_documents(_principal(operator or user_id, role, department))


@router.get("/tags")
async def list_all_tags() -> list[dict]:
    """All known tags."""
    return await service.list_tags()
