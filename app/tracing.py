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
