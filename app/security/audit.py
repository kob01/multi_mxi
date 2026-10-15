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
  :func:`shutdown_audit`(lifespan 已挂, atexit 兜底)。此边界在 OTel 主写路径同样成立
  (BatchLogRecordProcessor 也是后台线程攒批), 所以关停必须走 shutdown。
- 哨兵之后入队的记录没人消费了: 所以关停后的迟到 log 由 :meth:`_enqueue` 直接
  同步落盘, 不静默丢弃。

OTel 化(路线: Logs API 主写 + 原生兜底, 二者并存而非替换):
- 主写路径把每条审计变成一个 OTel LogRecord —— trace_id 复用审计号本身
  (new_trace_id() 产出的 32 位小写 hex 已是合法 W3C 128-bit, 直接转 int 建链路关联,
  无需换号也不破坏 ChatResponse/Langfuse seed 派生/运维 grep 三方依赖), 落盘交给
  BatchLogRecordProcessor + 自定义 _JsonlFileExporter(一次 open('a') 批量追加)。
- **数据面完全不变**: 两条件路径共用同一份 :meth:`_build_line`, 写出的行 JSON 字节
  逐字一致, SIEM 采集与 grep 复盘无感。
- **留痕永不失败红线不破**: OTel 只在 import 期惰性可选(本模块顶部不 import SDK),
  未装 SDK / 配置关闭 / 初始化失败 / 单条 emit 异常, 任一种都只把那一条降级到原生
  队列, 绝不冒泡回图节点。测试注入自定义 writer(AuditLogger(writer=...))时强制走原生。
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
    """Generate a unique trace id for one user turn.

    返回 32 位小写 hex —— 这本身就是合法的 W3C/OTel 128-bit trace-id, 因此 OTel 主写
    路径可直接 int(hex, 16) 建链路关联, 无需为合规而换号(换号会同时影响 ChatResponse、
    Langfuse create_trace_id(seed=) 派生、运维 grep 三方, 是有意的不改动)。
    """
    return uuid.uuid4().hex


def _w3c_int(trace_id: str) -> int | None:
    """把审计号转成 OTel 的 128-bit trace_id(int); 非合法 hex / 越界 / 为 0 时返回 None。

    None 表示这条记录不带 trace 上下文(而不是报错): 历史降级值如 "unknown"/空串就归这里,
    与"能不能建关联"是两件不同的事, 不该影响能不能留痕。
    """
    value = (trace_id or "").strip()
    if not value:
        return None
    try:
        n = int(value, 16)
    except ValueError:
        return None
    if n <= 0 or n >= (1 << 128):
        return None
    return n


class _JsonlFileExporter:
    """OTel LogExporter 的鸭子类型实现: 把一批 log record 的 body(已序列化 JSON 行)批量追加落盘。

    刻意**不**继承 opentelemetry.sdk._logs.export.LogExporter —— 模块顶部 import SDK 会把
    "审计零依赖"降级路径绑死在第三方包上(违背安全红线); 运行时 BatchLogRecordProcessor 只
    调用 export/force_flush/shutdown 三个方法, 不做 isinstance 校验, 鸭子类型足够。落盘姿态
    与原生 _flush_buf 一致: 一次 open('a') 写整批 + flush, OSError 只记 error 返回 FAILURE。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, batch):  # batch: Sequence[ReadableLogRecord]
        from opentelemetry.sdk._logs.export import LogExportResult

        lines: list[str] = []
        for record in batch:
            lr = getattr(record, "log_record", record)
            body = getattr(lr, "body", None)
            if isinstance(body, str) and body:
                lines.append(body)
        if lines:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                    f.flush()
            except OSError as exc:  # 与原生一致: 落盘失败丢这一批, 只告警不外溢
                logger.error(
                    "audit OTel export write failed (path=%s, %d records lost): %s",
                    self.path, len(lines), exc,
                )
                return LogExportResult.FAILURE
        return LogExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        return None


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
        # OTel 主写通道状态: "uninit" 未尝试 / "ready" 可用 / "disabled" 永久不可用(不重试)。
        # provider/logger/logrecord/trace_flags 只在 _ensure_otel 成功时被填充(惰性, import 期不碰 SDK)。
        self._otel_state: str = "uninit"
        self._otel_provider: Any = None
        self._otel_logger: Any = None
        self._otel_logrecord: Any = None
        self._otel_trace_flags: Any = None

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

        永不抛出、永不阻塞: 序列化在调用线程做(纯 CPU, 微秒级); 落盘优先交给 OTel
        批处理器(主写), 不可用时交给后台线程(兜底)。任一层写不进都只是"这一条走另一条
        路径", 绝不把 IO 异常冒泡回图节点 —— 审计节点若允许失败, "永不失败的留痕步骤"
        反而会把整轮对话打断。
        """
        line = self._build_line(trace_id, actor, action, detail, session_id)
        if line is None:
            return
        # 主写: OTel Logs API(emit 即返回, 落盘由 BatchLogRecordProcessor 批量完成)。
        if self._emit_otel(trace_id, actor, action, session_id, line):
            return
        # 兜底: 原生队列(OTel 关闭/不可用/单条 emit 异常都汇到这里)。
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

    # ------------------------------------------------------------ OTel 主写(Logs API)

    def _build_line(
        self,
        trace_id: str,
        actor: str,
        action: str,
        detail: dict[str, Any] | None,
        session_id: str | None,
    ) -> str | None:
        """两条写路径共用的唯一记录构造点: 打码 + 序列化; 不可序列化返回 None(只告警)。

        字段名/顺序/ts 毫秒 ISO 格式是对外契约(SIEM 采集与 grep 复盘都按它), 任何一条
        路径都不许各自拼一份, 否则会漂出第二套格式。OTel 与原生都拿这同一段 JSON 行。
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
            return json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:  # 不可序列化的 detail(如 ORM 对象)
            logger.error("audit record not serializable (action=%s): %s", action, exc)
            return None

    def _ensure_otel(self) -> bool:
        """惰性建 OTel 主写通道并缓存结论; 返回是否可用(不可用则永久不再重试)。

        三条"不可用"的判定都只意味着回到原生落盘, 不报错: 配置关闭、注入了自定义 writer
        (测试断言批量语义, 必须走原生)、SDK 未安装。构造失败同样降级 —— 审计底座不能因为
        一个可选观测面起不来就少留一行。
        """
        if self._otel_state != "uninit":
            return self._otel_state == "ready"
        with self._lock:
            if self._otel_state != "uninit":
                return self._otel_state == "ready"
            settings = get_settings()
            if self._closed or self._writer_fn is not None or not settings.audit_otel_enabled:
                self._otel_state = "disabled"
                return False
            try:
                from opentelemetry._logs import LogRecord
                from opentelemetry.sdk._logs import LoggerProvider
                from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.trace import TraceFlags
            except Exception as exc:  # noqa: BLE001 - 没装 SDK 只是回到原生落盘
                logger.warning("audit OTel 不可用(未安装 SDK), 退回原生批量落盘: %s", exc)
                self._otel_state = "disabled"
                return False
            try:
                provider = LoggerProvider(
                    resource=Resource.create(
                        {"service.name": settings.audit_service_name or "mxi-audit"}
                    )
                )
                provider.add_log_record_processor(
                    BatchLogRecordProcessor(
                        _JsonlFileExporter(self.path),
                        schedule_delay_millis=_FLUSH_INTERVAL * 1000,
                        max_export_batch_size=_DRAIN_BATCH,
                        export_timeout_millis=5000,
                        max_queue_size=_QUEUE_MAXSIZE,
                    )
                )
                self._otel_provider = provider
                self._otel_logger = provider.get_logger("mxi.audit")
                self._otel_logrecord = LogRecord
                self._otel_trace_flags = TraceFlags.SAMPLED
                self._otel_state = "ready"
                return True
            except Exception as exc:  # noqa: BLE001 - 初始化失败也只是回到原生
                logger.warning("audit OTel 初始化失败, 退回原生批量落盘: %s", exc)
                self._otel_state = "disabled"
                return False

    def _emit_otel(
        self, trace_id: str, actor: str, action: str, session_id: str | None, line: str
    ) -> bool:
        """把这一行交给 OTel 批处理器; 成功返回 True(调用方即不再走原生队列)。

        emit 只把记录入 processor 内存队列(不碰磁盘), 但仍包 try/except: 任何意外都返回
        False 让调用点降级到原生队列, 一条不丢。trace_id 能转成合法 W3C int 才建链路关联,
        否则(span_id 一并)留空 —— 关联能力与留痕能力解耦。
        """
        if self._closed or not self._ensure_otel():
            return False
        try:
            tid = _w3c_int(trace_id)
            record = self._otel_logrecord(
                body=line,
                timestamp=time.time_ns(),
                trace_id=tid,
                span_id=(int(uuid.uuid4().hex[:16], 16) or 1) if tid else None,
                trace_flags=self._otel_trace_flags if tid else None,
                event_name=action or "audit",
                attributes={"mxi.actor": actor or "", "mxi.session_id": session_id or ""},
            )
            self._otel_logger.emit(record=record)
            return True
        except Exception as exc:  # noqa: BLE001 - emit 任何意外都降级原生, 不冒泡
            logger.warning("audit OTel emit 失败, 本条走原生落盘: %s", exc)
            return False

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
        "记录已被取走但还没写完"的窗口里会假报成功。OTel 主写路径先单独 force_flush,
        再走原生屏障(两条路径可能同时有积压: 正常走 OTel, emit 失败的那几条走原生)。
        """
        if self._otel_provider is not None and not self._closed:
            try:
                self._otel_provider.force_flush(int(max(1.0, timeout) * 1000))
            except Exception as exc:  # noqa: BLE001 - 刷不动也只是还没落, 不改结论
                logger.warning("audit OTel force_flush failed: %s", exc)
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
        """排空并停掉落盘线程与 OTel 处理器(供 FastAPI lifespan 与 atexit 调用), 幂等。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            provider = self._otel_provider
            thread = self._thread
        # OTel 主写: provider.shutdown() 内部先 flush 攒批再停 processor 线程。
        # 必须放在 thread 判断之前 —— 纯 OTel 路径下原生 thread 一直是 None, 早退就漏刷。
        if provider is not None:
            try:
                provider.shutdown()
            except Exception as exc:  # noqa: BLE001 - 关停路径不得掩盖主流程, 也不外溢
                logger.warning("audit OTel provider shutdown failed: %s", exc)
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
