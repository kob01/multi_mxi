"""Document management API: upload -> tag suggestion -> confirm ingest.

权限口径(与 /api/memory 一致): 本系统没有 token, 身份由请求里的
``uploader``/``operator`` + ``role`` + ``department`` 声明, 因此服务层只能做两件事:
改可见性/删除限"所有者本人或 admin", 列表按调用者的文档 ACL 裁剪。它挡住的是
误操作与伪造角色越权改写, 不是伪造工号本身(那需要真正的认证)。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.docs import service
from app.docs.parsers import modality_of, parse_blocks
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
    """Phase-2 confirmation payload from the upload page."""

    doc_key: str
    filename: str
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


@router.post("/upload")
async def upload_doc(file: UploadFile = File(...), uploader: str = Form("anonymous")) -> dict:
    """Phase 1: save + parse + duplicate check + LLM tag suggestion.

    上传走流式落盘(:func:`service.stage_upload`), 不再一次把整个文件读进内存:
    多人同时传几十 MB 时, ``await file.read()`` 会同时握住所有体(内存尖峰直
    接 OOM), 而流式版本内存恒等于一个分块。
    """
    trace_id = new_trace_id()
    try:
        doc_key, path, ext = await service.stage_upload(file.filename or "unnamed", file)
        _, blocks = await parse_blocks(path)
    except service.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:  # e.g. MinerU service unavailable
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    existing = await service.check_existing(doc_key)
    preview = "\n\n".join(b.text for b in blocks)
    tags = await service.suggest_tags(preview)
    get_audit_logger().log(
        trace_id, "docs", "upload_received",
        {"doc_key": doc_key, "filename": path.name, "ext": ext,
         "overwrite": existing is not None, "uploader": uploader},
    )
    return {
        "doc_key": doc_key,
        "filename": path.name,
        "ext": ext,
        "modality": modality_of(path),
        "overwritten": existing is not None,
        "suggested_tags": tags,
        "preview_len": len(preview),
        "preview": preview[:500],
    }


@router.post("/ingest")
async def ingest_doc(req: IngestRequest) -> dict:
    """Phase 2: chunk + embed + pgvector overwrite + document metadata."""
    trace_id = new_trace_id()
    try:
        result = await service.ingest_confirmed(
            req.doc_key, req.filename, req.tags, req.uploader,
            visibility=req.visibility, dept_id=req.dept_id, allowed_roles=req.allowed_roles,
        )
    except service.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    get_audit_logger().log(
        trace_id, "docs", "ingest_completed",
        {"doc_key": req.doc_key, "filename": req.filename, "tags": req.tags,
         "chunk_count": result["chunk_count"], "uploader": req.uploader,
         "visibility": result["visibility"]},
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
