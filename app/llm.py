"""Chat model factory.

Routes to the DeepSeek online API (OpenAI-compatible) for model names
starting with ``deepseek``, and to local Ollama otherwise. Callers only ask
for a chat model by name/temperature and never care about the transport.

深度思考 (thinking):
    DeepSeek 侧经 ``extra_body={"thinking": {...}, "reasoning_effort": ...}``
    控制; 思考内容在响应消息的 ``reasoning_content`` 字段里, 由
    ``extract_reasoning`` 统一透出(OpenAI 兼容层与 Ollama 字段名不同)。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk

from app.config import get_settings


def is_deepseek(model: str) -> bool:
    """Whether the given model name should be served by the DeepSeek API."""
    return model.lower().startswith("deepseek")


def _deepseek_extra_body(thinking: bool, settings) -> dict[str, Any]:
    """DeepSeek 思考开关的 extra_body (关闭时不传 effort, 避免参数被拒)。"""
    if not thinking:
        return {"thinking": {"type": "disabled"}}
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": settings.llm_reasoning_effort,
    }


def get_chat_model(
    model: str | None = None,
    *,
    temperature: float = 0.1,
    json_mode: bool = False,
    thinking: bool | None = None,
) -> BaseChatModel:
    """Build a chat model for ``model`` (defaults to ``settings.llm_model``).

    ``json_mode`` requests JSON-structured output (intent classification).
    ``thinking`` 为 None 时取全局默认 ``llm_thinking_enabled``; json_mode
    始终强制关闭思考(结构化短任务降时延)。
    """
    settings = get_settings()
    name = model or settings.llm_model
    want_thinking = settings.llm_thinking_enabled if thinking is None else thinking

    if is_deepseek(name):
        # 用官方 DeepSeek 集成而非通用 ChatOpenAI: langchain-openai v1 不提取
        # 非标准字段 reasoning_content(思考内容), ChatDeepSeek 会透传到
        # additional_kwargs, 流式 chunk 为逐段增量。
        from langchain_deepseek import ChatDeepSeek

        if not settings.deepseek_api_key:
            raise RuntimeError(
                "缺少 DeepSeek API 密钥"
            )
        kwargs: dict = {
            "model": name,
            "base_url": settings.deepseek_base_url,
            "api_key": settings.deepseek_api_key,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
            # 意图分类等结构化短任务关闭思考模式, 降低时延 (deepseek-flash 默认开启)
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            # 非 json 通道显式声明思考开关(开/关), 不依赖服务端默认值
            kwargs["extra_body"] = _deepseek_extra_body(want_thinking, settings)
        return ChatDeepSeek(**kwargs)

    from langchain_ollama import ChatOllama

    ollama_kwargs: dict = {
        "model": name,
        "base_url": settings.ollama_base_url,
        "temperature": temperature,
    }
    if json_mode:
        ollama_kwargs["format"] = "json"
    elif want_thinking:
        # 新版 ChatOllama 的 think 参数开启思考档(不支持的模型会报错, 由调用侧降级)
        ollama_kwargs["think"] = "low"
    return ChatOllama(**ollama_kwargs)


def get_streaming_chat_model(
    model: str | None = None,
    *,
    temperature: float = 0.1,
    thinking: bool = True,
) -> BaseChatModel:
    """逐 token 流式生成用的模型实例(按 thinking 变体缓存)。

    thinking 是构造期参数, 必须按本轮请求的思考开关取对应实例,
    否则关闭思考的请求也会从服务端 extra_body 默认值里拿到 reasoning。
    """
    key = (model or "", temperature, thinking)
    cached = _stream_models.get(key)
    if cached is None:
        cached = get_chat_model(model, temperature=temperature, thinking=thinking)
        _stream_models[key] = cached
    return cached


_stream_models: dict[tuple, BaseChatModel] = {}


def extract_reasoning(msg: AIMessage | AIMessageChunk) -> str:
    """从响应消息中提取思考内容, 取不到返回空串(优雅降级)。

    ChatDeepSeek 透传 ``additional_kwargs["reasoning_content"]``(流式逐段增量);
    ChatOllama 的思考内容在 ``additional_kwargs["thinking"]``。
    流式 chunk 与聚合消息同名字段, 逐段拼接即可得到完整思考。
    """
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "thinking"):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
