"""SSE 断点续传缓冲区 (StreamHub)。

核心思路: 把 "run(图的执行)" 与 "HTTP 连接" 解耦 —— 图在后台 asyncio 任务里
跑, 产生的事件按自增 id 写入进程内缓冲区; SSE 连接只是缓冲区的一个读者,
随时可以断, 断了凭 ``Last-Event-ID`` 再进来: 先重放已产生的历史事件, 再接续
实时事件, 直到 run 结束。页面刷新因此不会丢一次正在生成的回答。

与 Session Memory 同一进程内定位(见 app/assistant/memory.py 的降级说明):
单 worker 部署有效; 服务重启后未完成的 run 不可续流, 前端凭 404 降级为拉
取已持久化的会话历史(app/chat_store.py)。

事件协议(data 均为 JSON, 每条带自增 id 作为 SSE event id):
    status  {"stage": "...", "text": "..."}   节点进度(检索中/调用工具中…)
    think   {"delta": "..."}                  思考过程增量
    token   {"delta": "..."}                  回答正文增量
    result  {ChatResponse 字段 + message_id}  最终结构化结果
    done    {"status": "completed|error"}     本轮结束(必有)
    error   {"message": "..."}                异常说明
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def new_run_id() -> str:
    """一次图执行(run)的标识, 前端持久化后凭它跨刷新续流。"""
    return uuid.uuid4().hex


class RunBuffer:
    """单个 run 的事件缓冲: 自增 id + 轮询读取, 支持多读者断点重放。

    事件量小(token 粒度 × 并发 run 数), 读侧用 50ms 轮询而不是 Condition:
    避免 wait_for(Condition.wait()) 超时取消时的重获锁边界问题。
    """

    def __init__(self) -> None:
        self.events: list[tuple[int, dict[str, Any]]] = []
        self.done = False
        self.finished_at: float | None = None
        self._next_id = 0

    async def append(self, event: dict[str, Any]) -> int:
        """追加一个事件, 返回其自增 id(单事件循环内无竞态, 无需加锁)。"""
        self._next_id += 1
        self.events.append((self._next_id, event))
        return self._next_id

    async def mark_done(self) -> None:
        self.done = True
        self.finished_at = time.monotonic()

    async def iterate(self, from_id: int = 0) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """从 ``from_id``(不含)开始重放历史事件, 再接续实时事件直到 done。

        事件 id 单调递增且无空洞, "id > cursor" 的过滤式重放天然幂等,
        多标签页并发读取也各自独立。
        """
        cursor = from_id
        while True:
            batch = [(i, e) for i, e in self.events if i > cursor]
            for i, e in batch:
                cursor = i
                yield i, e
            if self.done:
                return
            if not batch:
                await asyncio.sleep(0.05)


class StreamHub:
    """run_id -> RunBuffer 的进程内注册表(惰性清理过期缓冲区)。"""

    def __init__(self) -> None:
        self._buffers: dict[str, RunBuffer] = {}

    def create(self, run_id: str) -> RunBuffer:
        buf = RunBuffer()
        self._buffers[run_id] = buf
        self._sweep()
        return buf

    def get(self, run_id: str) -> RunBuffer | None:
        self._sweep()
        return self._buffers.get(run_id)

    async def append(self, run_id: str, event: dict[str, Any]) -> int | None:
        buf = self._buffers.get(run_id)
        if buf is None:
            return None
        return await buf.append(event)

    async def finish(self, run_id: str) -> None:
        buf = self._buffers.get(run_id)
        if buf is not None:
            await buf.mark_done()

    def _sweep(self) -> None:
        """回收结束超过 ``stream_buffer_ttl`` 秒的缓冲区(惰性, 无需定时器)。"""
        ttl = get_settings().stream_buffer_ttl
        now = time.monotonic()
        expired = [
            rid
            for rid, buf in self._buffers.items()
            if buf.done and buf.finished_at is not None and now - buf.finished_at > ttl
        ]
        for rid in expired:
            self._buffers.pop(rid, None)


_hub: StreamHub | None = None


def get_stream_hub() -> StreamHub:
    """进程级单例 StreamHub。"""
    global _hub
    if _hub is None:
        _hub = StreamHub()
    return _hub


def sse_frame(event_id: int, event: dict[str, Any]) -> str:
    """一条 SSE 帧: ``id:`` 供 Last-Event-ID 续传, ``data:`` 为事件 JSON。"""
    payload = json.dumps(event, ensure_ascii=False)
    return f"id: {event_id}\nevent: message\ndata: {payload}\n\n"
