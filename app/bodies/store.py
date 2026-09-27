"""BodyStore: 正文外置存储的领域门面(整篇 raw/normalized/structure + 父块全文)。

降级约定(逐方法体落实, 见方案 1.2):
- ``mongo_enabled=false``: 读路径返回 "" / {} / 0, 写路径抛错(入库不能静默丢正文);
- 连接类异常(``PyMongoError`` 家族): 读路径返回空(调用方降级到子块文本),
  写路径向上抛, 由 ``docs/service.py`` 统一转成 502。

不变量: 只按 ``_id``/``doc_key``/``parent_id`` 精确取, 无内容/正则/聚合查询接口。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pymongo.errors import PyMongoError

from app.bodies.client import (
    COLL_DOC_BODY,
    COLL_DOC_BODY_PARTS,
    COLL_PARENT_TEXT,
    get_db,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

# 溢出分片的 part_no 编号基址: raw 分片从该基址起编号, 与 normalized 分片(从 0 起)
# 在 (doc_key, part_no) 唯一索引下互不冲突。
_RAW_PART_BASE = 1_000_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bsize(text: str) -> int:
    return len(text.encode("utf-8"))


def _split_text(text: str, cap_bytes: int) -> list[str]:
    """把超长文本按 UTF-8 字节上限切成片, 只在字符边界断, 不产生半个多字节字符。"""
    if not text:
        return []
    parts: list[str] = []
    buf: list[str] = []
    cur = 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if cur + b > cap_bytes and buf:
            parts.append("".join(buf))
            buf, cur = [ch], b
        else:
            buf.append(ch)
            cur += b
    if buf:
        parts.append("".join(buf))
    return parts


@dataclass
class ParentTextItem:
    """一个父块的全文与其定位信息(写入 Mongo parent_texts 的领域对象)。"""

    parent_id: str
    doc_id: str
    text: str
    anchor: dict = field(default_factory=dict)
    start_offset: int = -1
    end_offset: int = -1
    content_hash: str = ""


class BodyStore:
    """正文外置存储门面: 进程级单例, 构造零 I/O(同 PgVectorStore 约定)。"""

    def __init__(self) -> None:
        self._db = None

    def _coll(self, name: str):
        if self._db is None:
            self._db = get_db()
        return self._db[name]

    # ------------------------------------------------------------- 整篇正文
    async def save_doc_body(
        self,
        doc_key: str,
        *,
        raw: str,
        normalized: str,
        structure: list[dict],
        meta: dict | None = None,
    ) -> None:
        """写入整篇正文(不删旧, 由 upsert 覆盖); 超过阈值字段溢出到 doc_body_parts。

        写路径: mongo 关闭或连接异常一律向上抛, 让 ``service.py`` 决定 502 ——
        绝不能出现"PG 有元数据行但正文没落库"的中间态(不变量 7)。
        """
        if not get_settings().mongo_enabled:
            raise RuntimeError("mongo_enabled=false, 无法写入文档正文")
        from app.docs.normalize import NORMALIZER_VERSION, content_hash

        cap = get_settings().mongo_body_max_bytes
        # head 始终内联(建图只用开头, 常态无需拼分片)
        head = normalized[:8192]

        norm_parts = _split_text(normalized, cap) if _bsize(normalized) > cap else []
        raw_parts = _split_text(raw, cap) if _bsize(raw) > cap else []

        doc = {
            "_id": doc_key,
            "doc_key": doc_key,
            "raw_text": "" if raw_parts else raw,
            "normalized_text": "" if norm_parts else normalized,
            "head": head,
            "structure": structure,
            "normalizer_version": NORMALIZER_VERSION,
            "raw_hash": content_hash(raw),
            "norm_hash": content_hash(normalized),
            "char_count": len(normalized),
            "parts": len(norm_parts),
            "raw_parts": len(raw_parts),
            "meta": meta or {},
            "updated_at": _utcnow(),
        }
        await self._coll(COLL_DOC_BODY).replace_one(
            {"_id": doc_key}, doc, upsert=True
        )

        # 分片: 先按 doc_key 清旧, 再写新(重入库正文变短时避免残留脏分片)。
        await self._coll(COLL_DOC_BODY_PARTS).delete_many({"doc_key": doc_key})
        shard_docs: list[dict] = []
        for i, seg in enumerate(norm_parts):
            shard_docs.append(
                {
                    "_id": f"{doc_key}#norm#{i}",
                    "doc_key": doc_key,
                    "part_no": i,
                    "text": seg,
                    "updated_at": _utcnow(),
                }
            )
        for i, seg in enumerate(raw_parts):
            shard_docs.append(
                {
                    "_id": f"{doc_key}#raw#{_RAW_PART_BASE + i}",
                    "doc_key": doc_key,
                    "part_no": _RAW_PART_BASE + i,
                    "text": seg,
                    "updated_at": _utcnow(),
                }
            )
        if shard_docs:
            await self._coll(COLL_DOC_BODY_PARTS).insert_many(shard_docs, ordered=False)

    async def get_doc_body(
        self, doc_key: str, *, field: str = "normalized", head_only: bool = False
    ) -> str:
        """按 doc_key + 字段名精确取正文(读路径, 失败降级返回空串)。

        field: "normalized" | "raw" | "head"。head_only=True 直取内联 head, 不拼分片。
        """
        if field not in ("normalized", "raw", "head"):
            raise ValueError(f"unknown body field: {field}")
        if not get_settings().mongo_enabled:
            return ""
        try:
            doc = await self._coll(COLL_DOC_BODY).find_one({"_id": doc_key})
            if not doc:
                return ""
            if field == "head" or head_only:
                return doc.get("head", "") or ""
            if field == "normalized":
                inline = doc.get("normalized_text") or ""
                nparts = int(doc.get("parts") or 0)
                lo, hi = 0, nparts  # normalized 分片 part_no 从 0 连续
            else:
                inline = doc.get("raw_text") or ""
                nparts = int(doc.get("raw_parts") or 0)
                lo, hi = _RAW_PART_BASE, _RAW_PART_BASE + nparts
            if inline:
                return inline
            if not nparts:
                return ""
            # 溢出正文: 按 part_no 升序拼接分片(part_no 区间已按字段隔离)。
            cursor = (
                self._coll(COLL_DOC_BODY_PARTS)
                .find({"doc_key": doc_key, "part_no": {"$gte": lo, "$lt": hi}})
                .sort("part_no", 1)
                .batch_size(get_settings().mongo_batch_page_size)
            )
            return "".join(d["text"] async for d in cursor)
        except PyMongoError as exc:
            logger.warning("get_doc_body 读失败(降级为空) doc_key=%s: %s", doc_key, exc)
            return ""

    async def delete_doc_body(self, doc_key: str) -> None:
        """删除整篇正文及其分片(写路径, 由调用方在 PG 提交成功后执行, 异常吞为告警)。"""
        if not get_settings().mongo_enabled:
            return
        try:
            await self._coll(COLL_DOC_BODY).delete_one({"_id": doc_key})
            await self._coll(COLL_DOC_BODY_PARTS).delete_many({"doc_key": doc_key})
        except PyMongoError as exc:
            logger.warning("delete_doc_body 失败(残留由 --prune 回收) doc_key=%s: %s", doc_key, exc)

    # ------------------------------------------------------------- 父块全文
    async def save_parent_texts(self, items: Sequence[ParentTextItem]) -> int:
        """批量 upsert 父块全文(写路径, 失败向上抛)。"""
        if not items:
            return 0
        if not get_settings().mongo_enabled:
            raise RuntimeError("mongo_enabled=false, 无法写入父块正文")
        from pymongo import ReplaceOne

        page = get_settings().mongo_batch_page_size
        now = _utcnow()
        ops: list[ReplaceOne] = []
        for it in items:
            doc = {
                "_id": it.parent_id,
                "doc_id": it.doc_id,
                "text": it.text,
                "anchor": it.anchor,
                "start_offset": it.start_offset,
                "end_offset": it.end_offset,
                "content_hash": it.content_hash,
                "char_count": len(it.text),
                "updated_at": now,
            }
            ops.append(ReplaceOne({"_id": it.parent_id}, doc, upsert=True))
        written = 0
        coll = self._coll(COLL_PARENT_TEXT)
        for start in range(0, len(ops), page):
            res = await coll.bulk_write(ops[start : start + page], ordered=False)
            written += int(res.upserted_count + res.modified_count + res.inserted_count)
        return len(items)

    async def get_parent_texts(self, parent_ids: Sequence[str]) -> dict[str, str]:
        """热路径: 一次 $in 批量按 _id 精确取父块全文(读路径, 缺键不报错)。

        缺键(正文缺失/Mongo 抖动)只 WARNING, 由 ``assemble_parents`` 退回子块文本。
        """
        if not parent_ids:
            return {}
        if not get_settings().mongo_enabled:
            return {}
        ids = list(dict.fromkeys(parent_ids))  # 去重保序
        page = get_settings().mongo_batch_page_size
        out: dict[str, str] = {}
        try:
            coll = self._coll(COLL_PARENT_TEXT)
            for start in range(0, len(ids), page):
                batch = ids[start : start + page]
                async for doc in coll.find({"_id": {"$in": batch}}, {"text": 1}):
                    out[doc["_id"]] = doc.get("text", "")
        except PyMongoError as exc:
            logger.warning("get_parent_texts 读失败(降级): %s", exc)
            return out
        missing = len(ids) - len(out)
        if missing:
            logger.warning("parent text missing: %d ids", missing)
        return out

    async def delete_parents_by_doc(self, doc_id: str) -> int:
        """删除某文档的全部父块正文(重入库/删除文档; 写路径吞为告警)。"""
        if not get_settings().mongo_enabled:
            return 0
        try:
            res = await self._coll(COLL_PARENT_TEXT).delete_many({"doc_id": doc_id})
            return int(res.deleted_count or 0)
        except PyMongoError as exc:
            logger.warning("delete_parents_by_doc 失败 doc_id=%s: %s", doc_id, exc)
            return 0

    async def delete_stale_parents(self, doc_id: str, keep_ids: set[str]) -> int:
        """删除该 doc 下不在 keep_ids 中的陈旧父块正文(expand-then-contract 的收缩步)。"""
        if not get_settings().mongo_enabled:
            return 0
        try:
            res = await self._coll(COLL_PARENT_TEXT).delete_many(
                {"doc_id": doc_id, "_id": {"$nin": list(keep_ids)}}
            )
            return int(res.deleted_count or 0)
        except PyMongoError as exc:
            logger.warning("delete_stale_parents 失败 doc_id=%s: %s", doc_id, exc)
            return 0

    # --------------------------------------------------------------- 运维
    async def list_doc_keys(self) -> set[str]:
        if not get_settings().mongo_enabled:
            return set()
        try:
            return {k async for k in await self._coll(COLL_DOC_BODY).distinct("_id")}
        except PyMongoError as exc:
            logger.warning("list_doc_keys 失败: %s", exc)
            return set()

    async def count(self) -> dict[str, int]:
        if not get_settings().mongo_enabled:
            return {COLL_DOC_BODY: 0, COLL_DOC_BODY_PARTS: 0, COLL_PARENT_TEXT: 0}
        try:
            return {
                COLL_DOC_BODY: await self._coll(COLL_DOC_BODY).count_documents({}),
                COLL_DOC_BODY_PARTS: await self._coll(COLL_DOC_BODY_PARTS).count_documents({}),
                COLL_PARENT_TEXT: await self._coll(COLL_PARENT_TEXT).count_documents({}),
            }
        except PyMongoError as exc:
            logger.warning("count 失败: %s", exc)
            return {COLL_DOC_BODY: -1, COLL_DOC_BODY_PARTS: -1, COLL_PARENT_TEXT: -1}

    async def prune_orphans(
        self, live_doc_keys: set[str], live_parent_ids: set[str]
    ) -> dict[str, int]:
        """回收 Mongo 中 PG 不再引用的孤儿正文(PG 是事实来源, 残留在此清理)。

        分页扫描 _id 集合在 Python 侧比对(不用聚合/正则, 恪守不变量 3), 收集孤儿后
        按 _id $in 分批删。``live_doc_keys`` 为 documents.doc_key 集合,
        ``live_parent_ids`` 为 doc_parents.parent_id 集合。
        """
        removed = {COLL_DOC_BODY: 0, COLL_DOC_BODY_PARTS: 0, COLL_PARENT_TEXT: 0}
        if not get_settings().mongo_enabled:
            return removed
        page = get_settings().mongo_batch_page_size
        try:
            # 父块孤儿: parent_id 不在 live_parent_ids
            orphan_parents: list[str] = []
            async for doc in self._coll(COLL_PARENT_TEXT).find({}, {"_id": 1}):
                if doc["_id"] not in live_parent_ids:
                    orphan_parents.append(doc["_id"])
            for start in range(0, len(orphan_parents), page):
                res = await self._coll(COLL_PARENT_TEXT).delete_many(
                    {"_id": {"$in": orphan_parents[start : start + page]}}
                )
                removed[COLL_PARENT_TEXT] += int(res.deleted_count or 0)
            # 整篇正文孤儿: doc_key 不在 live_doc_keys
            orphan_bodies: list[str] = []
            async for doc in self._coll(COLL_DOC_BODY).find({}, {"_id": 1}):
                if doc["_id"] not in live_doc_keys:
                    orphan_bodies.append(doc["_id"])
            for start in range(0, len(orphan_bodies), page):
                keys = orphan_bodies[start : start + page]
                res = await self._coll(COLL_DOC_BODY).delete_many({"_id": {"$in": keys}})
                removed[COLL_DOC_BODY] += int(res.deleted_count or 0)
                pres = await self._coll(COLL_DOC_BODY_PARTS).delete_many(
                    {"doc_key": {"$in": keys}}
                )
                removed[COLL_DOC_BODY_PARTS] += int(pres.deleted_count or 0)
        except PyMongoError as exc:
            logger.warning("prune_orphans 失败(已删 %s): %s", removed, exc)
        return removed


_body_store: BodyStore | None = None


def get_body_store() -> BodyStore:
    """进程级单例(同 get_es_bm25() 风格)。"""
    global _body_store
    if _body_store is None:
        _body_store = BodyStore()
    return _body_store
