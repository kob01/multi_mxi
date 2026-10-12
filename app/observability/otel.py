"""OTel span 导出(问数状态机每个阶段一条 span + 归因要用的属性)。

姿态与 :mod:`app.tracing` 的 Langfuse 一致: **开关关、地址空、或没装 SDK 就整体无操作**,
绝不因为可观测不可用而影响回答。这里刻意不引 auto-instrumentation(那要改启动方式并给
每个第三方库打 patch), 只手工埋状态机的 8 个阶段 —— 归因需要的是"这轮的 conf/route/
tokens/semver", 不是框架调用图。

属性名单就是方案 §7 那份: conf(槽位置信)、recall(召回分)、route、retries、tokens、semver。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import get_settings

logger = logging.getLogger(__name__)

_initialized = False
_disabled = False
_tracer = None

# span 属性里的敏感面: 用户原话与 SQL 都不进 OTel(链路系统通常比数据库更宽松),
# 只带结构与分数 —— 归因看形状与决策依据就够了, 看内容应该去留痕表(那里有权限门)。
_MAX_ATTR_CHARS = 200


def enabled() -> bool:
    settings = get_settings()
    return bool(settings.askdata_otel_enabled and (settings.otel_endpoint or "").strip())


def init() -> bool:
    """惰性初始化 tracer; 返回是否真的可用(不可用时调用侧无感)。"""
    global _initialized, _disabled, _tracer
    if _tracer is not None:
        return True
    if _disabled or not enabled():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider(id_generator=RandomIdGenerator())
            trace.set_tracer_provider(provider)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=get_settings().otel_endpoint))
        )
        _tracer = provider.get_tracer(get_settings().otel_service_name)
        _initialized = True
        logger.info("[askdata] OTel 已启用 -> %s", get_settings().otel_endpoint)
    except Exception as exc:  # noqa: BLE001 - 没装 SDK/地址不通都只是"这一路没有 trace"
        _disabled = True
        logger.warning("[askdata] OTel 不可用(本轮起不再尝试): %s", exc)
    return _tracer is not None


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """一条阶段 span: ``with span("askdata.link", {...}) as extra: extra["recall"] = ...``。

    返回的 dict 让调用侧在块内补充属性(比如召回分数要等工具回来才知道), 而不用把
    span 对象本身散到业务代码里。未启用时返回一个空 dict 的哑上下文。
    """
    if not init():
        yield {}
        return
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    with _tracer.start_as_current_span(f"askdata.{name}") as current:  # type: ignore[union-attr]
        for key, value in (attributes or {}).items():
            _set(current, key, value)
        try:
            yield extra
        except Exception as exc:  # noqa: BLE001 - span 记下异常后原样抛出, 不吞
            try:
                current.record_exception(exc)
                _mark_error(current, str(exc)[:200])
            except Exception:  # noqa: BLE001 - 可观测写失败不能替掉业务异常本身
                logger.debug("otel span 记异常失败(忽略)", exc_info=True)
            raise
        finally:
            _set(current, "duration_ms", round((time.perf_counter() - started) * 1000, 1))
            for key, value in extra.items():
                _set(current, key, value)


def _set(current: Any, key: str, value: Any) -> None:
    try:
        if isinstance(value, (list, tuple, dict)):
            text = str(value)
            current.set_attribute(key, text[:_MAX_ATTR_CHARS])
        elif value is not None:
            current.set_attribute(key, value)
    except Exception:  # noqa: BLE001 - 属性形态问题不该影响回答
        return


def emit(name: str, attributes: dict[str, Any] | None = None, *, duration_ms: float = 0.0) -> None:
    """一条已经结束的 span(节点已经跑完再补记的场景, 不需要嵌套上下文)。"""
    if not init():
        return
    with _tracer.start_as_current_span(f"askdata.{name}") as current:  # type: ignore[union-attr]
        for key, value in (attributes or {}).items():
            _set(current, key, value)
        _set(current, "duration_ms", round(duration_ms, 1))


def _mark_error(current: Any, message: str) -> None:
    """把 span 标成错误(带描述)。拿不到 SDK 的 StatusCode 时什么都不做。"""
    if _STATUS_CODE is None:
        return
    try:
        current.set_status(_STATUS_CODE.ERROR, message)
    except Exception:  # noqa: BLE001
        return


def flush() -> None:
    """进程关停前把批量队列里的 span 推出去(否则最后几轮会留在内存里丢掉)。"""
    if _tracer is None:
        return
    try:
        provider = _tracer.get_provider() if hasattr(_tracer, "get_provider") else None
        if provider is not None and hasattr(provider, "force_flush"):
            provider.force_flush()
    except Exception as exc:  # noqa: BLE001 - 关停路径上的失败只记日志
        logger.warning("otel flush failed: %s", exc)


try:  # 状态码类型只在 SDK 里存在; 拿不到就退化成只记录异常不标状态。
    from opentelemetry.trace import StatusCode as _STATUS_CODE  # type: ignore
except Exception:  # noqa: BLE001
    _STATUS_CODE = None
