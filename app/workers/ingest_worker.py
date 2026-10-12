"""后台 worker 进程(入库流水线): 解析 / embedding / 发布 / 投影 + 回收调度。

与网关同镜像、不同 command(``python -m app.workers.ingest_worker``): OCR、大文件切块、
LLM tags、embedding、ES 写入都不该压在 HTTP 请求线程上, 也不该与对话抢同一个进程。

编排口径(理解这一段就看懂了整个 worker):

- **PG 任务表是事实源, Redis Streams 只是低延迟 handoff**。``stream_consume`` 负责"事件
  一到就开工", ``db_poll`` 负责"事件没到也得开工"(Redis 当时不通、消息丢了、进程重启
  漏消息三种情况都靠它自愈)。两者都只做同一件事: 尝试按租约认领任务, 抢到才跑。
- **每个阶段幂等**, 所以认领失败/中途崩溃都不需要"回滚", 只需要重放。
- **worker 不建表**: schema 归属只在网关 lifespan 的 ``init_schema()``。两个进程同时跑
  ``create_all`` + ALTER 会出现"一个在补列、一个在读该列"的竞态, 而这类竞态的表象是
  启动期一条难懂的 DDL 错误, 排查成本远高于在这里多等几秒。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from app.cache.redis_client import close_redis
from app.config import get_settings
from app.db.session import get_engine, get_session_factory
from app.docs import pipeline

logger = logging.getLogger("app.workers.ingest_worker")

# worker 需要看到的表: 缺任何一张都还没准备好干活(网关还没跑完 init_schema)。
_REQUIRED_TABLES = ("doc_uploads", "ingest_tasks", "outbox_events")
# 等 schema 就绪的上限: 超时不退出进程, 只继续按轮询等(冷启动时 PG 比 worker 慢是常态)。
_SCHEMA_RETRY_SECONDS = 5
# 建消费者组的尝试次数(每次间隔 3s): 超过就降级为纯 PG 轮询, 不死等 Redis。
_GROUP_RETRY = 5
# 轮询里"不值得记一条 INFO"的结果: 别的 worker 已抢走, 或租约未到期。
_NOOP_OUTCOMES = ("skipped", "waiting")


def _worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


# 阻塞式 XREADGROUP 必须用专用客户端: 共享的 ``get_redis()`` 为了"缓存永不阻断对话"
# 把 socket_timeout 定在 2s, 而 ``block=5000`` 的长轮询本来就要占住连接更久 —— 用共享
# 客户端的结局是每条消费命令必然读超时, 入库时延完整退化到轮询间隔(而且刷屏)。
_stream_redis: Redis | None = None


def get_stream_redis() -> Redis | None:
    """stream 专用连接(不做读超时截断); ``redis_enabled=false`` 时返回 None。"""
    global _stream_redis
    settings = get_settings()
    if not settings.redis_enabled:
        return None
    if _stream_redis is None:
        _stream_redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=None,           # 阻塞命令由 block 参数自己定时, 不做 socket 级截断
            health_check_interval=30,
            max_connections=max(4, settings.ingest_worker_concurrency + 2),
        )
    return _stream_redis


def _ensure_logging() -> None:
    """补 root handler: 否则 uvicorn 之外的裸进程日志只会走 lastResort(WARNING 起)。"""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


# ---------------------------------------------------------------- schema 就绪等待

async def wait_for_schema(stop: asyncio.Event) -> bool:
    """轮询到三张流水线表都存在才返回(不做任何 DDL)。

    表存在性是"网关已完成 init_schema"的可靠信号; 用 ``pg_tables`` 而不是 ``to_regclass``
    是为了与迁移脚本同一口径(历史上曾因直接查列名在缺表时抛错并被静默吞掉)。
    """
    sql = text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename = ANY(:names)"
    )
    attempt = 0
    while not stop.is_set():
        try:
            async with get_session_factory()() as session:
                rows = (await session.execute(sql, {"names": list(_REQUIRED_TABLES)})).scalars().all()
            missing = [t for t in _REQUIRED_TABLES if t not in set(rows)]
            if not missing:
                logger.info("schema 就绪: %s", ", ".join(_REQUIRED_TABLES))
                return True
            attempt += 1
            if attempt == 1 or attempt % 12 == 0:
                # 只在第一次与每分钟打一条: worker 比网关先起是正常时序, 不该刷日志。
                logger.error(
                    "等待网关建表(缺 %s); worker 不做 DDL, 将继续每 %ds 重试",
                    ",".join(missing), _SCHEMA_RETRY_SECONDS,
                )
        except Exception as exc:  # noqa: BLE001 - PG 还没健康起来时属预期
            attempt += 1
            if attempt % 12 == 1:
                logger.warning("schema 探测失败(稍后重试): %s", exc)
        await asyncio.sleep(_SCHEMA_RETRY_SECONDS)
    return False


# ---------------------------------------------------------------- stream 基础设施

async def ensure_stream_group(stop: asyncio.Event) -> bool:
    """幂等创建消费者组(``XGROUP CREATE ... MKSTREAM``); BUSYGROUP 视为已存在。

    只试有界几次就退回去跑 ``db_poll``: 等 Redis 就绪不是 worker 的职责, 死等会出现
    "Redis 挂了 = 入库完全不跑" —— 而事实上任务表驱动能把它跑完, 只是时延从秒变到轮询间隔。
    """
    settings = get_settings()
    redis = get_stream_redis()
    if redis is None:
        logger.warning("redis_enabled=false, 只按 PG 轮询驱动入库任务")
        return False
    for attempt in range(_GROUP_RETRY):
        if stop.is_set():
            return False
        try:
            await redis.xgroup_create(
                settings.ingest_stream_key, settings.ingest_stream_group, id="0", mkstream=True
            )
            logger.info("stream group 就绪: %s / %s", settings.ingest_stream_key, settings.ingest_stream_group)
            return True
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" in str(exc):
                logger.info("stream group 已存在, 直接复用")
                return True
            logger.warning(
                "建 stream group 失败(%d/%d): %s", attempt + 1, _GROUP_RETRY, exc
            )
            await asyncio.sleep(3)
    logger.error(
        "stream 通道未就绪, 本轮降级为纯 PG 轮询驱动(入库时延=%ds, 可由回收器重试重新启用)",
        settings.ingest_db_poll_seconds,
    )
    return False


async def _ack(redis: Any, msg_id: str) -> None:
    settings = get_settings()
    with contextlib.suppress(Exception):
        await redis.xack(settings.ingest_stream_key, settings.ingest_stream_group, msg_id)


def _decode_message(fields: dict) -> dict[str, Any]:
    """把 stream 消息体解成 {topic, doc_key, payload, event_id}。"""
    payload_raw = fields.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw)
    except (TypeError, ValueError):
        payload = {}
    return {
        "topic": str(fields.get("topic") or ""),
        "doc_key": str(fields.get("doc_key") or ""),
        "payload": payload,
        "event_id": str(fields.get("event_id") or ""),
    }


# 阻塞 XREADGROUP 的等待时长与失败退避基值。
_STREAM_BLOCK_MS = 5000
_STREAM_RETRY_SECONDS = 3


# ---------------------------------------------------------------- 协程: 消费 stream

async def stream_consume(stop: asyncio.Event, gate: asyncio.Semaphore) -> None:
    """XREADGROUP 领取事件并分派; 处理完才 XACK。

    先 ``async with gate`` 再分派: 并发上限压在本地排队, 不把 OCR/Ollama 压到雪崩。
    处理异常不 ACK(留在 pending), 由 :func:`claim_stale_pending` 按 idle 时长重领 ——
    "失败就丢消息"会让任务永远停在未推进的阶段。
    """
    settings = get_settings()
    redis = get_stream_redis()
    if redis is None:
        return
    worker = _worker_name()
    logger.info("stream 消费启动 consumer=%s", worker)
    warned = False
    backoff = _STREAM_RETRY_SECONDS
    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(
                groupname=settings.ingest_stream_group,
                consumername=worker,
                streams={settings.ingest_stream_key: ">"},
                count=settings.ingest_claim_batch,
                block=_STREAM_BLOCK_MS,
            )
            warned = False
            if not resp:
                continue
            for _stream, messages in resp:
                for msg_id, fields in messages:
                    msg = _decode_message(fields)
                    if not msg["topic"]:
                        await _ack(redis, msg_id)
                        continue
                    async with gate:
                        try:
                            outcome = await pipeline.dispatch_event(msg["topic"], msg["payload"], worker)
                            logger.debug(
                                "事件处理完成 topic=%s doc_key=%s outcome=%s",
                                msg["topic"], msg["doc_key"], outcome,
                            )
                        except Exception as exc:  # noqa: BLE001 - 不 ACK, 留给重领
                            logger.warning(
                                "事件处理异常(留在 pending 待重领) topic=%s: %s",
                                msg["topic"], exc,
                            )
                            continue
                    await _ack(redis, msg_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Redis 抖动退回 PG 轮询, 不退出协程
            # 同类失败只告警一次(否则 Redis 挂了会被刷成日志风暴), 退避到上限后保持。
            if not warned:
                logger.warning("stream 消费失败(转入退避; 期间由 PG 轮询兜住): %s", exc)
                warned = True
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 2)


async def claim_stale_pending(stop: asyncio.Event) -> None:
    """按 idle 时长重领滞留 pending 的消息(XAUTOCLAIM)。

    worker 被 SIGKILL 时消息会留在 pending 列表里没人 ACK; 不回收就会出现"事件已消费过
    但任务没推进"的悬挂。任务本身幂等, 重领安全。
    """
    settings = get_settings()
    redis = get_stream_redis()
    if redis is None:
        return
    worker = _worker_name()
    min_idle_ms = settings.ingest_task_lease_seconds * 1000
    while not stop.is_set():
        await asyncio.sleep(30)
        try:
            while True:
                claimed = await redis.xautoclaim(
                    settings.ingest_stream_key,
                    settings.ingest_stream_group,
                    worker,
                    min_idle_time=min_idle_ms,
                    count=settings.ingest_claim_batch,
                )
                # redis-py 返回 (next_start_id, messages[, deleted]]; 空消息即结束本轮。
                messages = claimed[1] if len(claimed) > 1 else []
                if not messages:
                    break
                for msg_id, fields in messages:
                    msg = _decode_message(fields or {})
                    if not msg["topic"]:
                        with contextlib.suppress(Exception):
                            await redis.xack(
                                settings.ingest_stream_key, settings.ingest_stream_group, msg_id
                            )
                        continue
                    try:
                        await pipeline.dispatch_event(msg["topic"], msg["payload"], worker)
                    except Exception as exc:  # noqa: BLE001 - 下轮再试
                        logger.warning("重领消息处理失败(下轮再试) topic=%s: %s", msg["topic"], exc)
                        continue
                    with contextlib.suppress(Exception):
                        await redis.xack(
                            settings.ingest_stream_key, settings.ingest_stream_group, msg_id
                        )
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("XAUTOCLAIM 失败(下轮再试): %s", exc)


# ---------------------------------------------------------------- 协程: PG 轮询自愈

async def db_poll(stop: asyncio.Event, gate: asyncio.Semaphore) -> None:
    """从任务表捞"没人持有租约"的任务并推进: 事件通道坏了他也能跑完。

    这是 worker 与"纯消息队列驱动"方案的本质区别: 队列只优化时延, 正确性由库里的
    状态机保证。认领本身是 ``UPDATE ... WHERE lease 空或过期 RETURNING``, 抢不到就跳过。
    """
    settings = get_settings()
    worker = _worker_name()
    while not stop.is_set():
        await asyncio.sleep(settings.ingest_db_poll_seconds)
        try:
            task_ids = await pipeline.fetch_claimable_task_ids(settings.ingest_claim_batch)
            for task_id in task_ids:
                async with gate:
                    outcome = await pipeline.advance_task(task_id, worker)
                if outcome not in _NOOP_OUTCOMES:
                    logger.info("轮询推进 task_id=%s -> %s", task_id, outcome)
            upload_ids = await pipeline.fetch_pending_upload_ids(settings.ingest_claim_batch)
            for upload_id in upload_ids:
                async with gate:
                    state = await pipeline.run_parse_job(upload_id)
                if state not in ("inprogress", "skipped"):
                    logger.info("轮询解析 upload_id=%s -> %s", upload_id, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 轮询协程绝不能因一次抖动退出
            logger.warning("db_poll 本轮失败: %s", exc)


# ---------------------------------------------------------------- 协程: outbox relay

async def outbox_relay(stop: asyncio.Event) -> None:
    """把库里的待投事件 XADD 到 stream(投递成功才标 sent)。

    Redis 不通时这里只会堆积事件 —— 堆积是安全的: 任务表还在, db_poll 会照样把活干完,
    事件恢复投递后也只是"多余地再触发一次认领", 而认领会因租约已被持有而跳过。
    """
    settings = get_settings()
    redis = get_stream_redis()
    while not stop.is_set():
        await asyncio.sleep(settings.outbox_relay_interval_seconds)
        if redis is None:
            continue
        try:
            events = await pipeline.fetch_unsent_events(settings.outbox_poll_batch)
            for ev in events:
                try:
                    await redis.xadd(
                        settings.ingest_stream_key,
                        {
                            "event_id": str(ev["event_id"]),
                            "topic": ev["topic"],
                            "doc_key": ev["doc_key"],
                            "payload": json.dumps(ev["payload"], ensure_ascii=False),
                        },
                    )
                    await pipeline.mark_event_sent(ev["event_id"])
                except Exception as exc:  # noqa: BLE001 - 单条失败退避重试
                    logger.warning(
                        "事件投递失败 event_id=%s topic=%s: %s", ev["event_id"], ev["topic"], exc
                    )
                    await pipeline.mark_event_retry(
                        ev["event_id"], int(ev["attempts"]) + 1, f"{exc.__class__.__name__}: {exc}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("outbox relay 本轮失败: %s", exc)


# ---------------------------------------------------------------- 协程: 调度器

async def scheduler_loop(stop: asyncio.Event) -> None:
    """定时回收: 超时任务回收 / 陈旧块 GC / 过期上传清理。

    三个职责共用一个循环但各自按自己的间隔触发: 回收要快(它决定卡住的任务多久能
    被重新认领), GC 可以慢(它只是省磁盘), 清理更慢(过期以小时计)。
    """
    settings = get_settings()
    gc_interval = max(30, settings.ingest_gc_interval_seconds)
    last_gc = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        await asyncio.sleep(max(5, settings.ingest_reap_interval_seconds))
        now = loop.time()
        try:
            await pipeline.reap_stalled_tasks()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务回收失败: %s", exc)
        if now - last_gc >= gc_interval:
            last_gc = now
            try:
                await pipeline.gc_deprecated_rows()
                await pipeline.cleanup_expired_uploads()
            except Exception as exc:  # noqa: BLE001
                logger.warning("GC/过期清理失败: %s", exc)


# ---------------------------------------------------------------- 关停与主入口

async def _shutdown_clients() -> None:
    """释放本进程持有的连接池(与网关 lifespan 同一份清单, 少一项就多一堆 TIME_WAIT)。"""
    global _stream_redis
    from app.bodies.client import close_mongo
    from app.docs.parsers import close_mineru_client
    from app.rag.bm25 import close_es_client
    from app.rag.embeddings import close_embedder_client

    async def _close_stream() -> None:
        # stream 专用连接(阻塞读不做超时截断)与共享缓存连接是两个池, 都要放掉。
        if _stream_redis is not None:
            await _stream_redis.aclose()
    with contextlib.suppress(Exception):
        await close_es_client()
    with contextlib.suppress(Exception):
        await close_embedder_client()
    with contextlib.suppress(Exception):
        await close_mineru_client()
    with contextlib.suppress(Exception):
        await close_redis()
    with contextlib.suppress(Exception):
        await _close_stream()
    _stream_redis = None
    with contextlib.suppress(Exception):
        await close_mongo()
    with contextlib.suppress(Exception):
        await get_engine().dispose()


async def run_forever() -> None:
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows 的 signal 支持有限
            loop.add_signal_handler(sig, stop.set)

    worker = _worker_name()
    logger.info(
        "ingest worker 启动 name=%s concurrency=%d async=%s stream=%s",
        worker, settings.ingest_worker_concurrency, settings.ingest_async_enabled,
        settings.ingest_stream_key,
    )
    if not await wait_for_schema(stop):
        logger.info("收到关停信号, worker 退出(未开始消费)")
        await _shutdown_clients()
        return
    has_stream = await ensure_stream_group(stop)
    if not has_stream:
        logger.warning("无 stream 通道, 入库时延退化为 PG 轮询间隔(%ds)", settings.ingest_db_poll_seconds)

    gate = asyncio.Semaphore(max(1, settings.ingest_worker_concurrency))
    coros = [
        stream_consume(stop, gate),
        claim_stale_pending(stop),
        db_poll(stop, gate),
        outbox_relay(stop),
        scheduler_loop(stop),
    ]
    tasks = [asyncio.create_task(c, name=f"ingest-worker-{i}") for i, c in enumerate(coros)]
    await stop.wait()
    logger.info("收到关停信号: 不再认领新任务, 等待在跑的阶段结束(最多 %ds)", settings.ingest_task_lease_seconds)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await _release_my_leases(worker)
    await _shutdown_clients()
    logger.info("ingest worker 已退出")


async def _release_my_leases(worker: str) -> None:
    """退出前把自己持有的租约放掉: 否则接任的 worker 要等一整个租约时长才能重领。"""
    now = datetime.now(timezone.utc)
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE ingest_tasks SET lease_owner = '', lease_expires_at = NULL, "
                        "updated_at = :now WHERE lease_owner = :w"
                    ),
                    {"now": now, "w": worker},
                )
    except Exception as exc:  # noqa: BLE001 - 放不掉租约只影响时延, 回收器会兜住
        logger.warning("释放本机租约失败(将由回收器按超时处理): %s", exc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="入库流水线 worker(解析/embedding/发布/投影 + 回收)")
    parser.add_argument(
        "--once-gc",
        action="store_true",
        help="只跑一轮回收 + 陈旧块 GC + 过期清理后退出(运维手动触发, 不消费任务)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_logging()
    args = _parser().parse_args(argv)
    if args.once_gc:
        asyncio.run(_run_once_gc())
        return 0
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        return 130
    return 0


async def _run_once_gc() -> None:
    report = await pipeline.run_scheduler_once()
    logger.info("手动回收完成: %s", report)
    await _shutdown_clients()


if __name__ == "__main__":
    raise SystemExit(main())
