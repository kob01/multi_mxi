"""Chat model factory.

Routes by the provider registry (``settings.resolve_llm_provider``): model names
hitting a registered prefix (``deepseek*`` / ``glm*`` / ...) go to that vendor's
OpenAI-compatible endpoint, everything else falls back to local Ollama.
Callers only ask for a chat model by name/temperature and never care about
the transport. Adding a new online vendor = one registry entry in config
(``LLM_PROVIDERS_JSON``) + its base_url/secret fields, no code change here.

深度思考 (thinking):
    在线侧经 ``extra_body`` 控制, 参数形状由各供应商注册条目的
    ``thinking_template``(enabled/disabled 两形态)提供, {effort} 占位符
    替换为思考强度; 思考内容在响应消息的 ``reasoning_content`` 字段里, 由
    ``extract_reasoning`` 统一透出(OpenAI 兼容层与 Ollama 字段名不同)。
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk

from app.config import get_settings


def is_deepseek(model: str) -> bool:
    """Whether the given model name should be served by the DeepSeek API."""
    return model.lower().startswith("deepseek")


def _load_model_class(dotted: str):
    """按 "包:类名" 延迟加载 langchain 集成类(避免启动期硬依赖全部包)。"""
    pkg, _, cls = dotted.partition(":")
    if not cls:
        pkg, _, cls = "langchain_openai", "ChatOpenAI"
    return getattr(importlib.import_module(pkg), cls)


def _render_extra_body(thinking: bool, template: str, effort: str) -> dict[str, Any]:
    """由注册表的 thinking_template 渲染思考开关 extra_body。

    模板是 {"enabled": {...}, "disabled": {...}} 两形态的 JSON, 形态内的
    {effort} 占位符替换为思考强度; 置空/非法/缺对应形态时不传 extra_body
    (降级为服务端默认行为, 不报错), 模板写错不致于打断整条对话链路。
    """
    if not template:
        return {}
    try:
        forms = json.loads(template)
    except ValueError:
        return {}
    if not isinstance(forms, dict):
        return {}
    body = forms.get("enabled" if thinking else "disabled")
    if not isinstance(body, dict):
        return {}
    rendered = {
        k: (v.replace("{effort}", effort) if isinstance(v, str) else v)
        for k, v in body.items()
    }
    # effort 未配置(置空)时不传空串参数, 避免被供应商拒参
    return {k: v for k, v in rendered.items() if v != ""}


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

    provider = settings.resolve_llm_provider(name)
    if provider is not None:
        prefix, cfg = provider
        base_url = getattr(settings, cfg.get("base_url_field", ""), "") if cfg.get("base_url_field") else ""
        api_key = getattr(settings, cfg.get("api_key_field", ""), "") if cfg.get("api_key_field") else ""
        if not base_url or not api_key:
            raise RuntimeError(
                f"缺少在线 LLM 供应商配置(前缀 {prefix!r}): "
                f"base_url/密钥需经 settings 字段({cfg.get('base_url_field')}/"
                f"{cfg.get('api_key_field')})提供, 密钥只住 docker/secrets/"
            )
        # deepseek 分支用官方集成 ChatDeepSeek 而非通用 ChatOpenAI:
        # langchain-openai v1 不提取非标准字段 reasoning_content(思考内容),
        # ChatDeepSeek 会透传到 additional_kwargs, 流式 chunk 为逐段增量。
        model_cls = _load_model_class(cfg.get("model_class", "langchain_deepseek:ChatDeepSeek"))
        kwargs: dict = {
            "model": name,
            "base_url": base_url,
            "api_key": api_key,
            "temperature": temperature,
            # 墙钟上限 + 有界重试(两者都必须显式给, 理由见 Settings.llm_request_timeout):
            # 没有 timeout 时一个挂住的供应商会永久占住一个并发闸门与一路 HTTP 连接,
            # 而默认重试次数乘上人数就是配额翻倍(429 时只会把故障放大)。
            "timeout": settings.llm_request_timeout,
            "max_retries": max(0, int(settings.llm_max_retries)),
        }
        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
            # 意图分类等结构化短任务关闭思考模式, 降低时延 (deepseek-flash 默认开启)
            kwargs["extra_body"] = _render_extra_body(False, cfg.get("thinking_template", ""), "")
        else:
            # 非 json 通道显式声明思考开关(开/关), 不依赖服务端默认值
            kwargs["extra_body"] = _render_extra_body(
                want_thinking, cfg.get("thinking_template", ""), settings.llm_reasoning_effort
            )
        if not kwargs["extra_body"]:
            kwargs.pop("extra_body")
        return model_cls(**kwargs)

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
