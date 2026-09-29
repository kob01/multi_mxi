"""Document management API: upload -> tag suggestion -> confirm ingest."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.docs import service
from app.docs.parsers import modality_of, parse_blocks
from app.security.audit import get_audit_logger, new_trace_id

router = APIRouter(prefix="/api/docs", tags=["docs"])


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
    """Document visibility change payload (admin/owner operation)."""

    visibility: str
    dept_id: str = ""
    allowed_roles: list[str] = Field(default_factory=list)
    operator: str = "anonymous"


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
    """Change a document's visibility (metadata table + knowledge chunk rows)."""
    trace_id = new_trace_id()
    try:
        result = await service.update_document_acl(
            doc_key, req.visibility, req.dept_id, req.allowed_roles, req.operator
        )
    except service.UploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
async def delete_doc(doc_key: str, operator: str = "anonymous") -> dict:
    """Remove a document: knowledge chunk rows + metadata + upload files."""
    trace_id = new_trace_id()
    try:
        result = await service.delete_document(doc_key)
    except service.UploadError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:  # e.g. database / pgvector unavailable
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    get_audit_logger().log(
        trace_id, "docs", "document_deleted",
        {"doc_key": doc_key, "operator": operator},
    )
    return result


@router.get("")
async def list_docs() -> list[dict]:
    """Document list for the management page."""
    return await service.list_documents()


@router.get("/tags")
async def list_all_tags() -> list[dict]:
    """All known tags."""
    return await service.list_tags()
