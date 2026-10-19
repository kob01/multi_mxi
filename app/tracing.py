"""Tracing bootstrap: LangSmith (env-var switch) + Langfuse (callback handler).

LangChain / LangGraph auto-emit traces when the standard ``LANGSMITH_*``
environment variables are present. This module is the single place that
turns that machinery on/off from :class:`app.config.Settings`, so callers
never touch ``os.environ`` directly.

Compliance note (intranet-only deployment):
    Tracing uploads prompt/completion payloads to ``langsmith_endpoint``.
    It MUST stay disabled in production containers. Enable it only on a
    developer machine by setting ``LANGSMITH_TRACING=true`` and a personal
    ``LANGSMITH_API_KEY`` in the local ``.env`` (git-ignored).

Langfuse (与 LangSmith 并存的自托管可观测通道):
    开关 ``langfuse_enabled``, 上报目标是同 compose 网络内自建的 langfuse-web
    (profile=langfuse 栈), 对话数据不出容器网络, 故容器侧允许开启; 仍默认
    false, 由 docker/.env 置 LANGFUSE_ENABLED=true 才生效。密钥只住
    ``docker/secrets/langfuse_api_key.txt``(第一行 secret key / 第二行 public
    key, 容器内挂载为 /run/secrets/langfuse_api_key), 不进 dotenv。
    接入方式: LangChain CallbackHandler 挂在图顶层调用的 config 上(见
    :func:`langfuse_callback`), 全图节点/LLM/工具子 run 自动归并为一条 trace;
    未装 langfuse 包或未配密钥时退化为无操作, 不影响业务链路。
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_initialized = False
_langfuse_initialized = False


def init_tracing() -> bool:
    """Configure LangSmith env vars from settings; return whether active.

    Idempotent: safe to call from both the FastAPI lifespan and the
    LangGraph Studio graph factory.
    """
    global _initialized
    settings = get_settings()

    if not settings.langsmith_tracing or not settings.langsmith_api_key:
        # Explicitly ensure tracing stays off so a stray env var in the
        # container does not silently upload data.
        os.environ.setdefault("LANGSMITH_TRACING", "false")
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint

    if not _initialized:
        logger.info(
            "LangSmith tracing enabled -> project=%s endpoint=%s",
            settings.langsmith_project,
            settings.langsmith_endpoint,
        )
        _initialized = True
    return True


def init_langfuse() -> bool:
    """Enable Langfuse tracing; return whether active (idempotent).

    只做"配置 -> 环境变量"的搬运: langfuse SDK 在构造 CallbackHandler 时
    从 ``LANGFUSE_*`` 环境变量自读。合规口径: 目标是 compose 网络内的自建
    实例(非外部 SaaS), 但默认仍关, 需 compose 注入 LANGFUSE_ENABLED=true 才开。
    """
    global _langfuse_initialized
    settings = get_settings()

    if not settings.langfuse_enabled:
        # 开关未开: 显压住 SDK 自己的总开关, 防容器里残留的 LANGFUSE_* 环境变量
        # 让后续新增的埋点静默往外部上报。
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
        return False

    if not (settings.langfuse_api_key and settings.langfuse_public_key):
        # 开了开关但密钥不全 —— 必须告警而不是默少不报: secret 源文件不存在时
        # Docker 会把 /run/secrets/<name> 挂成**目录**(而非报错), config.py 的
        # is_file() 判定不通过 -> 这里拿到空值, 不提示就看不出任何异常。
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
        logger.warning(
            "LANGFUSE_ENABLED=true 但密钥不全(sk=%s, pk=%s), 本轮不接 trace; "
            "检查宿主机 docker/secrets/langfuse_api_key.txt 是否存在且为两行"
            "(第一行 sk-lf-..., 第二行 pk-lf-...); 注意文件在容器创建时才补建的话, "
            "compose 会把挂载点做成空目录, 需重建容器",
            "有" if settings.langfuse_api_key else "空",
            "有" if settings.langfuse_public_key else "空",
        )
        return False

    # LANGFUSE_BASE_URL 是当前 SDK 的规范键, LANGFUSE_HOST 为旧名, 双写兼容。
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_api_key
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    os.environ["LANGFUSE_BASE_URL"] = settings.langfuse_base_url
    os.environ["LANGFUSE_HOST"] = settings.langfuse_base_url
    os.environ["LANGFUSE_TRACING_ENVIRONMENT"] = settings.langfuse_environment
    os.environ["LANGFUSE_TRACING_ENABLED"] = "true"

    if not _langfuse_initialized:
        logger.info(
            "Langfuse tracing enabled -> base_url=%s environment=%s",
            settings.langfuse_base_url,
            settings.langfuse_environment,
        )
        _langfuse_initialized = True
    return True


def langfuse_callback(
    session_id: str = "", user_id: str = "", trace_id: str = ""
) -> dict:
    """Langfuse 回调 config 片段; 未启用时返回空 dict(调用侧 ** 展开无感)。

    返回形如 ``{"callbacks": [handler], "metadata": {...}, "tags": [...]}``,
    由图顶层调用 ``config={"configurable": ..., **langfuse_callback(...)}``
    合并; handler 挂在顶层 run 上, LangGraph 会把回调传播到所有子 run
    (节点内 LLM/工具/MCP 调用), 全链路归并为一条 trace。

    metadata 里的 ``langfuse_session_id``/``langfuse_user_id``/
    ``langfuse_trace_name`` 是 SDK 识别的约定键(其余键原样落进 trace metadata);
    ``trace_id`` 非空时用 ``Langfuse.create_trace_id(seed=...)`` 把审计日志里的
    trace_id 映射为确定性 W3C trace id —— Langfuse UI 里能直接按 audit.jsonl
    的 trace_id 定位同一轮对话(异常上报三通道与 trace 对齐)。
    """
    if not init_langfuse():
        return {}
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        # 镜像里没装 langfuse: 降级为无 trace, 不影响业务链路。
        logger.warning("langfuse enabled but package missing, skip tracing")
        return {}
    try:
        settings = get_settings()
        kwargs: dict = {}
        if trace_id:
            # seed 派生失败不能影响对话: 丢掉 trace_context 退回 SDK 自生成 id。
            try:
                from langfuse import Langfuse

                kwargs["trace_context"] = {
                    "trace_id": Langfuse.create_trace_id(seed=trace_id)
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("langfuse deterministic trace_id unavailable: %s", exc)
        handler = CallbackHandler(**kwargs)
        meta: dict = {"langfuse_trace_name": settings.langfuse_project}
        if session_id:
            meta["langfuse_session_id"] = session_id
        if user_id:
            meta["langfuse_user_id"] = user_id
        if trace_id:
            # 保留原始 trace_id 到 metadata: Langfuse 侧可按审计号检索。
            meta["mxi_trace_id"] = trace_id
        return {
            "callbacks": [handler],
            "metadata": meta,
            "tags": ["mxi", settings.langfuse_environment],
        }
    except Exception as exc:  # noqa: BLE001 - trace 挂了不能带崩对话链路
        logger.warning("langfuse handler init failed, tracing disabled: %s", exc)
        return {}


def shutdown_langfuse() -> None:
    """进程关停前 flush 批量上报队列, 避开最后几条 trace 丢在内存缓冲区。"""
    if not _langfuse_initialized:
        return
    try:
        from langfuse import get_client

        get_client().shutdown()
    except Exception as exc:  # noqa: BLE001 - 关停路径上的失败只记日志
        logger.warning("langfuse shutdown failed: %s", exc)


# --------------------------------------------------------------- 环节级评测
# 一轮对话只有一条 trace, 但「哪一个环节掉了链子」要看环节自己的分数。这条通道
# 把前三层漏斗里**可确定性判定**的信号按 EVAL_METRICS.md 的指标编号上报成 score,
# 于是 Langfuse UI 里能按 score name 筛「本轮哪些请求降级了 / 阈值裁空了 / 越权
# 命中被剔除了」, 不必去翻 audit.jsonl 再自己聚合。
#
# 为什么不用 score_current_span: 实测在 LangGraph 的节点函数体内 OTel current
# span 是无效的 —— CallbackHandler 在 on_chain_start 里 attach 的 context 不跨
# Pregel 的执行边界传进节点函数, score_current_span 于是静默 no-op (只在日志里
# 留一条 warning), 调用方看不出失败。所以这里显式建 span: 用 trace_context 把
# 环节 span 绑回同一条 trace (seed 与 langfuse_callback 一致), 再用 span.score()
# 把分数挂到该 span 上 —— 真正的 observation 级, 不是 trace 级。
#
# 口径边界: 意图准确率(RT-01)/忠实度(GEN-FAITH-01)这类需要标注集或 judge 的指标
# **不在这条通道**, 仍走离线评测; 这里只报规则能判定的量, 以及需要重标定时
# 赖以判断分布的观测值(如 top_score 的漂移)。

_DERIVED_IDS: dict[str, str] = {}
_DERIVED_IDS_MAX = 1024


class StageEval:
    """一个环节的评测句柄: 只暴露 :meth:`score`, 未启用时是空实现。

    刻意不抛任何异常 —— 可观测通道的失败不能反过来影响对话链路, 与
    :func:`langfuse_callback` 的降级口径一致。
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any = None) -> None:
        self._span = span

    def score(
        self,
        name: str,
        value: float | str,
        *,
        data_type: str = "NUMERIC",
        comment: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """挂一条分数。``data_type`` 取 NUMERIC/BOOLEAN/CATEGORICAL。

        BOOLEAN 用 1.0/0.0 表达(与 Langfuse 的取值约定一致); CATEGORICAL 直接
        传字符串, 同一个 name 下的取值分布就是 EVAL_METRICS 里那句「占比」。
        """
        if self._span is None:
            return
        try:
            self._span.score(
                name=name,
                value=value,
                data_type=data_type,
                comment=comment or None,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 - 打分失败只留 debug 痕迹
            logger.debug("stage score failed (%s): %s", name, exc)


_NULL_EVAL = StageEval()


def derive_trace_id(audit_trace_id: str) -> str | None:
    """审计号 -> Langfuse trace id, 与 :func:`langfuse_callback` 同一 seed。

    进程内缓存: 一轮对话的多个环节共用一个 seed, 不缓存等于每个环节重复算一次
    sha256。派生失败返回 None (调用侧放弃打分, 不抛)。
    """
    if not audit_trace_id:
        return None
    cached = _DERIVED_IDS.get(audit_trace_id)
    if cached:
        return cached
    try:
        from langfuse import Langfuse

        derived = Langfuse.create_trace_id(seed=audit_trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("derived trace id unavailable: %s", exc)
        return None
    if len(_DERIVED_IDS) >= _DERIVED_IDS_MAX:
        # 简单容量控制: 清空而非 LRU —— 缓存只省一次哈希, 重建代价极低。
        _DERIVED_IDS.clear()
    _DERIVED_IDS[audit_trace_id] = derived
    return derived


@contextmanager
def stage_eval(stage: str, audit_trace_id: str):
    """环节评测上下文: 进入时建 ``eval.<stage>`` span, 退出时关闭。

    用法::

        with stage_eval("kb_retrieve", state.get("trace_id") or "") as ev:
            ...
            ev.score("RET-DEG-01.rerank_degraded", 1.0, data_type="BOOLEAN")

    未启用 Langfuse / 拿不到派生 trace id / 建 span 失败时产出空句柄,
    ``score()`` 是 no-op, 调用侧不需要写分支。

    前置条件: 本函数用 ``_langfuse_initialized`` 走热路径短路, 因此依赖图顶层
    调用点已经执行过 :func:`langfuse_callback` (它内部会 init)。图之外的调用
    (如评测脚本) 需自己先 ``init_langfuse()``。
    """
    if not _langfuse_initialized:
        # 没启用就别再碰 SDK: 这个判断取代 init_langfuse(), 避免热路径上每个
        # 环节都重跑一遍「配置 -> 环境变量」的搬运。
        yield _NULL_EVAL
        return
    trace_id = derive_trace_id(audit_trace_id)
    if not trace_id:
        yield _NULL_EVAL
        return
    cm = None
    try:
        from langfuse import get_client

        cm = get_client().start_as_current_observation(
            trace_context={"trace_id": trace_id},
            name=f"eval.{stage}",
            as_type="span",
        )
        span = cm.__enter__()
    except Exception as exc:  # noqa: BLE001 - 建 span 失败不影响业务
        logger.debug("stage eval span unavailable (%s): %s", stage, exc)
        yield _NULL_EVAL
        return
    try:
        yield StageEval(span)
    except BaseException:
        # 环节自己抛了: 把异常信息带进 span 的 level/status_message。否则 trace
        # 里会显示成一个「正常但很短」的 span, 排查时把人往错方向带。
        try:
            cm.__exit__(*sys.exc_info())
        except Exception as exc:  # noqa: BLE001
            logger.debug("stage eval span close failed (%s): %s", stage, exc)
        raise
    try:
        cm.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("stage eval span close failed (%s): %s", stage, exc)
