"""Full-chain audit logging for every agent/tool hop.

Each audit record is one JSON line with a shared trace_id so the whole
chain (user -> assistant -> intent -> agent -> tool -> response) can be
reconstructed for compliance review.

写入模型(高并发下的关键改动): 调用点全在异步图节点里, 而旧实现是"每条记录开
一次文件 + 写 + 关" —— 一轮对话要落 8~15 条审计, 等于把同步磁盘 IO 压在唯一的
事件循环上; 1000 人共用一个网关进程时每个节点都在排队等磁盘, 整条链路的延迟由
盘决定。现在: 调用方只做 O(1) 入队(不碰磁盘、不阻塞、永不等待), 落盘由一个后台
线程持有句柄批量完成。

代价是"log() 返回时那一行可能还在内存里", 因此:
- 提供显式 :func:`flush_audit`(测试断言"这条已落盘"、以及进程关停路径);
- 队列有上限, 满了丢最老的一条并告警 —— 审计丢一条好过把磁盘抖动传给对话链路;
- 队列/线程按实例各自一份(网关与各 mcp/agent 进程可能写同一个文件, 但各有各的
  落盘线程与句柄, 追加语义由文件层的 O_APPEND 保证)。

两个必须知道的边界:
- 落盘线程是 daemon, 进程被 SIGKILL 时未刷的那批留痕会丢 —— 关停路径一定要走
  :func:`shutdown_audit`(lifespan 已挂, atexit 兜底)。
- 哨兵之后入队的记录没人消费了: 所以关停后的迟到 log 由 :meth:`_enqueue` 直接
  同步落盘, 不静默丢弃。
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.security.masking import mask_sensitive

logger = logging.getLogger(__name__)

# 队列容量: 磁盘持续比生产慢时到此上限就开始丢(而不是无界涨到 OOM)。
_QUEUE_MAXSIZE = 20000
# 每轮最多取多少条 / 最长多久强制落一次盘: 前者摊薄系统调用,
# 后者保证低频记录不会因为"凑不满一批"而长时间滞留内存。
_DRAIN_BATCH = 512
_FLUSH_INTERVAL = 0.2

# 队列元素只有三种: JSON 行(str) / _Barrier 屏障 / _STOP 关停哨兵。
_STOP = object()


class _Barrier:
    """把"此前的记录都已落盘"回传给 :meth:`AuditLogger.flush` 的调用方。"""

    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = threading.Event()


def new_trace_id() -> str:
    """Generate a unique trace id for one user turn."""
    return uuid.uuid4().hex


class AuditLogger:
    """Append-only JSONL audit sink, 异步落盘(调用点非阻塞)。"""

    def __init__(
        self,
        path: str | None = None,
        writer: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.path = Path(path or get_settings().audit_log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # writer 可注入: 默认落文件; 测试里换成计数函数即可断言"批量写而非逐条写"。
        self._writer_fn = writer
        self._q: queue.Queue[Any] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._lock = threading.Lock()  # 保护落盘线程生命周期与 dropped 计数
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._closed = False

    # ------------------------------------------------------------ 生产侧

    def log(
        self,
        trace_id: str,
        actor: str,
        action: str,
        detail: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record one audit line. Sensitive fields are masked first.

        永不抛出、永不阻塞: 序列化在本线程做(纯 CPU, 微秒级), 磁盘 IO 交给后台
        线程。写不进去(队列满/磁盘故障)只降级为告警 —— 审计节点若允许把 IO 异常
        冒泡回图节点, "永不失败的留痕步骤"反而会把整轮对话打断。
        """
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "trace_id": trace_id,
            "session_id": session_id,
            "actor": actor,
            "action": action,
            "detail": mask_sensitive(detail or {}),
        }
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:  # 不可序列化的 detail(如 ORM 对象)
            logger.error("audit record not serializable (action=%s): %s", action, exc)
            return
        self._enqueue(line)

    def _enqueue(self, line: str) -> None:
        """非阻塞入队; 队列满时丢最老的一条并计数, 绝不把调用方挂住。"""
        if self._closed:
            # 关停后的迟到记录: 线程已经不在消费了, 直接同步落这一条(不在热路径)
            self._flush_buf([line])
            return
        self._ensure_thread()
        try:
            self._q.put_nowait(line)
            return
        except queue.Full:
            pass
        except Exception as exc:  # noqa: BLE001 - 留痕组件的任何意外都不能外溢
            logger.error("audit enqueue failed: %s", exc)
            return
        # 满了: 腾一个位置给新记录(丢最老的, 保留最新现场)
        try:
            self._q.get_nowait()
            self._q.put_nowait(line)
        except (queue.Empty, queue.Full):
            return
        with self._lock:
            self._dropped += 1
            dropped = self._dropped
        if dropped == 1 or dropped % 100 == 0:
            logger.error(
                "audit 队列已满(%d), 已丢弃 %d 条留痕记录(磁盘写入跟不上产生速度)",
                _QUEUE_MAXSIZE, dropped,
            )

    # ------------------------------------------------------------ 落盘线程

    def _ensure_thread(self) -> None:
        """惰性起落盘线程(只在第一次 log 时, 不在 import/构造期)。"""
        with self._lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(
                target=self._write_loop, name="audit-writer", daemon=True
            )
            self._thread.start()

    def _write_loop(self) -> None:
        """批量落盘: 把"每条一次 open/write/close"压成"一批一次"。

        每轮先取一条(带超时, 空闲时不烧 CPU), 再把当前积压一并取走; 落盘时机是
        "攒够一批 / 距上次落盘到点 / 遇到屏障"三者之一。收到关停哨兵后先把剩余
        队列冲完再退出, 不留内存垃圾。
        """
        buf: list[str] = []
        last_flush = time.monotonic()
        stopped = False
        while not stopped:
            try:
                item: Any = self._q.get(timeout=_FLUSH_INTERVAL)
            except queue.Empty:
                item = None  # 本轮没取到东西(只是唤醒超时)
            if item is _STOP:
                stopped = True
            elif item is not None:
                stopped = self._consume(item, buf) or stopped
                stopped = self._drain(buf) or stopped
            if buf and (stopped or len(buf) >= _DRAIN_BATCH
                        or time.monotonic() - last_flush >= _FLUSH_INTERVAL):
                self._flush_buf(buf)
                buf.clear()
                last_flush = time.monotonic()

    def _consume(self, item: Any, buf: list[str]) -> bool:
        """处理一个队列元素; 返回"是否要求落盘线程停止"(只有哨兵会)。"""
        if isinstance(item, str):
            buf.append(item)
        elif isinstance(item, _Barrier):
            # 屏障之前的记录必须已落盘: 先冲再唤醒调用方
            self._flush_buf(buf)
            buf.clear()
            item.event.set()
        elif item is _STOP:
            return True
        return False

    def _drain(self, buf: list[str]) -> bool:
        """非阻塞取完当前积压(封顶到一批); 返回是否收到关停哨兵。"""
        while len(buf) < _DRAIN_BATCH:
            try:
                item: Any = self._q.get_nowait()
            except queue.Empty:
                return False
            if item is _STOP:
                return True
            if isinstance(item, _Barrier):
                self._flush_buf(buf)
                buf.clear()
                item.event.set()
            elif isinstance(item, str):
                buf.append(item)
        return False

    def _flush_buf(self, buf: list[str]) -> None:
        if not buf:
            return
        if self._writer_fn is not None:
            try:
                self._writer_fn(list(buf))
            except Exception as exc:  # noqa: BLE001 - 注入的写失败同样不外溢
                logger.error("audit custom writer failed (%d records): %s", len(buf), exc)
            return
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write("\n".join(buf) + "\n")
                f.flush()
        except OSError as exc:
            logger.error(
                "audit log write failed (path=%s, %d records lost): %s", self.path, len(buf), exc
            )

    # ------------------------------------------------------------ 收尾

    def flush(self, timeout: float = 2.0) -> bool:
        """把已入队的记录落盘; 返回是否确认刷完。给测试/脚本/关停路径显式调用。

        用"往队列里放一个屏障 + 等落盘线程唤醒"而不是轮询 ``q.empty()``: 后者在
        "记录已被取走但还没写完"的窗口里会假报成功。
        """
        if self._closed or self._thread is None or not self._thread.is_alive():
            buf: list[str] = []
            self._drain(buf)
            self._flush_buf(buf)
            return self._q.empty()
        barrier = _Barrier()
        try:
            self._q.put(barrier, timeout=max(0.05, timeout))
        except queue.Full:
            return False
        return barrier.event.wait(timeout=max(0.05, timeout))

    def close(self, timeout: float = 2.0) -> None:
        """排空并停掉落盘线程(供 FastAPI lifespan 与 atexit 调用), 幂等。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        if not thread.is_alive():
            # 线程已不在: 自己把剩余队列落盘, 不留内存垃圾
            buf: list[str] = []
            self._drain(buf)
            self._flush_buf(buf)
            return
        try:
            self._q.put_nowait(_STOP)
        except queue.Full:
            logger.warning("audit 关停时队列仍满, 尾部记录可能丢失")
        if thread is not threading.current_thread():
            thread.join(timeout=timeout)

    @property
    def inflight(self) -> int:
        """队列里尚未落盘的记录数(监控与测试用)。"""
        return self._q.qsize()

    @property
    def dropped(self) -> int:
        """因队列满被丢弃的记录数(应当恒为 0; 非 0 说明磁盘是瓶颈)。"""
        return self._dropped


_logger: AuditLogger | None = None
_atexit_registered = False


def get_audit_logger() -> AuditLogger:
    """Process-wide singleton audit logger."""
    global _logger, _atexit_registered
    if _logger is None:
        _logger = AuditLogger()
        if not _atexit_registered:
            # 脚本/进程不显式调 shutdown 时也要把内存里的留痕冲出去, 否则合规日志
            # 会缺最后一批(原实现每条即时落盘, 不存在这个问题)。
            atexit.register(shutdown_audit)
            _atexit_registered = True
    return _logger


def flush_audit(timeout: float = 2.0) -> bool:
    """把已产生的审计记录全部落盘(测试断言 / 关停前调用)。"""
    return _logger.flush(timeout) if _logger is not None else True


def shutdown_audit(timeout: float = 2.0) -> None:
    """排空并关停落盘线程(挂到 FastAPI lifespan, 幂等)。"""
    if _logger is not None:
        _logger.close(timeout)
