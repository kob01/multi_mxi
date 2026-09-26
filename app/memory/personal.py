"""个人级记忆的编排层 (架构图里的 Personal Agent 一层)。

把架构图对上代码:

    User -> PersonalMemoryAgent
             ├── Session Memory      app/assistant/memory.py (Redis, 本模块不碰)
             ├── User Memory         profile(user_profiles 表) + preference/habit
             ├── Episodic Memory     episode 桶(带时间锚点的经历)
             ├── Personal Knowledge  knowledge 桶(由情节蒸馏或对话直接沉淀)
             └── Personal Graph      Neo4j :MemoryUser 锚点 + 实体关系

两个入口对应读/写两条链路: ``build()`` 在 build_context 节点并行拉各桶拼成
Business Context, ``write()`` 在 persist_memory 节点把一次提取的结果分桶落盘。
架构图里 Session -> Episodic 那条边由 ``add_session_episode()`` 承担: 会话窗口
溢出折叠出摘要时, 摘要本身作为一条情节沉淀下来。

降级口径与既有记忆层完全一致: 任一桶读失败只让该桶缺席, 写失败只是这轮少几条
记忆, 全链路不抛出到对话主流程。``personal_memory_enabled=false`` 时读写都退回
引入分桶之前的单一 fact 通道。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from app.assistant.prompts import EPISODE_REFLECT_PROMPT
from app.config import get_settings
from app.llm import get_chat_model
from app.memory import graph_store
from app.memory.extraction import MemoryExtraction
from app.memory.profile_store import get_profile_store
from app.memory.taxonomy import (
    LEGACY_KIND_FACT,
    SOURCE_REFLECTION,
    SOURCE_SESSION_SUMMARY,
    SOURCE_TURN,
    MemoryBucket,
    label_of,
    spec_of,
)
from app.memory.vector_store import MemoryHit, get_long_term_store

logger = logging.getLogger(__name__)

# 蒸馏输入最多看最近几条情节: 再多既撑 prompt 也没必要(蒸馏要的是近期规律)。
_REFLECT_EPISODE_LIMIT = 8
# 记忆管理页一次拉的每桶条数(情节长得最快, 不做分页只做硬上限)。
_OVERVIEW_LIMIT = 100
# 读路径的 Graph 邻居展开跳数与实体起点上限。
_GRAPH_HOPS = 2

_reflector_llm = None


def _reflector():
    """惰性建情节蒸馏专用的 json_mode 模型实例(进程级单例)。"""
    global _reflector_llm
    if _reflector_llm is None:
        _reflector_llm = get_chat_model(get_settings().llm_model, temperature=0, json_mode=True)
    return _reflector_llm


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _item_dict(hit: MemoryHit) -> dict[str, Any]:
    """记忆条目 -> 前端可用结构(带中文桶名, 前端不必自己维护 kind -> label 映射)。"""
    return {
        "id": hit.id,
        "kind": hit.kind,
        "kind_label": label_of(hit.kind),
        "title": hit.title,
        "content": hit.content,
        "source": hit.source,
        "occurred_at": _iso(hit.occurred_at),
        "created_at": _iso(hit.created_at),
        "score": round(hit.score, 4),
    }


@dataclass
class PersonalContext:
    """一轮对话召回的个人记忆; 空桶不产生 prompt 小节。"""

    profile: str = ""
    preferences: list[MemoryHit] = field(default_factory=list)
    habits: list[MemoryHit] = field(default_factory=list)
    episodes: list[MemoryHit] = field(default_factory=list)
    knowledge: list[MemoryHit] = field(default_factory=list)
    legacy: list[MemoryHit] = field(default_factory=list)
    graph: list[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        """按桶拼成 prompt 片段(顺序即稳定度: 画像 -> 偏好 -> 习惯 -> 情节 -> 知识)。"""
        sections: list[str] = []
        if self.profile.strip():
            sections.append(f"{spec_of(MemoryBucket.PROFILE).prompt_header}\n{self.profile.strip()}")
        for bucket, hits in (
            (MemoryBucket.PREFERENCE, self.preferences),
            (MemoryBucket.HABIT, self.habits),
            (MemoryBucket.EPISODE, self.episodes),
            (MemoryBucket.KNOWLEDGE, self.knowledge),
        ):
            if not hits:
                continue
            spec = spec_of(bucket)
            lines = [f"- {hit.title}：{hit.content}" if hit.title else f"- {hit.content}" for hit in hits]
            sections.append(spec.prompt_header + "\n" + "\n".join(lines))
        if self.graph:
            sections.append("[关联记忆]\n" + "\n".join(self.graph))
        if self.legacy:
            # 分桶之前的老记录仍以"[长期记忆]"小节带上, 不因为升级就失联。
            sections.append("[长期记忆]\n" + "\n".join(f"- {hit.content}" for hit in self.legacy))
        return "\n".join(sections)

    def audit(self) -> dict[str, Any]:
        """各桶命中条数与 id, 供 build_context 审计(记忆拼接必须可观测)。"""
        return {
            "profile_chars": len(self.profile),
            "preference": [h.id for h in self.preferences],
            "habit": [h.id for h in self.habits],
            "episode": [h.id for h in self.episodes],
            "knowledge": [h.id for h in self.knowledge],
            "legacy": [h.id for h in self.legacy],
            "graph_paths": len(self.graph),
        }

    @property
    def hit_ids(self) -> list[int]:
        return [h.id for group in (self.preferences, self.habits, self.episodes, self.knowledge, self.legacy) for h in group]


class PersonalMemoryAgent:
    """个人级记忆的读写编排; 无状态, 进程级单例。"""

    # ------------------------------------------------------------ 读路径

    async def build(self, user_id: str, query: str) -> PersonalContext:
        """并行拉齐各桶, 拼成 Business Context 的记忆部分。

        四路各自吞异常: 画像读不到、向量库抽风、Neo4j 没起, 都只让对应小节缺席,
        不能因为记忆层把整条对话链路打断。
        """
        settings = get_settings()
        ctx = PersonalContext()
        if not user_id or not settings.long_term_memory_enabled:
            return ctx
        store = get_long_term_store()
        if not settings.personal_memory_enabled:
            # 老行为: 一次向量召回, 不分桶。
            ctx.legacy = await self._safe_legacy_facts(user_id, query)
            return ctx

        profile_text, stable_pairs, vector_hits, graph_facts = await asyncio.gather(
            self._safe_profile(user_id),
            self._safe_stable_buckets(user_id),
            self._safe_vector_recall(user_id, query),
            self._safe_graph(user_id, query),
        )
        ctx.profile = profile_text
        ctx.preferences, ctx.habits = stable_pairs
        ctx.episodes, ctx.knowledge, ctx.legacy = self._split_vector_hits(vector_hits)
        ctx.graph = graph_facts
        await self._touch(ctx.hit_ids)
        return ctx

    async def _safe_profile(self, user_id: str) -> str:
        try:
            profile = await get_profile_store().get(user_id)
            return str(profile.get("summary") or "")
        except Exception as exc:  # noqa: BLE001 - 画像缺席只是少了身份背景
            logger.warning("用户画像读取失败, 本轮跳过: %s", exc)
            return ""

    async def _safe_stable_buckets(self, user_id: str) -> tuple[list[MemoryHit], list[MemoryHit]]:
        """偏好/习惯直读: 稳定策略与当轮问法无关, 走标量而不走 embedding。"""
        settings = get_settings()
        try:
            preferences, habits = await asyncio.gather(
                get_long_term_store().list_recent(
                    user_id, [MemoryBucket.PREFERENCE.value], limit=settings.memory_preference_top_k
                ),
                get_long_term_store().list_recent(
                    user_id, [MemoryBucket.HABIT.value], limit=settings.memory_habit_top_k
                ),
            )
            return preferences, habits
        except Exception as exc:  # noqa: BLE001
            logger.warning("偏好/习惯直读失败, 本轮跳过: %s", exc)
            return [], []

    async def _safe_vector_recall(self, user_id: str, query: str) -> list[MemoryHit]:
        """情节 + 知识 + 遗留事实: 一次 embedding、一次查询覆盖三个桶。"""
        settings = get_settings()
        kinds = [
            MemoryBucket.EPISODE.value,
            MemoryBucket.KNOWLEDGE.value,
            LEGACY_KIND_FACT,
        ]
        budget = (
            settings.memory_episode_top_k
            + settings.memory_knowledge_top_k
            + settings.long_term_memory_top_k
        )
        try:
            return await get_long_term_store().search_by_buckets(user_id, query, kinds, budget)
        except Exception as exc:  # noqa: BLE001 - PG/Embedding 不可用只是少了语义记忆
            logger.warning("个人记忆向量召回失败, 本轮跳过: %s", exc)
            return []

    def _split_vector_hits(
        self, hits: list[MemoryHit]
    ) -> tuple[list[MemoryHit], list[MemoryHit], list[MemoryHit]]:
        """按桶裁剪: 情节还要过时间窗 + 衰减重排, 知识/遗留事实按分数取 Top-K。"""
        settings = get_settings()
        now = datetime.now(timezone.utc)
        episodes: list[MemoryHit] = []
        knowledge: list[MemoryHit] = []
        legacy: list[MemoryHit] = []
        for hit in hits:
            if hit.kind == MemoryBucket.EPISODE.value:
                if len(episodes) < settings.memory_episode_top_k and self._episode_fresh(hit, now):
                    episodes.append(hit)
            elif hit.kind == MemoryBucket.KNOWLEDGE.value:
                if len(knowledge) < settings.memory_knowledge_top_k:
                    knowledge.append(hit)
            elif len(legacy) < settings.long_term_memory_top_k:
                legacy.append(hit)
        return episodes, knowledge, legacy

    def _episode_fresh(self, hit: MemoryHit, now: datetime) -> bool:
        """情节时间窗过滤 + 陈旧降权: 超出 ``episodic_window_days`` 的不带。

        新鲜度锚点取 ``occurred_at`` 与 ``created_at`` 中较新的一个: 发生日期是
        LLM 推算的, 猜错(如错年份)不应该让一条刚刚记下的经历整条被过滤掉。
        """
        settings = get_settings()
        stamps = [t for t in (hit.occurred_at, hit.created_at) if t is not None]
        if not stamps:
            return True
        stamp = max(stamps)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_days = (now - stamp).days
        if age_days > settings.episodic_window_days:
            return False
        # 未超窗但已过保鲜期: 降权排到后面(同分才按相似度), 让"最近发生的事"优先。
        if age_days > settings.memory_decay_days:
            hit.score *= 0.85
        return True

    async def _safe_graph(self, user_id: str, query: str) -> list[str]:
        """Graph 通道没有"查询 -> 实体"的反向索引, 用查询文本里出现的已有实体名做起点。"""
        if not get_settings().graph_memory_enabled:
            return []
        try:
            names = await graph_store.entity_names(user_id)
            candidates = [name for name in names if name and name in query]
            if not candidates:
                return []
            return await graph_store.related_facts(user_id, candidates, hops=_GRAPH_HOPS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("个人图谱检索失败, 本轮跳过: %s", exc)
            return []

    async def _safe_legacy_facts(self, user_id: str, query: str) -> list[MemoryHit]:
        try:
            rows = await get_long_term_store().search_memories(user_id, query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("长期记忆 Vector 通道检索失败, 本轮跳过: %s", exc)
            return []
        return [
            MemoryHit(id=0, content=content, kind=kind, score=score)
            for content, kind, score in rows
        ]

    async def _touch(self, memory_ids: list[int]) -> None:
        """召回命中即"又用了一次"; 失败只是少了淘汰依据, 不影响本轮。"""
        ids = [i for i in memory_ids if i]
        if not ids:
            return
        try:
            await get_long_term_store().touch(ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("记忆 last_accessed_at 刷新失败(忽略): %s", exc)

    # ------------------------------------------------------------ 写路径

    async def write(
        self,
        user_id: str,
        session_id: str,
        extraction: MemoryExtraction,
        *,
        source: str = SOURCE_TURN,
    ) -> dict[str, int]:
        """把一次提取的结果分桶落盘, 返回各桶写入条数(供审计)。"""
        settings = get_settings()
        stats: dict[str, int] = {}
        if not user_id or extraction.is_empty or not settings.long_term_memory_enabled:
            return stats
        store = get_long_term_store()
        if not settings.personal_memory_enabled:
            written = 0
            for fact in extraction.facts:
                await store.upsert_memory(
                    user_id, fact, kind=LEGACY_KIND_FACT, source_session_id=session_id, source=source
                )
                written += 1
            stats[LEGACY_KIND_FACT] = written
            await graph_store.upsert_entities(
                user_id, extraction.entities, extraction.relations, source=source
            )
            return stats

        profile_result = await get_profile_store().merge(user_id, extraction.profile)
        stats["profile_added"] = int(profile_result.get("added", 0))
        stats["profile_updated"] = int(profile_result.get("updated", 0))
        for bucket, items in (
            (MemoryBucket.PREFERENCE, [("", text) for text in extraction.preferences]),
            (MemoryBucket.HABIT, [("", text) for text in extraction.habits]),
            (
                MemoryBucket.EPISODE,
                [(episode.title, episode.content, episode.occurred_at) for episode in extraction.episodes],
            ),
            (MemoryBucket.KNOWLEDGE, [(item.topic, item.content) for item in extraction.knowledge]),
        ):
            stats[bucket.value] = await self._upsert_bucket(
                store, user_id, session_id, bucket, items, source=source
            )
        await graph_store.upsert_entities(
            user_id, extraction.entities, extraction.relations, source=source
        )
        stats["reflected"] = await self.reflect(user_id)
        return stats

    async def _upsert_bucket(
        self,
        store,
        user_id: str,
        session_id: str,
        bucket: MemoryBucket,
        items: Sequence[tuple],
        *,
        source: str,
    ) -> int:
        """逐条落一个桶; 单条失败只少这一条, 不影响同桶其余条目。

        ``items`` 元素是 ``(title, content)`` 或 ``(title, content, occurred_at)``
        (只有情节带时间锚点), 用位置而不是 dataclass 是为了不让四个桶各定一个类型。
        """
        written = 0
        for item in items:
            title, content = item[0], item[1]
            occurred_at = item[2] if len(item) > 2 else None
            if not content.strip():
                continue
            try:
                await store.upsert_memory(
                    user_id,
                    content,
                    kind=bucket.value,
                    source_session_id=session_id,
                    title=title,
                    source=source,
                    occurred_at=occurred_at,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 - 单条写失败不值得丢掉整桶
                logger.warning("记忆桶 %s 写入失败, 跳过该条: %s", bucket.value, exc)
        return written

    async def add_session_episode(self, user_id: str, session_id: str, summary: str) -> bool:
        """Session -> Episodic: 会话摘要折叠成一条情节记忆。"""
        settings = get_settings()
        if (
            not user_id
            or not summary.strip()
            or not settings.long_term_memory_enabled
            or not settings.personal_memory_enabled
        ):
            return False
        try:
            await get_long_term_store().upsert_memory(
                user_id,
                summary.strip(),
                kind=MemoryBucket.EPISODE.value,
                source_session_id=session_id,
                title="会话摘要",
                source=SOURCE_SESSION_SUMMARY,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话摘要沉淀为情节失败, 本轮跳过: %s", exc)
            return False

    async def reflect(self, user_id: str, *, force: bool = False) -> int:
        """情节 -> 个人知识蒸馏: 攒够门槛条数才调一次 LLM, 返回新增知识条数。

        不做门槛就会每轮都拿近期情节再跑一次蒸馏, 既烧 token 又会把同一条经验
        反复写回(查重相似度不到阈值时就是重复行)。水位线存在画像行上, 画像行不
        存在时 ``mark_reflected`` 会补一行, 不需要额外的状态表。
        """
        settings = get_settings()
        if not user_id or not settings.long_term_memory_enabled:
            return 0
        store = get_long_term_store()
        profile_store = get_profile_store()
        try:
            since = None if force else await profile_store.get_last_reflected_at(user_id)
            pending = await store.count_since(user_id, MemoryBucket.EPISODE.value, since)
            if not force and pending < settings.memory_reflect_min_episodes:
                return 0
            episodes = await store.list_recent(
                user_id,
                [MemoryBucket.EPISODE.value],
                limit=_REFLECT_EPISODE_LIMIT,
                order_by="created_at",
            )
            if not episodes:
                await profile_store.mark_reflected(user_id)
                return 0
            lines = [
                f"- {(_iso(hit.occurred_at or hit.created_at) or '')[:10]} {hit.title}: {hit.content}"
                for hit in episodes
            ]
            resp = await _reflector().ainvoke(EPISODE_REFLECT_PROMPT.format(episodes="\n".join(lines)))
            data = json.loads(str(resp.content))
            items = data.get("knowledge") if isinstance(data, dict) else None
            added = await self._upsert_bucket(
                store,
                user_id,
                "",
                MemoryBucket.KNOWLEDGE,
                [
                    (str(entry.get("topic") or "")[:60], str(entry.get("content") or "").strip())
                    for entry in (items or [])
                    if isinstance(entry, dict) and str(entry.get("content") or "").strip()
                ],
                source=SOURCE_REFLECTION,
            )
            await profile_store.mark_reflected(user_id)
            return added
        except Exception as exc:  # noqa: BLE001 - 蒸馏失败下次攒够情节再试
            logger.warning("情节蒸馏失败, 本轮跳过: %s", exc)
            return 0

    # ------------------------------------------------------------ 管理视图

    async def overview(self, user_id: str) -> dict[str, Any]:
        """记忆管理页的数据源: 画像 + 各桶明细 + 图谱 + 计数概览。"""
        buckets = [
            MemoryBucket.PREFERENCE,
            MemoryBucket.HABIT,
            MemoryBucket.EPISODE,
            MemoryBucket.KNOWLEDGE,
        ]
        profile: dict[str, Any] = {"attributes": {}, "summary": "", "updated_at": None}
        items_by_kind: dict[str, list[dict[str, Any]]] = {}
        graph = {"nodes": [], "links": [], "paths": []}
        counts: dict[str, int] = {}
        try:
            profile = await get_profile_store().get(user_id)
            for bucket in buckets:
                hits = await get_long_term_store().list_recent(
                    user_id, [bucket.value], limit=_OVERVIEW_LIMIT, order_by="created_at"
                )
                items_by_kind[bucket.value] = [_item_dict(h) for h in hits]
            legacy_hits = await get_long_term_store().list_recent(
                user_id, [LEGACY_KIND_FACT], limit=_OVERVIEW_LIMIT, order_by="created_at"
            )
            items_by_kind[LEGACY_KIND_FACT] = [_item_dict(h) for h in legacy_hits]
            counts = await get_long_term_store().count_by_kind(user_id)
            if get_settings().graph_memory_enabled:
                graph = await graph_store.user_subgraph(user_id)
        except Exception as exc:  # noqa: BLE001 - 记忆页读不到就展示已拿到的部分
            logger.warning("个人记忆概览读取失败, 返回部分结果: %s", exc)
        attrs = profile.get("attributes") or {}
        labels = {bucket.value: spec_of(bucket).label for bucket in buckets}
        labels[LEGACY_KIND_FACT] = label_of(LEGACY_KIND_FACT)
        return {
            "user_id": user_id,
            "profile": profile,
            "buckets": items_by_kind,
            "graph": graph,
            "labels": labels,
            "stats": {
                "profile_keys": len(attrs),
                **{kind: int(counts.get(kind, 0)) for kind in items_by_kind},
                "graph_entities": len(graph.get("nodes") or []),
            },
        }

    async def delete(self, user_id: str, item_id: int) -> bool:
        if not user_id or not item_id:
            return False
        try:
            return (await get_long_term_store().delete_items(user_id, [item_id])) > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆删除失败: id=%s err=%s", item_id, exc)
            return False

    async def clear(self, user_id: str, bucket: MemoryBucket | str) -> int:
        """清空一个桶; 画像没有逐条粒度, 只能整行删除。"""
        if not user_id:
            return 0
        key = bucket.value if isinstance(bucket, MemoryBucket) else str(bucket)
        try:
            if key == MemoryBucket.PROFILE.value:
                return 1 if await get_profile_store().clear(user_id) else 0
            return await get_long_term_store().clear_bucket(user_id, key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆桶清空失败: bucket=%s err=%s", key, exc)
            return 0


_personal_agent: PersonalMemoryAgent | None = None


def get_personal_agent() -> PersonalMemoryAgent:
    """进程级单例个人记忆编排。"""
    global _personal_agent
    if _personal_agent is None:
        _personal_agent = PersonalMemoryAgent()
    return _personal_agent
