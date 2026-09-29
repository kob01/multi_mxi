"""Embedding utilities backed by local Ollama (bge-m3).

进程级共享一个 ``httpx.AsyncClient``: 查询向量在对话热路径上(每轮至少一次, 意图
语义层 + 检索各一次), 每次新建客户端会堆 TIME_WAIT 并把握手开销乘上并发数 ——
与 ``app/rag/reranker.py`` / ``app/tools/_http.py`` 是同一条结论。
上限按"Ollama 单进程串行推理"给: 连接开得再多也只是把排队从本地池搬到下游服务,
反而更容易全线超时。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Ollama 0.32.x 对超大 input 数组会在内部 tokenize 阶段失败
# (400: Post "http://127.0.0.1:<runner>/tokenize": connection refused),
# 实测同一份 406 条文本一次提交必失败、按 64 条分片则全部成功;
# 分片同时避免单请求耗时撞上超时 (大文档入库动辄上千个 chunk)。
EMBED_BATCH_SIZE = 64

_client: httpx.AsyncClient | None = None
# 查询向量的本地闸门: 实测 30 并发直打 Ollama 会有一批请求在其内部 tokenize 阶段
# 失败返 400(不是超时), 所以并发必须在网关这边卡住, 而不是把排队搬到下游。
_query_gate: asyncio.Semaphore | None = None
_query_gate_limit = 0


def _get_query_gate() -> asyncio.Semaphore:
    global _query_gate, _query_gate_limit
    limit = max(1, int(get_settings().embedding_query_concurrency))
    if _query_gate is None or _query_gate_limit != limit:
        _query_gate = asyncio.Semaphore(limit)
        _query_gate_limit = limit
    return _query_gate


def get_embedder_client() -> httpx.AsyncClient:
    """Lazily build the process-wide Ollama client (bounded pool, request-level timeout).

    客户端本身不设总超时: 查询向量与批量入库的合理超时差一个数量级, 由调用点按
    请求传 ``timeout``; 建连超时统一收紧(服务没起来时要快速失败而不是挂着)。
    """
    global _client
    if _client is None or _client.is_closed:
        s = get_settings()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(s.embedding_timeout, connect=5.0),
            limits=httpx.Limits(
                max_connections=max(1, s.embedding_max_connections),
                max_keepalive_connections=max(1, s.embedding_max_connections // 2),
            ),
        )
    return _client


async def close_embedder_client() -> None:
    """释放连接池(供 lifespan 关闭时调用, 与 close_reranker_client 同风格)。"""
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("closing embedding client failed: %s", exc)
    _client = None


class OllamaEmbedder:
    """Thin async client for Ollama's /api/embed endpoint.

    bge-m3 produces 1024-dim dense vectors and natively supports
    multilingual + long-context (8k) inputs, which fits enterprise
    Chinese/English mixed documents.
    """

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.embedding_model
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts (split into bounded sub-batches).

        分片串行发送而不是并发: Ollama 内部本来就串行推理, 并发只会把压力推到
        下游(表现为整体超时), 而入库路径本来就是后台任务, 不在乎多几轮往返。

        Args:
            texts: Non-empty list of strings.

        Returns:
            A list of dense vectors, one per input text.
        """
        if not texts:
            return []
        timeout = float(get_settings().embedding_timeout)
        vectors: list[list[float]] = []
        client = get_embedder_client()
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            payload = {"model": self.model, "input": list(texts[start : start + EMBED_BATCH_SIZE])}
            resp = await client.post(f"{self.base_url}/api/embed", json=payload, timeout=timeout)
            resp.raise_for_status()
            vectors.extend(list(map(float, vec)) for vec in resp.json()["embeddings"])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query string (对话热路径)。

        两道保护都是为并发而加, 不是为单次调用:
        - 本地闸门(``embedding_query_concurrency``): Ollama 在多个并发请求下会在其
          内部 tokenize 阶段失败返 400(实测 30 并发必现), 之后整批查询都拿不到向量
          —— 意图语义层集体下沉 LLM、检索稠密通道集体为空, 看着像"降级正常"实则质变;
        - 拿不到就重试一次(400/超时): 推理服务在两个请求之间的瞬时状态不同, 实测
          重试成功率很高; 再失败才往上抛, 由调用方逐层降级。

        查询档超时比批量档短得多: 拿不到向量时调用方会降级, 快速失败比让整轮对话
        挂在 Ollama 的排队上更好(闸门内的排队时间不计入单次超时, 所以可等)。
        """
        timeout = float(get_settings().embedding_query_timeout)
        gate = _get_query_gate()
        async with gate:
            last_exc: Exception | None = None
            for attempt in (1, 2):
                try:
                    vectors = await self._embed_one(text, timeout)
                    if vectors:
                        return vectors[0]
                    raise RuntimeError("embedding 返回空向量")
                except Exception as exc:  # noqa: BLE001 - 重试一次再交给调用方降级
                    last_exc = exc
                    if attempt == 1:
                        await asyncio.sleep(0.2)
            logger.warning("查询向量两次均失败(本轮检索/意图语义层将降级): %s", str(last_exc)[:160])
            assert last_exc is not None
            raise last_exc

    async def _embed_one(self, text: str, timeout: float) -> list[list[float]]:
        payload = {"model": self.model, "input": [text]}
        resp = await get_embedder_client().post(
            f"{self.base_url}/api/embed", json=payload, timeout=timeout
        )
        resp.raise_for_status()
        return [list(map(float, vec)) for vec in resp.json()["embeddings"]]
