"""解析产物存储(artifact): 一次解析, 预览与入库共用同一份产物。

为什么必须有这一层: 旧链路是 ``upload -> parse_blocks(预览) -> ingest -> parse_blocks(再解析)``,
同一个文件被解析两次。两次解析之间 OCR 服务状态、图片识别结果、库版本都可能在动,
于是"页面上看到的内容"与"真正入库的内容"不是同一份产物; 而扫描 PDF 的逐页 OCR 是分钟级
重活, 做两遍纯浪费。artifact 把解析结果固化成一份可命名(``upload_id``)、可校验
(``content_sha256`` + ``parser_version``)、可过期的产物, 预览读它、入库也读它。

正文只存一份: ``blocks`` 里放的是**每个块在 raw_text 中的偏移**, 读回时按偏移切 raw 还原
``ParsedBlock``。不这么做的代价是每个块的文本会再抄一遍(几十万字文档就是几十万字冗余),
而"正文唯一副本"本来就是本项目的既定约束。

偏移基准是 ``raw``(块文本用两个换行拼接出来的整串), 与 ``normalized_text`` 上的
``doc_parents.start_offset/end_offset`` 是两套坐标, 互不替代: 后者由入库侧的 ``_Locator``
在 normalized 上重新回算, 不能拿 artifact 的偏移直接写进 PG。

降级约定与 :class:`app.bodies.store.BodyStore` 一致: ``mongo_enabled=false`` 时读路径返回
``None``/空, 写路径抛错(入库不能静默丢产物)。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from pymongo.errors import PyMongoError

from app.bodies.client import COLL_PARSE_ARTIFACT, COLL_PARSE_ARTIFACT_PARTS, get_db
from app.bodies.store import byte_size, split_text
from app.config import get_settings
from app.docs.parsers import ParsedBlock

logger = logging.getLogger(__name__)

# 预览取 raw 开头这么多字(与旧 /upload 响应的 preview 口径一致, 前端不改可读长度)。
PREVIEW_CHARS = 500
# 建图/标签提示用的更长短文; artifact 不内联整篇超长正文, 靠分片承载。
HEAD_CHARS = 2000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def join_raw(blocks: Sequence[ParsedBlock]) -> str:
    """把块拼成整篇 raw(与 ``app/rag/ingest.py`` 历史上 ``"\\n\\n".join`` 完全同口径)。"""
    return "\n\n".join(b.text for b in blocks)


def block_offsets(blocks: Sequence[ParsedBlock], raw: str) -> list[dict[str, Any]]:
    """按拼接顺序算出每个块在 ``raw`` 中的 ``[start, end)``。

    ``raw`` 由本模块自己拼出来, 所以逐块 ``startswith`` 必然命中; 万一不命中(调用方
    传了与 raw 不匹配的 blocks), 必须立刻抛出而不是写进 -1 偏移 —— 偏移错一位, 入库
    还原出来的块就会串台, 而且事后从产物里看不出来。
    """
    out: list[dict[str, Any]] = []
    cursor = 0
    for seq, block in enumerate(blocks):
        text = block.text
        if not raw.startswith(text, cursor):
            raise ValueError(f"block {seq} 与 raw_text 不匹配(偏移无法对齐)")
        start = cursor
        end = start + len(text)
        out.append(
            {
                "seq": seq,
                "section": block.section or "",
                "page_no": int(block.page_no),
                "parent_type": getattr(block, "parent_type", "section") or "section",
                "start": start,
                "end": end,
            }
        )
        cursor = end + 2  # 跳过分隔符 "\n\n"
    return out


class ArtifactStore:
    """解析产物门面: 进程级单例, 构造零 I/O。"""

    def __init__(self) -> None:
        self._db = None

    def _coll(self, name: str):
        if self._db is None:
            self._db = get_db()
        return self._db[name]

    async def save(
        self,
        upload_id: str,
        *,
        doc_key: str,
        content_sha256: str,
        parser_version: str,
        modality: str,
        filename: str,
        blocks: Sequence[ParsedBlock],
        suggested_tags: Sequence[str] | None = None,
        warnings: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """固化一份解析产物(写路径: mongo 关闭或连接异常一律向上抛)。"""
        if not get_settings().mongo_enabled:
            raise RuntimeError("mongo_enabled=false, 无法写入解析产物")
        raw = join_raw(blocks)
        index = block_offsets(blocks, raw)
        cap = get_settings().mongo_body_max_bytes
        parts = split_text(raw, cap) if byte_size(raw) > cap else []

        doc = {
            "_id": upload_id,
            "upload_id": upload_id,
            "doc_key": doc_key,
            "content_sha256": content_sha256,
            "parser_version": parser_version,
            "modality": modality,
            "filename": filename,
            "raw_text": "" if parts else raw,
            "parts": len(parts),
            "blocks": index,
            "block_count": len(index),
            "raw_len": len(raw),
            "head": raw[:HEAD_CHARS],
            "preview": raw[:PREVIEW_CHARS],
            "preview_len": len(raw),
            "suggested_tags": list(suggested_tags or []),
            "warnings": list(warnings or []),
            "created_at": _utcnow(),
        }
        await self._coll(COLL_PARSE_ARTIFACT).replace_one(
            {"_id": upload_id}, doc, upsert=True
        )
        # 分片: 先按 upload_id 清旧再写新(重解析后正文变短时不留脏分片)。
        await self._coll(COLL_PARSE_ARTIFACT_PARTS).delete_many({"upload_id": upload_id})
        if parts:
            await self._coll(COLL_PARSE_ARTIFACT_PARTS).insert_many(
                [
                    {
                        "_id": f"{upload_id}#raw#{i}",
                        "upload_id": upload_id,
                        "part_no": i,
                        "text": seg,
                        "created_at": _utcnow(),
                    }
                    for i, seg in enumerate(parts)
                ],
                ordered=False,
            )
        return doc

    async def update_tags(
        self, upload_id: str, suggested_tags: Sequence[str], warnings: Sequence[str] = ()
    ) -> None:
        """回填 LLM 标签(标签在解析之后才产出, 且不触发重解析)。"""
        if not get_settings().mongo_enabled:
            return
        try:
            await self._coll(COLL_PARSE_ARTIFACT).update_one(
                {"_id": upload_id},
                {
                    "$set": {
                        "suggested_tags": list(suggested_tags),
                        "warnings": list(warnings),
                        "updated_at": _utcnow(),
                    }
                },
            )
        except PyMongoError as exc:
            logger.warning("artifact 标签回填失败(预览仍可看) upload_id=%s: %s", upload_id, exc)

    async def get(self, upload_id: str) -> dict[str, Any] | None:
        """取产物元文档(不含分片正文); 读失败降级为 None。"""
        if not get_settings().mongo_enabled:
            return None
        try:
            return await self._coll(COLL_PARSE_ARTIFACT).find_one({"_id": upload_id})
        except PyMongoError as exc:
            logger.warning("artifact 读失败(降级为无产物) upload_id=%s: %s", upload_id, exc)
            return None

    async def is_reusable(
        self, upload_id: str, content_sha256: str, parser_version: str
    ) -> bool:
        """产物能否直接复用: 同一份字节 + 同一个解析器版本。

        任一不同就必须重解析 —— 解析器改了还复用旧产物, 表现为"代码改了没生效"。
        """
        doc = await self.get(upload_id)
        if not doc:
            return False
        return (
            doc.get("content_sha256") == content_sha256
            and doc.get("parser_version") == parser_version
        )

    async def load_raw(self, upload_id: str) -> str:
        """取整篇 raw(内联优先, 否则按 part_no 升序拼分片)。"""
        doc = await self.get(upload_id)
        if not doc:
            return ""
        inline = doc.get("raw_text") or ""
        if inline:
            return inline
        nparts = int(doc.get("parts") or 0)
        if not nparts:
            return ""
        cursor = (
            self._coll(COLL_PARSE_ARTIFACT_PARTS)
            .find({"upload_id": upload_id, "part_no": {"$gte": 0, "$lt": nparts}})
            .sort("part_no", 1)
            .batch_size(get_settings().mongo_batch_page_size)
        )
        return "".join(d["text"] async for d in cursor)

    async def load_blocks(self, upload_id: str) -> tuple[str, list[ParsedBlock], str]:
        """还原 ``(modality, blocks, raw)``: 预览与入库的唯一读取入口。

        缺产物直接抛(不静默返回空块): 空块会让"入库成功但零知识块"看起来像成功。
        """
        doc = await self.get(upload_id)
        if not doc:
            raise ValueError(f"解析产物不存在: upload_id={upload_id}")
        raw = await self.load_raw(upload_id)
        if not raw:
            raise ValueError(f"解析产物正文为空: upload_id={upload_id}")
        blocks: list[ParsedBlock] = []
        for item in doc.get("blocks") or []:
            start, end = int(item.get("start", -1)), int(item.get("end", -1))
            if start < 0 or end < start or end > len(raw):
                raise ValueError(
                    f"解析产物偏移越界: upload_id={upload_id} seq={item.get('seq')}"
                )
            blocks.append(
                ParsedBlock(
                    section=item.get("section", ""),
                    page_no=int(item.get("page_no", -1)),
                    text=raw[start:end],
                    parent_type=item.get("parent_type", "section") or "section",
                )
            )
        return str(doc.get("modality") or "text"), blocks, raw

    async def delete(self, upload_id: str) -> None:
        """删产物及其分片(过期回收 / 文档删除投影; 失败只告警, 残留由 GC 再收)。"""
        if not get_settings().mongo_enabled:
            return
        try:
            await self._coll(COLL_PARSE_ARTIFACT).delete_one({"_id": upload_id})
            await self._coll(COLL_PARSE_ARTIFACT_PARTS).delete_many({"upload_id": upload_id})
        except PyMongoError as exc:
            logger.warning("artifact 删除失败(由 GC 再回收) upload_id=%s: %s", upload_id, exc)

    async def list_orphans(
        self, known_upload_ids: set[str], *, older_than: datetime | None = None
    ) -> list[str]:
        """列出 ``doc_uploads`` 行已不存在的产物 upload_id(孤儿回收用)。

        写入顺序是"先上传行后产物", 所以产物孤立 = 上传行已被删。``older_than`` 给
        正在进行的解析留缓冲: 不卡这个时间差就会把刚写完、行还没回库的产物当成孤儿删掉。
        """
        if not get_settings().mongo_enabled:
            return []
        query: dict[str, Any] = {}
        if older_than is not None:
            query["created_at"] = {"$lt": older_than}
        out: list[str] = []
        try:
            async for doc in self._coll(COLL_PARSE_ARTIFACT).find(query, {"_id": 1}):
                if str(doc["_id"]) not in known_upload_ids:
                    out.append(str(doc["_id"]))
        except PyMongoError as exc:
            logger.warning("artifact 孤儿扫描失败: %s", exc)
        return out

    async def count(self) -> int:
        if not get_settings().mongo_enabled:
            return 0
        try:
            return int(await self._coll(COLL_PARSE_ARTIFACT).count_documents({}))
        except PyMongoError as exc:
            logger.warning("artifact count 失败: %s", exc)
            return -1


_artifact_store: ArtifactStore | None = None


def get_artifact_store() -> ArtifactStore:
    """进程级单例(与 ``get_body_store()`` 同风格)。"""
    global _artifact_store
    if _artifact_store is None:
        _artifact_store = ArtifactStore()
    return _artifact_store
