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
    skip    {"until_id": N, "message": ...}    更早事件已按上限回收(仅溢出时产生)
    result  {ChatResponse 字段 + message_id}  最终结构化结果
    done    {"status": "completed|error"}     本轮结束(必有)
    error   {"message": "..."}                异常说明

过载行为: 新建 run 先过 ``stream_max_concurrent_runs`` 闸门(:meth:`StreamHub.try_acquire_run`),
拿不到额度就抛 :class:`RunOverloaded`(路由层转 503 + Retry-After) —— 宁可让后来的人
立刻知道"现在忙", 也不要把已有所有人的回复拖进集体超时。
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


class RunOverloaded(RuntimeError):
    """同时在跑的 run 已达上限: 路由层转 503, 不排队也不静默降级。"""


class RunBuffer:
    """单个 run 的事件缓冲: 自增 id + Event 唤醒, 支持多读者断点重放。

    并发规模下两个硬约束(1000 人同时在流时缺一不可):
    - 读侧用 Event 唤醒而不是 50ms 轮询: 轮询时每 50ms 都重扫整条事件列表,
      开销是 O(事件数 × 读者数 × 20 次/秒), 几千条 token 事件会把事件循环烧穿;
      改为二分定位切片 + 新事件 set 唤醒, 空闲读者只挂在一个 Event 上。
    - 事件条数有上限(``stream_max_events``): 超限后丢弃最老事件, 内存有顶;
      续流凭"尾部 + id 单调"仍然成立, 最终 result 事件不会被丢。
    id 仍然单调递增(只是可能有空洞), "id > cursor" 的过滤式重放天然幂等。
    """

    def __init__(self, max_events: int | None = None) -> None:
        self.events: list[tuple[int, dict[str, Any]]] = []
        self.done = False
        self.finished_at: float | None = None
        self._next_id = 0
        # 已丢弃事件的最大 id(读者 cursor 低于它说明历史不可全部回放了)
        self._lowest_id = 0
        # 跳号提示单独存而不混进 events: 逐条丢弃时 events 始终是连号区间,
        # 把提示插进去反而会造成"提示自己也被丢弃"或反复插删。
        # id 取"第一条被丢的事件"的号: 它永远低于现存事件, 且已读到它的读者
        # (cursor >= 它)不会被重放第二遍。
        self._skip: tuple[int, dict[str, Any]] | None = None
        self._max_events = max(
            16, max_events if max_events is not None else get_settings().stream_max_events
        )
        self._new_event = asyncio.Event()

    async def append(self, event: dict[str, Any]) -> int:
        """追加一个事件, 返回其自增 id(单事件循环内无竞态, 无需加锁)。"""
        self._next_id += 1
        self.events.append((self._next_id, event))
        dropped = len(self.events) - self._max_events
        if dropped > 0:
            self._fold_discarded(self.events[:dropped])
            self.events = self.events[dropped:]
        self._new_event.set()
        return self._next_id

    def _fold_discarded(self, discarded: list[tuple[int, dict[str, Any]]]) -> None:
        """把被丢弃的最老事件折叠成一条可回放的"跳号"提示。"""
        if not discarded:
            return
        first_id = discarded[0][0]
        self._lowest_id = discarded[-1][0]
        self._skip = (
            first_id,
            {
                "type": "skip",
                "until_id": self._lowest_id,
                "message": "更早的事件已按缓冲区上限回收, 最终结果仍会完整下发",
            },
        )

    async def mark_done(self) -> None:
        self.done = True
        self.finished_at = time.monotonic()
        self._new_event.set()  # 唤醒全部空闲读者收尾

    def pending(self, cursor: int) -> list[tuple[int, dict[str, Any]]]:
        """cursor 之后的未读事件(由 _next_id 单调性保证升序, 二分定位即连续区间)。

        丢弃过历史时先补一条 skip 提示: 它的 id 是被丢区间的第一个号, 所以
        已读到它的读者不会被重放第二遍, 而迟到的读者能知道"前面的历史拿不回来了"。
        """
        if cursor >= self._next_id:
            return []
        idx = bisect_key(self.events, cursor)
        batch = self.events[idx:]
        if self._skip is not None and self._skip[0] > cursor:
            return [self._skip, *batch]
        return batch

    async def wait_new(self, timeout: float = 1.0) -> None:
        """等一次"新事件/done"的唤醒(不 clear 标记: 清除动作属于读者循环的开头)。

        所有读者共用同一个 Event(append/mark_done 统一 set), 所以在等待之前清除会
        造成竞态: 刚清完就来一条新事件 -> 那个 set 被抹掉 -> 这位读者白等一个 timeout。
        正确次序是"先 clear 再取 pending"(见 :meth:`iterate` 循环开头)。
        """
        try:
            await asyncio.wait_for(self._new_event.wait(), timeout)
        except TimeoutError:
            pass

    def mark_seen(self) -> None:
        """清除唤醒标记: 必须紧接在取 pending 之前调用(与 :meth:`iterate` 配对)。"""
        self._new_event.clear()

    async def iterate(self, from_id: int = 0) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """从 ``from_id``(不含)开始重放历史事件, 再接续实时事件直到 done。

        事件 id 单调递增(超限后允许有空洞, 空洞由 skip 提示补齐语义),
        多标签页并发读取各自独立; 客户端断开时生成器被 cancel, 缓冲区不受影响。
        """
        cursor = from_id
        while True:
            # 先清后取: 清除与取数之间新到的事件必然在 batch 里, 取数之后新到的
            # 事件会重新 set 标记, wait 立即返回 —— 既不丢唤醒也不会空等一个超时。
            self.mark_seen()
            batch = self.pending(cursor)
            for i, e in batch:
                cursor = i
                yield i, e
            if self.done:
                return
            if not batch:
                await self.wait_new()


def bisect_key(events: list[tuple[int, Any]], cursor: int) -> int:
    """首个 id > cursor 的下标(事件列表按 id 升序, 二分 O(log n))。"""
    lo, hi = 0, len(events)
    while lo < hi:
        mid = (lo + hi) // 2
        if events[mid][0] <= cursor:
            lo = mid + 1
        else:
            hi = mid
    return lo


class StreamHub:
    """run_id -> RunBuffer 的进程内注册表 + 同时在跑的 run 并发闸门。

    三道治理(都是 1000 人共用一个网关进程时的硬需求):
    1. TTL 惰性回收(原行为) + 限频扫描: 原实现每条命令全表扫一遍,
       百个活跃 run x 每秒几百条事件时扫描本身就成了热路径开销。
    2. 缓冲区总数硬顶(``stream_max_buffers``): 触顶时按结束时间最老提前回收,
       否则"刷新后再也不回来"的会话会把 finished 缓冲区堆到内存耗尽。
    3. 并发闸门(``stream_max_concurrent_runs``): 新建 run 前先过闸, 超上限立刻
       拒绝(路由层转 503), 而不是让所有下游(LLM 配额/PG/事件循环)被拖到集体超时。
    """

    def __init__(self) -> None:
        self._buffers: dict[str, RunBuffer] = {}
        self._last_sweep = 0.0
        self._inflight = 0

    # ---------------- 并发闸门 ----------------

    def try_acquire_run(self) -> bool:
        """非阻塞取一个并发额度; 取不到 = 过载, 调用方应立即拒绝本次请求。

        自己计数而不是用 asyncio.Semaphore: 闸门只要求"同时不超上限", 没有等待者,
        拿得到信号量内部的私有属性反而更险(需要 hack ``_value``)。
        """
        if self._inflight >= max(1, get_settings().stream_max_concurrent_runs):
            return False
        self._inflight += 1
        return True

    def release_run(self) -> None:
        """归还并发额度(必须在 run 终态的 finally 里调用, 泄漏会永久收紧闸门)。"""
        self._inflight = max(0, self._inflight - 1)

    @property
    def inflight(self) -> int:
        return self._inflight

    # ---------------- 缓冲区注册表 ----------------

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
        """回收结束超过 ``stream_buffer_ttl`` 秒的缓冲区; 限频 + 总量硬顶。

        只回收已结束的 run: 在跑的缓冲区即使触顶也不能丢(读者还在等它的事件),
        硬顶只约束"结束后还要白占 TTL 秒"的那部分内存。
        """
        now = time.monotonic()
        if now - self._last_sweep >= 5.0:
            self._last_sweep = now
            ttl = get_settings().stream_buffer_ttl
            expired = [
                rid
                for rid, buf in self._buffers.items()
                if buf.done and buf.finished_at is not None and now - buf.finished_at > ttl
            ]
            for rid in expired:
                self._buffers.pop(rid, None)
        cap = max(1, get_settings().stream_max_buffers)
        overflow = len(self._buffers) - cap
        if overflow > 0:
            finished = sorted(
                (buf.finished_at or 0.0, rid) for rid, buf in self._buffers.items() if buf.done
            )
            for _when, rid in finished[:overflow]:
                self._buffers.pop(rid, None)
            if len(self._buffers) > cap:
                logger.warning(
                    "StreamHub 缓冲区数 %d 超过硬顶 %d 且无足够已结束 run 可回收", len(self._buffers), cap
                )


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
