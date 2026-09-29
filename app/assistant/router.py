"""FastAPI router exposing the single Assistant entry point.

端点:
    POST /api/chat                 非流式一次性问答(保留, 评测脚本等仍可用)
    POST /api/chat/stream          流式问答: 启动 run 并在本连接上从 0 号事件续读
    GET  /api/chat/stream/{run_id} 断点重连: 凭 Last-Event-ID 重放并续流
    GET  /api/sessions             某用户的会话列表
    GET  /api/sessions/{id}/messages  一个会话的完整历史(刷新后回填)
    DELETE /api/sessions/{id}      删除会话及其记录
    GET  /api/files/reports/{name} 回取分析产物文件（图表 SVG/PNG / 报告 Markdown / 数据 CSV）
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.assistant.graph import get_orchestrator
from app.assistant.stream import RunOverloaded, get_stream_hub, sse_frame
from app.chat_store import get_chat_store
from app.config import get_settings
from app.db.session import db_available
from app.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["assistant"])

# SSE 响应头: 禁缓存 + 禁代理缓冲(Nginx 等), 保证逐帧直达。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _stream_response(run_id: str, from_id: int) -> StreamingResponse:
    """把一个 run 的事件缓冲区包装成 SSE 响应(重放 + 续读一体)。"""
    buf = get_stream_hub().get(run_id)
    if buf is None:
        # 缓冲区已过期(TTL)或服务已重启: 前端凭 404 降级为拉取会话历史
        raise HTTPException(status_code=404, detail="run not found or expired")

    async def frames() -> AsyncIterator[str]:
        # 先声明 run_id, 前端据此持久化以便刷新后重连
        yield f"id: 0\nevent: message\ndata: {json.dumps({'type': 'run', 'run_id': run_id}, ensure_ascii=False)}\n\n"
        try:
            async for event_id, event in buf.iterate(from_id):
                yield sse_frame(event_id, event)
        except asyncio.CancelledError:  # 客户端断开: 缓冲区保留, run 继续跑
            logger.debug("SSE client disconnected from run %s", run_id)
            raise

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Unified chat endpoint: every user message enters here."""
    try:
        return await get_orchestrator().handle(req)
    except RunOverloaded as exc:
        # 过载不是服务故障: 给 503 + Retry-After, 让前端/代理自己退避重试
        raise _overloaded(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Start one streaming turn; events are replayable after disconnect."""
    try:
        run_id = await get_orchestrator().handle_stream(req)
    except RunOverloaded as exc:
        raise _overloaded(exc) from exc
    return _stream_response(run_id, from_id=0)


def _overloaded(exc: RunOverloaded) -> HTTPException:
    """并发闸门拒绝 -> 503(带 Retry-After, 避免前端无退避地死重试放大雪崩)。"""
    return HTTPException(
        status_code=503,
        detail=str(exc),
        headers={"Retry-After": "5"},
    )


@router.get("/chat/stream/{run_id}")
async def chat_stream_resume(
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """页面刷新/断网后的重连端点: 从 Last-Event-ID(不含)之后重放并续流。"""
    try:
        from_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        from_id = 0
    return _stream_response(run_id, from_id=from_id)


@router.get("/sessions")
async def list_sessions(user_id: str, limit: int = 50) -> list[dict]:
    """某用户最近的会话列表(标题 + 时间), 供前端展示/切换。"""
    return await get_chat_store().list_sessions(user_id, limit=limit)


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str) -> list[dict]:
    """一个会话的全部历史消息(含思考过程/路由/参考来源), 时间正序。"""
    return await get_chat_store().get_messages(session_id)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """删除一个会话及其全部消息记录。"""
    await get_chat_store().delete_session(session_id)
    return {"status": "deleted", "session_id": session_id}


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """就绪探针: 只回答"本进程能不能接新对话", 不报健康分。

    与 /api/health(存活探针)分开: 一个进程可以活着但已过载/未初始化完,
    运维侧需要的是"把新流量从负载器上摘下来"而不是"重启它"。
    依赖级降级(ES/TEI/Redis 不可用)不算不就绪 —— 那些路径本就有降级行为。
    """
    hub = get_stream_hub()
    limit = max(1, get_settings().stream_max_concurrent_runs)
    orchestrator = get_orchestrator()
    checks = {
        "inflight_runs": hub.inflight,
        "run_limit": limit,
        "graph_ready": orchestrator._graph is not None,  # noqa: SLF001
        "db_ready": db_available(),
    }
    ready = checks["graph_ready"] and hub.inflight < limit
    return JSONResponse(
        {"status": "ready" if ready else "busy", **checks},
        status_code=200 if ready else 503,
    )


@router.get("/files/reports/{name}")
async def get_report_file(name: str) -> FileResponse:
    """回取分析产物(图表 SVG/PNG / 报告 Markdown / 表格 CSV)。

    安全: 文件名走 app.analytics.store 的白名单正则校验(挡掉一切路径穿越),
    且只在 report_dir 目录内解析后的绝对路径才回文件; 不做目录列表。
    扩展名不在 ``analytics.store._EXT_KIND`` 里的文件根本进不了台账/下载
    (原网页创作工坊下线的 HTML 已不在该白名单内, 不可回取)。

    下面对 HTML 的 CSP sandbox 分支是那道产物还在时的护栏, 现处于休眠状态:
    保留是为了"若将来又开静态页入口, 不会忘了同源隔离" —— 新接入可下发 HTML 的
    产物时必须带上那段 sandbox(成品页与业务系统同源, 不加这道头一段恶意脚本就能
    读本域 cookie/localStorage 并调内网 API; sandbox 不带 allow-same-origin)。
    """
    from app.analytics.store import is_safe_name

    if not is_safe_name(name):
        raise HTTPException(status_code=400, detail="invalid artifact name")
    directory = Path(get_settings().report_dir).resolve()
    path = (directory / name).resolve()
    # resolve 后再确认仍在 report_dir 内: 双保险挡符号链接/相对路径穿越。
    if path.parent != directory or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    media = "image/svg+xml" if path.suffix == ".svg" else "text/markdown; charset=utf-8"
    headers: dict[str, str] | None = None
    if path.suffix.lower() == ".html":
        media = "text/html; charset=utf-8"
        headers = {
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals",
            "X-Content-Type-Options": "nosniff",
        }
    return FileResponse(path, media_type=media, headers=headers)
