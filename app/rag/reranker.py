"""Reranker backed by TEI (Text Embeddings Inference) `/rerank`.

真 cross-encoder: ``BAAI/bge-reranker-v2-m3`` 的序列分类头直接给 (query, text) 打分,
输出 sigmoid 归一后的 0~1 相关性 —— 取代原先借道 Ollama ``/api/embed`` 的伪 rerank
(那个 GGUF 在 Windows llama.cpp 上调用即崩, 每条查询都撞超时后退化成 RRF 融合序)。

一次查询只发一个批量请求(全部候选一起送), 且全进程复用一个 ``AsyncClient`` 连接池:
rerank 在对话热路径上, 每次新建客户端会堆 TIME_WAIT, 而 1+N 串行调用会把延迟放大
一个数量级。TEI 半死不能拖垮对话, 故超时压到秒级, 抛错交由 HybridRetriever 降级 RRF。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from app.config import get_settings
from app.schemas import KnowledgeChunk

logger = logging.getLogger(__name__)

# 单请求候选上限: TEI 默认 `--max-client-batch-size 32`, 超出整批直接 400。
# 生产 rag_top_k=8 远未触顶, 但评测可将 top_k 拉到 50, 故在此硬截断。
MAX_CANDIDATES = 32

# 进程级共享连接池: TEI 是常驻服务, 复用 keep-alive 连接才有意义。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazily build the process-wide TEI client (bounded pool + second-level timeout)."""
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            base_url=settings.tei_rerank_url.rstrip("/"),
            timeout=httpx.Timeout(
                settings.rerank_timeout, connect=settings.rerank_connect_timeout
            ),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
    return _client


async def close_reranker_client() -> None:
    """Close the shared client on shutdown (idempotent; mirrors close_redis/close_mongo)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _parse_scores(payload: object, n: int) -> list[float]:
    """TEI 打分结果 -> 按请求 texts 顺序对齐的分数列表。

    TEI 返回**裸数组** ``[{index, score, text?}]``(1.x)/``[{id, score}]``(老版),
    且数组是按分数降序的, ``index`` 才对应请求里的文本位置; Cohere 风格服务则把
    结果包在 ``{"results": [{index, relevance_score}]}`` 里。三种形态统一在此收敛,
    缺分数的位置回落 0.0(排到最后), 绝不把顺序错位当成打分结果。
    """
    items = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise RuntimeError(f"unexpected rerank payload: {type(payload).__name__}")
    scores = [0.0] * n
    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        idx = item.get("index", item.get("id", pos))
        raw = item.get("score", item.get("relevance_score", 0.0))
        if isinstance(idx, int) and 0 <= idx < n:
            scores[idx] = float(raw)
    return scores


class TeiReranker:
    """Score (query, chunk) relevance with a real cross-encoder served by TEI."""

    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.tei_rerank_url).rstrip("/")
        self.max_chars = settings.rerank_max_chars

    @staticmethod
    def _doc_text(chunk: KnowledgeChunk, max_chars: int) -> str:
        """打分文本 = 标题 + 正文(标题给领域锚点, 正文给证据), 按字符截断控 batch 规模。"""
        return f"{chunk.title}\n{chunk.content}"[:max_chars]

    async def rerank(
        self, query: str, chunks: Sequence[KnowledgeChunk], top_n: int
    ) -> list[KnowledgeChunk]:
        """Rerank candidate chunks; returns top_n with the 0~1 relevance score.

        Raises on any transport/service error — ``HybridRetriever`` 负责捕获并降级到
        RRF 融合序, 这里不做静默兜底, 否则真故障会被掩盖成"排序质量变差"。
        """
        if not chunks:
            return []

        scored = list(chunks[:MAX_CANDIDATES])
        texts = [self._doc_text(c, self.max_chars) for c in scored]
        client = _get_client()
        resp = await client.post(
            "/rerank",
            json={
                "query": query,
                "texts": texts,
                # raw_scores=false 才拿 sigmoid 后的 0~1; retrieval_score_threshold 按此标度设定。
                "raw_scores": False,
                "return_text": False,
                # 必传: 不截断则超长候选直接让 TEI 报错(整批打分失败 -> 白降级)。
                "truncate": True,
            },
        )
        if not resp.is_success:
            # 424=模型不是单分类序列分类头, 429=过载, 5xx=后端; 带响应体片段便于定位。
            raise RuntimeError(f"TEI /rerank {resp.status_code}: {resp.text[:200]}")
        scores = _parse_scores(resp.json(), len(scored))

        ranked = [
            chunk.model_copy(update={"score": score})
            for chunk, score in zip(scored, scores)
        ]
        ranked.sort(key=lambda c: c.score, reverse=True)
        # 未参与打分的溢出候选(超 MAX_CANDIDATES)保持原 RRF 相对序附在尾部, 不静默丢。
        ranked.extend(chunks[MAX_CANDIDATES:])
        return ranked[:top_n]

    async def health(self) -> bool:
        """TEI 就绪探测(模型加载完才返 2xx)。永不抛异常。"""
        try:
            resp = await _get_client().get("/health")
            return resp.is_success
        except Exception as exc:  # noqa: BLE001 - 探测失败即不可用
            logger.debug("TEI /health probe failed: %s", exc)
            return False

    async def probe(self) -> bool:
        """真打一次 /rerank, 校验明显相关的文本确实排在无关文本之前。

        比 ``health()`` 更强: 覆盖"服务活着但模型不对(424)/分数全 0"这类故障。永不抛异常。
        """
        try:
            scored = await self.rerank(
                "报销单怎么提交",
                [
                    KnowledgeChunk(
                        chunk_id="probe-relevant", doc_id="probe", title="费用报销",
                        source="probe", content="员工可在费控系统中提交报销单并上传发票。",
                    ),
                    KnowledgeChunk(
                        chunk_id="probe-noise", doc_id="probe", title="技术滑行教程",
                        source="probe", content="今天天气不错, 适合去雪场练平行转弯。",
                    ),
                ],
                top_n=2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("TEI rerank probe unavailable (%s): %s", self.base_url, str(exc)[:160])
            return False
        return bool(scored) and scored[0].chunk_id == "probe-relevant"


_reranker: TeiReranker | None = None


def get_reranker() -> TeiReranker:
    """Process-wide singleton of the rerank stage (shares the module-level client)."""
    global _reranker
    if _reranker is None:
        _reranker = TeiReranker()
    return _reranker
