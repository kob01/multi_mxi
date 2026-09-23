"""Embedding utilities backed by local Ollama (bge-m3)."""

from collections.abc import Sequence

import httpx

from app.config import get_settings

# Ollama 0.32.x 对超大 input 数组会在内部 tokenize 阶段失败
# (400: Post "http://127.0.0.1:<runner>/tokenize": connection refused),
# 实测同一份 406 条文本一次提交必失败、按 64 条分片则全部成功;
# 分片同时避免单请求耗时撞上 120s 超时 (大文档入库动辄上千个 chunk)。
EMBED_BATCH_SIZE = 64


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

        Args:
            texts: Non-empty list of strings.

        Returns:
            A list of dense vectors, one per input text.
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=120.0) as client:
            for start in range(0, len(texts), EMBED_BATCH_SIZE):
                payload = {"model": self.model, "input": list(texts[start : start + EMBED_BATCH_SIZE])}
                resp = await client.post(f"{self.base_url}/api/embed", json=payload)
                resp.raise_for_status()
                vectors.extend(list(map(float, vec)) for vec in resp.json()["embeddings"])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""
        return (await self.embed([text]))[0]
