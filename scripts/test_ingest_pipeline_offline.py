"""入库流水线改造的离线单测: 不需要整栈, 不需要 PG/Mongo/Redis/ES/Ollama。

跑法::

    uv run python -m scripts.test_ingest_pipeline_offline

覆盖的正是这次改造最容易「静默坏掉」的六件事(都只靠读代码看不出来):

1. **task_id 幂等**: 同一 ``(doc_key, upload_id, content_sha256)`` 必须算出同一个 id ——
   连点两次「确认入库」如果排两个队, 后一个会把前一个刚发布的块再改一遍;
2. **解析产物往返**: artifact 存的是「块在 raw 里的偏移」, 还原必须逐字节等价, 且
   偏移不齐时立刻抛而不是写 -1(串台的块事后查不到);
3. **每条读路径都过滤陈旧行**: ``deprecated_at IS NULL`` 漏一处, 就会出现「旧内容还能被
   检索到」或「增量 embed 认为块没变从而发出无向量行」;
4. **发布只标记不物删**: 主链路里一条 ``DELETE`` 都不许出现(长事务锁 + 空窗正是这次要
   修掉的东西);
5. **定向失效不误伤**: bump 某篇文档的版本号后, 只有含它的缓存项失效, 其余仍命中;
   读不到版本号时必须当未命中(否则 Redis 抖动期间会复用陈旧 chunk_id);
6. **事件与业务写同事务**: outbox 绝不自开第二个 session —— 那样就又变回双写。

真实的租约抢占/超时回收/端到端入库在容器里验证(见 README「验证」一节与
``.qoder/rules/container-first-verification.md``)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.dialects import postgresql

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def _sql(stmt: Any) -> str:
    """把语句编译成 SQL 文本(带占位符即可, 断言只看结构)。"""
    return str(stmt.compile(dialect=postgresql.dialect()))


# ------------------------------------------------------------------ 通用假会话
class FakeResult:
    def __init__(
        self, rows: list[Any] | None = None, scalar: Any = None, rowcount: int = 0
    ) -> None:
        self._rows = rows or []
        self._scalar = scalar
        self.rowcount = rowcount

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> "FakeResult":
        return self

    def scalar_one(self) -> Any:
        return self._scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def __iter__(self):
        return iter(self._rows)


class _Begin:
    def __init__(self, session: "FakeSession") -> None:
        self._s = session

    async def __aenter__(self) -> "FakeSession":
        return self._s

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # 记录提交/回滚: 断言「异常时事件也不该留下」要靠它。
        self._s.txns.append("rollback" if exc_type else "commit")
        return False


class FakeSession:
    """记录所有执行过的 SQL, 按预先登记的片段返回结果。"""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.sql: list[str] = []
        self.txns: list[str] = []
        self.added: list[Any] = []
        self._responses = responses or {}

    def _reply(self, stmt: Any) -> FakeResult:
        text = _sql(stmt) if hasattr(stmt, "compile") else str(stmt)
        for needle, value in self._responses.items():
            if needle in text:
                return value if isinstance(value, FakeResult) else FakeResult(value)
        return FakeResult()

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        self.sql.append(_sql(stmt) if hasattr(stmt, "compile") else str(stmt))
        return self._reply(stmt)

    def begin(self) -> _Begin:
        return _Begin(self)

    async def commit(self) -> None:
        self.txns.append("commit")

    async def rollback(self) -> None:
        self.txns.append("rollback")

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeFactory:
    """替掉 get_session_factory(): 数一下开了几个 session(双写检测的关键)。"""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.sessions: list[FakeSession] = []
        self._responses = responses or {}

    def __call__(self) -> FakeSession:
        session = FakeSession(self._responses)
        self.sessions.append(session)
        return session

    @property
    def opened(self) -> int:
        return len(self.sessions)

    @property
    def all_sql(self) -> list[str]:
        return [s for session in self.sessions for s in session.sql]


# ------------------------------------------------------------------ 1. task_id 幂等
def test_task_id_idempotency() -> None:
    from app.docs import pipeline

    a = pipeline.make_task_id("doc1", "up1", "sha-a")
    check("同一 (doc_key, upload_id, sha256) 得到同一 task_id",
          a == pipeline.make_task_id("doc1", "up1", "sha-a"))
    check("重传(新 upload_id)得到不同 task_id", a != pipeline.make_task_id("doc1", "up2", "sha-a"))
    check("内容变化得到不同 task_id", a != pipeline.make_task_id("doc1", "up1", "sha-b"))
    check("task_id 长度落在列宽内", len(a) == 32, f"len={len(a)}")
    check("ACTIVE_STAGES 与部分唯一索引谓词同口径",
          pipeline.ACTIVE_STAGES == ("pending", "parsing", "embedding", "publishing", "indexing"),
          str(pipeline.ACTIVE_STAGES))
    check("终态只有 ready/failed(回收器以此判是否该重认领)",
          set(pipeline.TERMINAL_STAGES) == {"ready", "failed"})


# ------------------------------------------------------------------ 2. 解析产物往返
class FakeCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def sort(self, key, direction):  # noqa: ANN001, ARG002 - 只需要保序
        return self

    def batch_size(self, n):  # noqa: ANN001, ARG002
        return self

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield d

        return _gen()


class FakeColl:
    """只实现 artifact 门面用到的几条命令(恪守「只按 _id 精确取」的不变量)。"""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def replace_one(self, flt: dict, doc: dict, upsert: bool = False) -> None:
        self.docs[flt["_id"]] = doc

    async def find_one(self, flt: dict) -> dict | None:
        return self.docs.get(flt["_id"])

    async def update_one(self, flt: dict, update: dict) -> None:
        doc = self.docs.get(flt["_id"])
        if doc is not None:
            doc.update(update.get("$set") or {})

    async def delete_one(self, flt: dict) -> None:
        self.docs.pop(flt["_id"], None)

    async def delete_many(self, flt: dict) -> None:
        if "upload_id" in flt:
            for key in [k for k, v in self.docs.items() if v.get("upload_id") == flt["upload_id"]]:
                self.docs.pop(key, None)

    def find(self, flt: dict, proj: dict | None = None) -> FakeCursor:
        return FakeCursor(list(self.docs.values()))


def test_artifact_roundtrip() -> None:
    from app.docs import artifacts as A
    from app.docs.parsers import ParsedBlock

    blocks = [
        ParsedBlock(section="第一章", page_no=1, text="甲方与乙方签订合同"),
        ParsedBlock(section="第二章/付款", page_no=2, text="付款期限为 30 天", parent_type="clause"),
    ]
    raw = A.join_raw(blocks)
    index = A.block_offsets(blocks, raw)
    check("偏移还原逐字节等价",
          all(raw[i["start"] : i["end"]] == b.text for i, b in zip(index, blocks)))
    check("块间偏移不重叠且隔一个分隔符", index[1]["start"] == index[0]["end"] + 2)
    try:
        A.block_offsets(blocks, "完全不匹配的正文")
        check("偏移对不齐时必须抛错(不写 -1)", False, "未抛异常")
    except ValueError:
        check("偏移对不齐时必须抛错(不写 -1)", True)

    colls = {A.COLL_PARSE_ARTIFACT: FakeColl(), A.COLL_PARSE_ARTIFACT_PARTS: FakeColl()}
    store = A.ArtifactStore()
    store._db = True  # 跳过真建连
    store._coll = lambda name: colls[name]  # type: ignore[method-assign]

    async def _run() -> dict[str, Any]:
        await store.save(
            "up-1", doc_key="dk-1", content_sha256="sha-1", parser_version="p1",
            modality="text", filename="合同.txt", blocks=blocks,
        )
        out = {
            "same": await store.is_reusable("up-1", "sha-1", "p1"),
            "other_sha": await store.is_reusable("up-1", "sha-2", "p1"),
            "other_ver": await store.is_reusable("up-1", "sha-1", "p2"),
        }
        _md, loaded, lraw = await store.load_blocks("up-1")
        doc = await store.get("up-1")
        await store.update_tags("up-1", ["合同", "法务"])
        doc_after = await store.get("up-1")
        missing = await store.get("不存在的-upload_id")
        out.update({
            "texts": [b.text for b in loaded],
            "shapes": all(
                loaded[i].section == b.section and loaded[i].page_no == b.page_no
                and loaded[i].parent_type == b.parent_type
                for i, b in enumerate(blocks)
            ),
            "lraw": lraw, "raw": raw,
            "preview": (doc or {}).get("preview"),
            "tags": (doc_after or {}).get("suggested_tags"),
            "missing": missing,
        })
        return out

    res = asyncio.run(_run())
    check("同字节+同解析器才能复用产物", res["same"] and not res["other_sha"] and not res["other_ver"])
    check("load_blocks 逐块还原正文", res["texts"] == [b.text for b in blocks])
    check("load_blocks 还原 section/page_no/parent_type", res["shapes"])
    check("raw 与预览同源(不重解析)", res["lraw"] == res["raw"] and res["preview"] == res["raw"][:A.PREVIEW_CHARS])
    check("标签可事后回填且不触发重解析", res["tags"] == ["合同", "法务"])
    check("缺产物返回 None(调用方必须看到它不存在)", res["missing"] is None)


# ------------------------------------------------------------------ 3+4. 陈旧行过滤 / 发布不物删
_LIVE_NEEDLE = "deprecated_at IS NULL"


async def _exercise_reads() -> list[str]:
    """把 ChunkStore/ParentStore 的每条读路径跑一遍, 收集它们发出的 SQL。"""
    from app.rag import vectorstore as V

    factory = FakeFactory()
    original = V.get_session_factory
    V.get_session_factory = lambda: factory  # type: ignore[assignment]
    try:
        store = V.ChunkStore()
        parents = V.ParentStore()
        await store.search([0.1] * 4, 5)
        await store.get_texts(["c1"])
        await store.existing_hashes("d1")
        await store.get_embeddings(["c1"])
        await store.list_doc_chunks("d1")
        async for _ in store.iter_chunk_corpus():
            pass
        await parents.get_blocks(["p1"])
        await parents.list_ids("d1")
        await parents.list_ids()
    finally:
        V.get_session_factory = original  # type: ignore[assignment]
    return [s for s in factory.all_sql if not s.startswith("SET LOCAL")]


def test_reads_exclude_stale_rows() -> None:
    selects = asyncio.run(_exercise_reads())
    check("读路径至少跑出 9 条语句", len(selects) >= 9, f"{len(selects)} 条")
    missed = [s.split(" FROM")[0][:80] for s in selects if _LIVE_NEEDLE not in s]
    check("每条读路径都过滤陈旧行", not missed, "; ".join(missed))
    check("ANN 扫描列仍不含正文", "chunk_text" not in selects[0], selects[0][:200])
    # 语料导出必须带上 ACL 四列: ES 的 bool filter 读的就是索引里的这几项。
    # 只按 _NARROW_ATTRS 解包会把私有文档写成 public —— 越权只在"权限文档+关键词命中"
    # 同时出现时暴露, 靠看代码根本看不出来。
    # (热路径的 attach_texts 回表只取 chunk_id+chunk_text, 它不参与 ES 写入, 故不算语料查询。)
    corpus_sqls = [s for s in selects if "chunk_text" in s and "chunk_index" in s]
    check(
        "BM25 语料导出查询选取 ACL 列",
        len(corpus_sqls) >= 2
        and all(
            all(col in s for col in ("visibility", "owner_id", "dept_id", "allowed_roles"))
            for s in corpus_sqls
        ),
        str(corpus_sqls)[:200],
    )


def test_corpus_rows_carry_acl() -> None:
    """语料行 -> DTO 的构造点必须把 ACL 传下去(单文档增量写的依赖路径)。"""
    from types import SimpleNamespace

    from app.rag.vectorstore import _chunk_from_corpus_row

    row = SimpleNamespace(
        chunk_id="d1-p0000-c00", doc_id="d1", parent_id="d1-p0000", chunk_index=0, ord=0,
        title="T", source="s", modality="text", section="", page_no=1, content_hash="h",
        chunk_text="正文", visibility="private", owner_id="E1", dept_id="", allowed_roles="",
    )
    chunk = _chunk_from_corpus_row(row, row.chunk_text)
    check("语料 DTO 保留 visibility/owner_id",
          chunk.visibility == "private" and chunk.owner_id == "E1",
          f"{chunk.visibility}/{chunk.owner_id}")
    check("正文就位", chunk.content == "正文")


def test_publish_marks_instead_of_deleting() -> None:
    from app.rag import vectorstore as V
    from app.schemas import KnowledgeChunk, ParentBlock

    parent = ParentBlock(parent_id="d1-p0000", doc_id="d1", title="T", content="旧父块")
    kept = KnowledgeChunk(
        chunk_id="d1-p0000-c00", doc_id="d1", title="T", source="s",
        parent_id="d1-p0000", content="保留的块",
    )
    factory = FakeFactory({
        # 旧行: c00(现行, 本轮保留) + c01(本轮不再引用); 父块 p0000(保留) + p0001(陈旧)
        "SELECT doc_chunks.chunk_id": FakeResult(["d1-p0000-c00", "d1-p0000-c01"]),
        "SELECT doc_parents.parent_id": FakeResult(["d1-p0000", "d1-p0001"]),
    })
    original = V.get_session_factory
    V.get_session_factory = lambda: factory  # type: ignore[assignment]

    async def _run() -> tuple[int, list[str]]:
        session = factory()
        async with session.begin():
            return await V.publish_in_session(session, "d1", [parent], [kept], [[0.1]])

    try:
        written, marked = asyncio.run(_run())
    finally:
        V.get_session_factory = original  # type: ignore[assignment]

    check("发布返回写入子块数与被标陈旧的块",
          written == 1 and marked == ["d1-p0000-c01"], f"{written}/{marked}")
    check("发布不删子块行(主链路无长事务 DELETE)",
          not any("DELETE FROM doc_chunks" in s for s in factory.all_sql))
    check("发布不删父块行", not any("DELETE FROM doc_parents" in s for s in factory.all_sql))
    marks = [s for s in factory.all_sql if s.startswith("UPDATE doc_")]
    check("陈旧行走 UPDATE deprecated_at",
          bool(marks) and all("deprecated_at" in s for s in marks), str(marks)[:200])
    upserts = [s for s in factory.all_sql if "ON CONFLICT" in s]
    check("upsert 覆盖列里包含 deprecated_at(重入库重置标记)",
          bool(upserts) and all("deprecated_at" in s for s in upserts))


# ------------------------------------------------------------------ 5. 定向缓存失效
class FakeRedis:
    """只实现 retrieval cache 用到的几条命令(带 mget/pipeline/scan_iter)。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.fail = fail
        self.mget_calls = 0

    def _boom(self) -> None:
        if self.fail:
            raise RuntimeError("redis down")

    async def get(self, key: str) -> str | None:
        self._boom()
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._boom()
        self.data[key] = value

    async def delete(self, *keys: str) -> int:
        self._boom()
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self._boom()
        self.mget_calls += 1
        return [self.data.get(k) for k in keys]

    async def incr(self, key: str) -> int:
        self._boom()
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def scan_iter(self, match: str = "", count: int | None = None):  # noqa: ARG002
        self._boom()
        prefix = match.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[str] = []

    def incr(self, key: str) -> None:
        self._ops.append(key)

    async def execute(self) -> list[int]:
        return [await self._redis.incr(k) for k in self._ops]


def test_doc_gen_invalidation() -> None:
    from app.cache import retrieval_cache as RC
    from app.schemas import KnowledgeChunk
    from app.security.acl import Principal

    principal = Principal(user_id="E1", department="研发部")

    def _chunk(doc_id: str) -> KnowledgeChunk:
        return KnowledgeChunk(chunk_id=f"{doc_id}-c0", doc_id=doc_id, title="T",
                              content="正文", source="s")

    async def _scenario() -> dict[str, Any]:
        redis = FakeRedis()
        RC.get_redis = lambda: redis  # type: ignore[assignment]
        calls = {"invoke": 0}

        async def invoke_docs() -> tuple[list[KnowledgeChunk], str]:
            calls["invoke"] += 1
            return [_chunk("docA")], "rerank"

        _, _, hit1 = await RC.cached_retrieve("q", 8, 4, principal, invoke_docs)
        _, _, hit2 = await RC.cached_retrieve("q", 8, 4, principal, invoke_docs)
        await RC.bump_doc_gen(["docB"])          # 无关文档变更: 不该误伤
        _, _, hit3 = await RC.cached_retrieve("q", 8, 4, principal, invoke_docs)
        await RC.bump_doc_gen(["docA"])          # 命中文档变更: 必须失效
        _, _, hit4 = await RC.cached_retrieve("q", 8, 4, principal, invoke_docs)
        invokes_after = calls["invoke"]
        mgets = redis.mget_calls

        broken = FakeRedis(fail=True)
        RC.get_redis = lambda: broken  # type: ignore[assignment]
        calls["invoke"] = 0
        _, _, hit5 = await RC.cached_retrieve("q", 8, 4, principal, invoke_docs)
        return {
            "hit1": hit1, "hit2": hit2, "hit3": hit3, "hit4": hit4, "hit5": hit5,
            "invokes_after": invokes_after, "redis_invokes": calls["invoke"],
            "mgets": mgets,
        }

    out = asyncio.run(_scenario())
    check("首次未命中后回填缓存", out["hit1"] is False and out["hit2"] is True)
    check("无关文档的版本号变化不误伤命中", out["hit3"] is True)
    check("命中文档版本变化后定向失效", out["hit4"] is False)
    check("失效后确实重新回源(不是直接把旧 payload 给出去)", out["invokes_after"] == 2, str(out["invokes_after"]))
    check("读不到当前版本号时当未命中(安全降级)", out["redis_invokes"] == 1)
    check("定向失效靠 MGET/INCR, 不 SCAN 全库", out["mgets"] >= 2)


# ------------------------------------------------------------------ 6. outbox 与业务同事务
def test_outbox_shares_caller_session() -> None:
    from app.docs import pipeline

    session = FakeSession()
    asyncio.run(pipeline.emit_event(session, pipeline.TOPIC_PARSE, "dk-1", {"upload_id": "u1"}))
    check("事件写进调用方同一个 session(不另开连接)",
          len(session.sql) == 1 and "INSERT INTO outbox_events" in session.sql[0], str(session.sql)[:200])
    check("事件行带齐中继所需列(status 决定 relay 能否拾起它)",
          all(col in session.sql[0].split("FROM")[0] for col in ("topic", "doc_key", "payload", "status")),
          session.sql[0][:200])


def test_advance_task_respects_lease() -> None:
    """拿不到租约就不动任何存储: 两个 worker 同时写块表正是这次要拦住的事。"""
    from app.docs import pipeline

    factory = FakeFactory()  # UPDATE ... RETURNING 返空 = 租约被别人持着
    original = pipeline.get_session_factory
    pipeline.get_session_factory = lambda: factory  # type: ignore[assignment]
    try:
        outcome = asyncio.run(pipeline.advance_task("t1", "worker-x"))
    finally:
        pipeline.get_session_factory = original  # type: ignore[assignment]
    check("租约拿不到直接 skipped", outcome == "skipped", str(outcome))
    check("skipped 不开第二个 session", factory.opened == 1, str(factory.opened))


# ------------------------------------------------------------------ 7. 编排接线
def test_orchestration_wiring() -> None:
    from app.config import get_settings
    from scripts.dev_services import COMPOSE_REDLINE_KEYS, DEV_SERVICES, _parse_compose_env

    s = get_settings()
    check("worker 总开关与流键位齐备", bool(s.ingest_stream_key) and bool(s.ingest_stream_group))
    check("租约大于轮询间隔(否则任务会被自己抢回)", s.ingest_task_lease_seconds > s.ingest_db_poll_seconds)
    check("重试上限/GC 批次/outbox 重试均为正数",
          s.ingest_task_max_attempts >= 1 and s.ingest_gc_batch_rows > 0 and s.outbox_max_attempts >= 1)
    check("陈旧宽限期为正数(为 0 就退化成主链路物删)", s.ingest_stale_grace_minutes > 0)
    check("解析器版本键存在(产物复用判据)", bool(s.parser_version))
    check("上传保留窗口存在", s.upload_retention_hours > 0)

    check("dev_services 已纳管 ingest-worker", "ingest-worker" in DEV_SERVICES)
    compose_env = _parse_compose_env()
    check("compose 里有 ingest-worker 服务", "ingest-worker" in compose_env, str(list(compose_env))[:120])
    worker_env = compose_env.get("ingest-worker") or {}
    leaked = sorted(k for k in COMPOSE_REDLINE_KEYS & set(worker_env) if "${" in worker_env[k])
    check("worker 块里的红线键全为字面量", not leaked, str(leaked))
    check("worker 关键地址键已注入(否则会退宿主 localhost 默认值)",
          {"ES_URL", "UPLOAD_DIR", "MONGO_URL", "OLLAMA_BASE_URL", "PG_HOST"} <= set(worker_env),
          str(sorted(set(worker_env))))


def test_env_tracks_parity() -> None:
    """双轨同名键同值: 只一轨有值 = 容器行为与宿主脚本不可互相复现。"""

    def pairs(rel: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in Path(rel).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
        return out

    host = pairs(".env.example")
    container = pairs("docker/.env.example")
    keys = [
        k for k in host
        if k.startswith(("INGEST_", "OUTBOX_")) or k in {"PARSER_VERSION", "UPLOAD_RETENTION_HOURS"}
    ]
    missing = sorted(k for k in keys if k not in container)
    check("流水线配置键已写进两轨模板", bool(keys) and not missing, str(missing))
    drift = [f"{k}: host={host[k]!r} container={container[k]!r}" for k in keys if host.get(k) != container.get(k)]
    check("同名键同值(无漂移)", not drift, "; ".join(drift))


def main() -> int:
    tests = [
        test_task_id_idempotency,
        test_artifact_roundtrip,
        test_reads_exclude_stale_rows,
        test_corpus_rows_carry_acl,
        test_publish_marks_instead_of_deleting,
        test_doc_gen_invalidation,
        test_outbox_shares_caller_session,
        test_advance_task_respects_lease,
        test_orchestration_wiring,
        test_env_tracks_parity,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 一条用例挂掉不影响其余结论
            check(f"{test.__name__} 执行完成", False, f"{exc.__class__.__name__}: {exc}")
    failed = [name for ok, name, _ in _results if not ok]
    print(f"\n合计: {len(_results)} 项, 失败 {len(failed)}" + (f" -> {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
