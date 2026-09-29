"""Session Memory: 短期会话记忆(滚动窗口 + LLM 压缩摘要), Redis 支撑 + 进程内降级。

架构图里 Session Memory 落在 Redis。这里同时保留一份进程内 dict 作为降级路径:
``redis_enabled=false`` 或某次 Redis 命令失败时, 该会话直接退回进程内存储
(等价于本功能上线前的行为), 不阻断对话 —— 与缓存层 / Graph 长期记忆同一套
"能降级就降级"的策略, 而不是要求 Redis 必须是硬依赖。

注意: 降级到进程内 dict 意味着多进程部署时每个 worker 各存一份, 一致性不
如 Redis —— 这是降级路径固有的代价, 不是缺陷, 不应据此认为 Redis 可选。
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

from app.assistant.prompts import SUMMARY_PROMPT
from app.cache.redis_client import SESSION_PREFIX, get_redis, try_redis
from app.config import get_settings
from app.llm import get_chat_model

logger = logging.getLogger(__name__)


@dataclass
class SessionMemory:
    """进程内降级用的 per-session 状态: 滚动窗口 + 压缩摘要。"""

    turns: list[tuple[str, str]] = field(default_factory=list)  # (user, assistant)
    summary: str = ""


def _render_turn(user: str, assistant: str) -> str:
    return f"用户: {user}\n助手: {assistant}"


class MemoryStore:
    """Session memory manager with summarization on overflow."""

    def __init__(self) -> None:
        settings = get_settings()
        self._max_turns = settings.memory_max_turns
        self._summary_threshold = settings.memory_summary_threshold
        self._ttl = settings.session_memory_ttl
        # 仅降级路径使用: 有界 LRU(dict + move_to_end)而不是无限 dict。
        # Redis 长时间不可用且会话不重复时, 旧写法会按会话数单调涨内存(每会话
        # 还带最多 memory_max_turns 轮原文), 到 1000 人规模就是提前写好的一次 OOM。
        self._local_max = max(100, settings.session_memory_local_max)
        self._sessions: OrderedDict[str, SessionMemory] = OrderedDict()
        self._summarizer = get_chat_model(settings.llm_model, temperature=0)

    def _redis_or_none(self):
        return get_redis() if get_settings().redis_enabled else None

    def _turns_key(self, session_id: str) -> str:
        return f"{SESSION_PREFIX}:{session_id}:turns"

    def _summary_key(self, session_id: str) -> str:
        return f"{SESSION_PREFIX}:{session_id}:summary"

    # -------------------------------------------------------------- 降级路径
    def _local(self, session_id: str) -> SessionMemory:
        """取/建一个降级会话, 并按 LRU 淘汰超出的旧会话。

        淘汰只影响降级路径的历史完整性(与 Redis 掉同一个会话同性质), 不会串到
        别人的会话: 每个会话自己的 turns 只跟着自己的 key 走。
        """
        mem = self._sessions.get(session_id)
        if mem is None:
            mem = SessionMemory()
            self._sessions[session_id] = mem
            while len(self._sessions) > self._local_max:
                evicted, _ = self._sessions.popitem(last=False)
                logger.debug("session memory 降级缓存达上限, 淘汰最旧会话 %s", evicted)
        else:
            self._sessions.move_to_end(session_id)
        return mem

    def _local_history_text(self, session_id: str) -> str:
        mem = self._local(session_id)
        lines: list[str] = []
        if mem.summary:
            lines.append(f"[历史摘要] {mem.summary}")
        for user, assistant in mem.turns[-self._max_turns :]:
            lines.append(f"用户: {user}")
            lines.append(f"助手: {assistant}")
        return "\n".join(lines)

    async def _local_append(self, session_id: str, user: str, assistant: str) -> str:
        """降级路径的追加; 返回本轮新压缩出的摘要(未发生压缩时为空串)。"""
        mem = self._local(session_id)
        mem.turns.append((user, assistant))
        if len(mem.turns) <= self._summary_threshold:
            return ""
        overflow = mem.turns[: -self._max_turns]
        old = "\n".join(_render_turn(u, a) for u, a in overflow)
        prompt = SUMMARY_PROMPT.format(history=f"{mem.summary}\n{old}".strip())
        try:
            resp = await self._summarizer.ainvoke(prompt)
        except Exception as exc:  # noqa: BLE001 - 摘要失败也不能丢轮次, 故先摘要后裁剪
            logger.warning("session summary 失败, 本轮保留原文不裁剪: %s", exc)
            return ""
        mem.summary = str(resp.content).strip()
        mem.turns = mem.turns[-self._max_turns :]
        return mem.summary

    # -------------------------------------------------------------- Redis 路径
    async def history_text(self, session_id: str) -> str:
        """Render summary + recent window as prompt-ready text.

        Redis 可用时以 Redis 为准; 任一命令降级(连接失败)即退回进程内 dict。
        """
        redis = self._redis_or_none()
        if redis is None:
            return self._local_history_text(session_id)

        turns_key, summary_key = self._turns_key(session_id), self._summary_key(session_id)
        raw_turns = await try_redis(
            lambda: redis.lrange(turns_key, -self._max_turns, -1),
            default=None,
            what="session memory lrange",
        )
        if raw_turns is None:  # 命令级降级 -> 退回进程内
            return self._local_history_text(session_id)
        summary = (
            await try_redis(lambda: redis.get(summary_key), default="", what="session memory get summary")
            or ""
        )

        lines: list[str] = []
        if summary:
            lines.append(f"[历史摘要] {summary}")
        for raw in raw_turns:
            try:
                user, assistant = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                logger.warning("session memory turn payload invalid, skipped: %r", raw)
                continue
            lines.append(f"用户: {user}")
            lines.append(f"助手: {assistant}")
        return "\n".join(lines)

    async def append(self, session_id: str, user: str, assistant: str) -> str:
        """Append one turn; summarize into long-term memory on overflow.

        返回值是"本轮新折叠出来的会话摘要"(没发生溢出压缩时为空串)。调用方
        (``persist_memory``)凭这个返回值把摘要再沉一条到 Episodic Memory ——
        也就是架构图里 Session -> Episodic 那条边; 摘要本身仍归 Session 层管,
        记忆层不需要为此知道 user_id。
        """
        redis = self._redis_or_none()
        if redis is None:
            return await self._local_append(session_id, user, assistant)

        turns_key, summary_key = self._turns_key(session_id), self._summary_key(session_id)
        pushed = await try_redis(
            lambda: redis.rpush(turns_key, json.dumps([user, assistant], ensure_ascii=False)),
            default=None,
            what="session memory rpush",
        )
        if pushed is None:  # 命令级降级 -> 退回进程内, 不能只丢掉这一轮不写
            return await self._local_append(session_id, user, assistant)
        await try_redis(lambda: redis.expire(turns_key, self._ttl), what="session memory expire turns")

        overflow_n = int(pushed) - self._max_turns
        if int(pushed) <= self._summary_threshold or overflow_n <= 0:
            return ""

        # 溢出: 把最老的 overflow_n 轮折叠进摘要, 再从列表里 LTRIM 掉。
        overflow_raw = await try_redis(
            lambda: redis.lrange(turns_key, 0, overflow_n - 1),
            default=None,
            what="session memory lrange overflow",
        )
        old_summary = (
            await try_redis(lambda: redis.get(summary_key), default="", what="session memory get summary")
            or ""
        )
        if overflow_raw is None:
            return ""  # 读不到溢出内容就保持原样, 不能盲目 LTRIM 丢数据

        old_lines = []
        for raw in overflow_raw:
            try:
                u, a = json.loads(raw)
                old_lines.append(_render_turn(u, a))
            except (json.JSONDecodeError, ValueError):
                continue
        old_text = "\n".join(old_lines)
        prompt = SUMMARY_PROMPT.format(history=f"{old_summary}\n{old_text}".strip())
        try:
            resp = await self._summarizer.ainvoke(prompt)
            new_summary = str(resp.content).strip()
        except Exception as exc:  # noqa: BLE001 - 摘要失败也不能丢轮次, 保留旧摘要
            logger.warning("session summary 失败, 本轮保留旧摘要不裁剪: %s", exc)
            return ""
        await try_redis(
            lambda: redis.set(summary_key, new_summary, ex=self._ttl),
            what="session memory set summary",
        )
        await try_redis(lambda: redis.ltrim(turns_key, overflow_n, -1), what="session memory ltrim")
        return new_summary


_memory_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """Process-wide singleton memory store."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store
