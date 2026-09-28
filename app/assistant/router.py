"""FastAPI router exposing the single Assistant entry point.

端点:
    POST /api/chat                 非流式一次性问答(保留, 评测脚本等仍可用)
    POST /api/chat/stream          流式问答: 启动 run 并在本连接上从 0 号事件续读
    GET  /api/chat/stream/{run_id} 断点重连: 凭 Last-Event-ID 重放并续流
    GET  /api/sessions             某用户的会话列表
    GET  /api/sessions/{id}/messages  一个会话的完整历史(刷新后回填)
    DELETE /api/sessions/{id}      删除会话及其记录
    GET  /api/files/reports/{name} 回取 Analyst_Agent 生成的图表(SVG)/报告(Markdown)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.assistant.graph import get_orchestrator
from app.assistant.stream import get_stream_hub, sse_frame
from app.chat_store import get_chat_store
from app.config import get_settings
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Start one streaming turn; events are replayable after disconnect."""
    run_id = await get_orchestrator().handle_stream(req)
    return _stream_response(run_id, from_id=0)


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


@router.get("/files/reports/{name}")
async def get_report_file(name: str) -> FileResponse:
    """回取分析产物(图表 SVG / 报告 Markdown), 供前端内嵌展示。

    安全: 文件名走 app.analytics.store 的白名单正则校验(挡掉一切路径穿越),
    且只在 report_dir 目录内解析后的绝对路径才回文件; 不做目录列表。
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
    return FileResponse(path, media_type=media)
