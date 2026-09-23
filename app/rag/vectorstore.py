"""Milvus Lite vector store wrapper.

Milvus Lite runs embedded (single file), which keeps local/dev deployment
trivial while the same client code works against Milvus Server in K8s by
simply switching MILVUS_LITE_URI.

Schema carries parent-child chunking fields (parent_id / is_parent /
page_no / section). Retrieval always filters `is_parent == 0` so only child
chunks compete; parents are fetched by id afterwards for context assembly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException

from app.config import get_settings
from app.schemas import KnowledgeChunk

logger = logging.getLogger(__name__)

DENSE_DIM = 1024  # bge-m3 dense dimension

# pymilvus 默认 grpc.keepalive_time_ms=10000 且 PermitWithoutCalls=True, 即空闲
# 连接也每 10s 发一次 ping; 服务端(Milvus / Milvus Lite)的 keepalive 强制策略
# 视其为 ping 过频, 累计 3 次后直接回 GOAWAY(ENHANCE_YOUR_CALM,
# "too_many_pings") 掐断 channel, 并打出
# "Current keepalive time (before throttling): 10000ms"。
# 连接空闲约 30s 就会被掐断; pymilvus 2.6 的自动恢复(_recover)对同一坏连接反复
# 重连失败, 于是进程内后续所有 MilvusClient 都报
# "Fail connecting to server on 127.0.0.1:xxxxx, illegal connection params or
# server unavailable", 只能重启进程 —— 大文件入库(上传 + LLM 打标签 +
# embedding 期间长时间没有 Milvus RPC)必然踩到, 小文件则因间隔短而侥幸通过。
# Milvus Lite 跑在本机回环上, keepalive 没有意义: 关闭空闲 ping 并拉长间隔。
GRPC_OPTIONS = {
    "grpc.keepalive_time_ms": 120000,
    "grpc.keepalive_timeout_ms": 20000,
    "grpc.keepalive_permit_without_calls": 0,
}

OUTPUT_FIELDS = [
    "chunk_id", "doc_id", "title", "content", "source", "modality",
    "parent_id", "is_parent", "page_no", "section",
    "visibility", "owner_id", "dept_id", "allowed_roles",
]


def _reset_connection_manager() -> None:
    """Drop pymilvus's process-wide connection registry (best effort).

    Imported lazily: the manager only exists in newer pymilvus layouts, and an
    older one simply has nothing to reset (it reconnects on its own).
    """
    try:
        from pymilvus.client.connection_manager import ConnectionManager
    except ImportError:  # pragma: no cover - legacy pymilvus
        return
    ConnectionManager.get_instance().close_all()


class MilvusStore:
    """Vector persistence + ANN search over enterprise knowledge chunks."""

    def __init__(self, uri: str | None = None, collection: str | None = None) -> None:
        settings = get_settings()
        self.uri = self._resolve_uri(uri or settings.milvus_lite_uri)
        self.collection_name = collection or settings.milvus_collection
        self._lock = threading.Lock()
        self._client: MilvusClient | None = None
        self.client = self._connect()
        self._ensure_collection()

    def _connect(self) -> MilvusClient:
        """Open the Milvus client, self-healing a poisoned shared connection.

        pymilvus 2.6 keeps connections in a process-wide ConnectionManager
        singleton keyed by address. If that entry's channel dies (e.g. the
        server-side keepalive GOAWAY above), ``_recover()`` keeps reconnecting
        the same broken handler and every later ``MilvusClient`` in the process
        fails with "Fail connecting to server ... server unavailable" until the
        process restarts. Dropping the registry makes the next attempt build a
        fresh channel instead.
        """
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                self._client = MilvusClient(uri=self.uri, grpc_options=GRPC_OPTIONS)
            except MilvusException:
                logger.warning("milvus connect failed, rebuilding shared connection", exc_info=True)
                _reset_connection_manager()
                self._client = MilvusClient(uri=self.uri, grpc_options=GRPC_OPTIONS)
        return self._client

    @staticmethod
    def _resolve_uri(uri: str) -> str:
        """Anchor relative file URIs to the project root and mkdir parents.

        Milvus Lite auto-creates the db file on first use, but the parent
        directory must exist, and a relative path would otherwise resolve
        against the process CWD instead of the project root.
        """
        if uri.startswith(("http://", "https://")):
            return uri
        path = Path(uri)
        if not path.is_absolute():
            path = get_settings().base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _ensure_collection(self) -> None:
        """Create collection + index on first use; always load it into memory.

        Milvus Lite collections stay 'released' across processes, so every
        process opening the db file must explicitly load() before search.
        A legacy collection without the parent-child fields is dropped and
        recreated (documents must be re-ingested).
        """
        if self.client.has_collection(self.collection_name):
            fields = {f["name"] for f in self.client.describe_collection(self.collection_name)["fields"]}
            if "is_parent" not in fields or "visibility" not in fields:
                logger.warning(
                    "collection %s uses the legacy schema (missing %s), dropping and "
                    "recreating; documents must be re-ingested",
                    self.collection_name,
                    "is_parent/visibility" if "is_parent" not in fields else "visibility",
                )
                self.client.drop_collection(self.collection_name)
            else:
                self.client.load_collection(self.collection_name)
                return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=80)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("content", DataType.VARCHAR, max_length=8192)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("modality", DataType.VARCHAR, max_length=32)
        schema.add_field("parent_id", DataType.VARCHAR, max_length=80)
        schema.add_field("is_parent", DataType.INT64)  # 1 = parent block, 0 = child chunk
        schema.add_field("page_no", DataType.INT64)    # -1 = unknown
        schema.add_field("section", DataType.VARCHAR, max_length=256)
        # 文档级 ACL: 冗余在每个 chunk 上, 检索时由 Metadata Filter 前置裁剪
        schema.add_field("visibility", DataType.VARCHAR, max_length=16)   # public/dept/role/private
        schema.add_field("owner_id", DataType.VARCHAR, max_length=64)
        schema.add_field("dept_id", DataType.VARCHAR, max_length=64)
        schema.add_field("allowed_roles", DataType.VARCHAR, max_length=128)  # ",hr,admin,"
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=DENSE_DIM)
        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(
            collection_name=self.collection_name, schema=schema, index_params=index_params
        )
        self.client.load_collection(self.collection_name)

    @staticmethod
    def _row(c: KnowledgeChunk, v: Sequence[float]) -> dict[str, Any]:
        return {
            "chunk_id": c.chunk_id,
            "doc_id": c.doc_id,
            "title": c.title[:512],
            "content": c.content[:8192],
            "source": c.source[:512],
            "modality": c.modality,
            "parent_id": c.parent_id,
            "is_parent": 1 if c.is_parent else 0,
            "page_no": c.page_no,
            "section": c.section[:256],
            "visibility": c.visibility[:16],
            "owner_id": c.owner_id[:64],
            "dept_id": c.dept_id[:64],
            "allowed_roles": c.allowed_roles[:128],
            "embedding": list(v),
        }

    @staticmethod
    def _to_chunk(entity: dict[str, Any], score: float = 0.0) -> KnowledgeChunk:
        return KnowledgeChunk(
            chunk_id=entity["chunk_id"],
            doc_id=entity["doc_id"],
            title=entity["title"],
            content=entity["content"],
            source=entity["source"],
            modality=entity.get("modality", "text"),
            parent_id=entity.get("parent_id", ""),
            is_parent=bool(entity.get("is_parent", 0)),
            page_no=int(entity.get("page_no", -1)),
            section=entity.get("section", ""),
            visibility=entity.get("visibility") or "public",
            owner_id=entity.get("owner_id", ""),
            dept_id=entity.get("dept_id", ""),
            allowed_roles=entity.get("allowed_roles", ""),
            score=score,
        )

    def upsert(self, chunks: Sequence[KnowledgeChunk], vectors: Sequence[Sequence[float]]) -> int:
        """Insert or update chunks with their dense vectors."""
        assert len(chunks) == len(vectors), "chunks/vectors length mismatch"
        rows = [self._row(c, v) for c, v in zip(chunks, vectors)]
        self.client.upsert(collection_name=self.collection_name, data=rows)
        return len(rows)

    def search(self, query_vector: Sequence[float], top_k: int, acl_filter: str = "") -> list[KnowledgeChunk]:
        """ANN cosine search over child chunks; returns chunks with score.

        ``acl_filter`` is a Milvus scalar-expression built from the caller's
        Principal (see app.security.acl); it pre-trims unauthorized documents
        *before* TopK so they never compete for retrieval slots.
        """
        expr = "is_parent == 0"
        if acl_filter:
            expr = f"({expr}) and ({acl_filter})"
        results = self.client.search(
            collection_name=self.collection_name,
            data=[list(query_vector)],
            limit=top_k,
            filter=expr,
            output_fields=OUTPUT_FIELDS,
        )
        return [self._to_chunk(hit["entity"], float(hit["distance"])) for hit in results[0]]

    def query_parents(self, parent_ids: Sequence[str]) -> dict[str, KnowledgeChunk]:
        """Fetch parent blocks by chunk_id (for context assembly)."""
        if not parent_ids:
            return {}
        quoted = ", ".join(f'"{pid}"' for pid in parent_ids)
        rows = self.client.query(
            collection_name=self.collection_name,
            filter=f"chunk_id in [{quoted}]",
            output_fields=OUTPUT_FIELDS,
            limit=len(parent_ids),
        )
        return {r["chunk_id"]: self._to_chunk(r) for r in rows}

    def iter_child_chunks(self, limit: int = 16384) -> list[KnowledgeChunk]:
        """Return all child chunks (BM25 corpus rebuild)."""
        rows = self.client.query(
            collection_name=self.collection_name,
            filter="is_parent == 0",
            output_fields=OUTPUT_FIELDS,
            limit=limit,
        )
        return [self._to_chunk(r) for r in rows]

    def delete_by_doc(self, doc_id: str) -> None:
        """Remove all chunks of a document (used for overwrite re-ingest)."""
        self.client.delete(collection_name=self.collection_name, filter=f'doc_id == "{doc_id}"')

    def update_acl_by_doc(
        self, doc_id: str, visibility: str, owner_id: str, dept_id: str, allowed_roles: str
    ) -> int:
        """Rewrite the ACL metadata on every chunk row of a document.

        Milvus has no partial scalar update: we read the rows back (with their
        vectors), patch the four ACL fields, and upsert. Vectors are unchanged,
        so the ANN index stays consistent with MySQL as the source of truth.
        """
        rows = self.client.query(
            collection_name=self.collection_name,
            filter=f'doc_id == "{doc_id}"',
            output_fields=OUTPUT_FIELDS + ["embedding"],
            limit=16384,
        )
        if not rows:
            return 0
        for r in rows:
            r["visibility"] = visibility[:16]
            r["owner_id"] = owner_id[:64]
            r["dept_id"] = dept_id[:64]
            r["allowed_roles"] = allowed_roles[:128]
        self.client.upsert(collection_name=self.collection_name, data=rows)
        return len(rows)

    def count(self) -> int:
        """Return number of stored chunks (parents + children)."""
        stats = self.client.get_collection_stats(self.collection_name)
        return int(stats.get("row_count", 0))
