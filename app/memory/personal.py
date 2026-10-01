"""个人级记忆的编排层 (架构图里的 Personal Agent 一层)。

把架构图对上代码:

    User -> PersonalMemoryAgent
             ├── Session Memory      app/assistant/memory.py (Redis, 本模块不碰)
             ├── User Memory         profile(user_profiles 表) + preference/habit
             ├── Episodic Memory     episode 桶(带时间锚点的经历)
             ├── Personal Knowledge  knowledge 桶(只由显式"记一下"指令写入)
             └── Personal Graph      Neo4j :MemoryUser 锚点 + 实体关系

两个入口对应读/写两条链路: ``build()`` 在 build_context 节点并行拉各桶拼成
Business Context, ``write()`` 在 persist_memory 节点把一次提取的结果分桶落盘;
知识桶另有唯一写入口 ``remember_knowledge()`` —— 用户显式说"记一下"这类指令
才写(同话题同视角更新既有行, 否则新增), 每轮自动提取与情节蒸馏双通道已下线。
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

from app.assistant.prompts import (
    MEMORY_CONSOLIDATE_PROMPT,
    MEMORY_RECORD_MERGE_PROMPT,
    MEMORY_RECORD_PROMPT,
)
from app.config import get_settings
from app.llm import get_chat_model
from app.memory import graph_store
from app.memory.extraction import MemoryExtraction
from app.memory.profile_store import get_profile_store
from app.memory.taxonomy import (
    LEGACY_KIND_FACT,
    SOURCE_SESSION_SUMMARY,
    SOURCE_TURN,
    MemoryBucket,
    label_of,
    spec_of,
)
from app.memory.temporal import format_day
from app.memory.vector_store import MemoryHit, get_long_term_store

logger = logging.getLogger(__name__)

# 记忆管理页一次拉的每桶条数(情节长得最快, 不做分页只做硬上限)。
_OVERVIEW_LIMIT = 100
# 喂给提取器判重的已存偏好/习惯上限: 比召回 Top-K 大得多, 目标是一次看全整桶。
_DEDUP_CONTEXT_LIMIT = 30
# 单次归并最多看多少条(超出硬上限不送 LLM, 避免 prompt 膨胀)。
_CONSOLIDATE_SCAN_LIMIT = 50
# 读路径的 Graph 邻居展开跳数与实体起点上限。
_GRAPH_HOPS = 2

_record_llm = None


def _record_model():
    """惰性建显式记录链路(提炼/归位判定)共用的 json_mode 模型实例(进程级单例)。"""
    global _record_llm
    if _record_llm is None:
        _record_llm = get_chat_model(get_settings().llm_model, temperature=0, json_mode=True)
    return _record_llm


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _same_topic(existing_title: str, topic: str) -> bool:
    """确定性话题候选: 规范化后全等/互含, 或字符 bigram 重合度达标即算"可能同话题"。

    这一步只做粗筛把候选交给归位 LLM, 不替代它下结论。纯子串太脆: 提炼模型常把
    同话题的承接句起成新主题词("崇礼滑雪板预订" vs "崇礼滑雪板头盔预订"), 互不含
    就永远不进候选; bigram 重合度能兜住这类同源主题词。阈值 0.5 偏保守: 中文主题
    词短, 不同话题("报销审批"/"住宿预订")重合度自然落在阈值下; 空主题不进候选。
    """
    a = (existing_title or "").strip().lower()
    b = (topic or "").strip().lower()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / max(1, len(ga | gb)) >= 0.5


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
            sections.append(spec.prompt_header + "\n" + "\n".join(self._line(hit, spec) for hit in hits))
        if self.graph:
            sections.append("[关联记忆]\n" + "\n".join(self.graph))
        if self.legacy:
            # 分桶之前的老记录仍以"[长期记忆]"小节带上, 不因为升级就失联。
            sections.append("[长期记忆]\n" + "\n".join(f"- {hit.content}" for hit in self.legacy))
        return "\n".join(sections)

    @staticmethod
    def _line(hit: MemoryHit, spec) -> str:
        """一条记忆 -> prompt 行。

        带时间锚点的桶(情节)额外前缀发生日期: 同一件事的不同时刻观测(如旧体重与
        新体重)会同时被召回, 不给日期模型就无从判断哪条是现在; 口径对齐业界
        "记忆逐条标日期, 冲突按日期取最新"。没日期的行保持原样, 不拿记录时间充数。
        """
        stamp = format_day(hit.occurred_at) if spec.time_scoped and hit.occurred_at else ""
        head = f"{stamp} " if stamp else ""
        body = f"{hit.title}：{hit.content}" if hit.title else hit.content
        return f"- {head}{body}"

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

    async def existing_stable_texts(self, user_id: str) -> tuple[list[str], list[str]]:
        """读回已存的偏好/习惯文本, 喂给提取器判重(标量直读, 零 embedding)。

        这里故意拿得比召回 Top-K 多(整个桶的上限): 目的是"告诉模型已经记过什么",
        只给最近 3 条会漏掉更早写入的重复项, 达不到防重复的效果。读失败退化为空
        (等于回到无状态提取的旧行为), 不抛出。
        """
        if not user_id or not get_settings().personal_memory_enabled:
            return [], []
        try:
            preferences, habits = await asyncio.gather(
                get_long_term_store().list_recent(
                    user_id, [MemoryBucket.PREFERENCE.value],
                    limit=_DEDUP_CONTEXT_LIMIT, order_by="created_at",
                ),
                get_long_term_store().list_recent(
                    user_id, [MemoryBucket.HABIT.value],
                    limit=_DEDUP_CONTEXT_LIMIT, order_by="created_at",
                ),
            )
            return [h.content for h in preferences], [h.content for h in habits]
        except Exception as exc:  # noqa: BLE001 - 拿不到已存项只是少了防重复提示
            logger.warning("读取已存偏好/习惯失败, 本轮提取不做判重: %s", exc)
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

    async def _user_self_aliases(self, user_id: str) -> list[str]:
        """该用户在图里的自称集合: 工号 + 画像里的姓名。

        图谱锚定判定要回答"这条边是不是连着用户自己", 而模型既可能写"我"也可能
        直接写姓名(提示词两种都允许); 不折叠就会让同一个人裂成两个中心节点, 而
        且"朱斌-毕业于-X"这种本该保留的自述边会被当成第三方关系误删。
        """
        names = [user_id] if user_id else []
        try:
            profile = await get_profile_store().get(user_id)
            raw = (profile.get("attributes") or {}).get("姓名")
            for value in raw if isinstance(raw, list) else [raw]:
                text = str(value.get("value") if isinstance(value, dict) else value).strip()
                if text and text not in names:
                    names.append(text)
        except Exception as exc:  # noqa: BLE001 - 拿不到姓名只是少一个别名, "我"仍然可用
            logger.debug("读画像姓名失败(按仅\"我\"可用处理): %s", exc)
        return names

    async def write(
        self,
        user_id: str,
        session_id: str,
        extraction: MemoryExtraction,
        *,
        source: str = SOURCE_TURN,
    ) -> dict[str, Any]:
        """把一次提取的结果分桶落盘, 返回各桶写入条数(供审计)。

        值的类型不齐一: 各桶是条数, ``graph`` 是 ``{nodes, edges, dropped}`` 一个子结
        构(图侧拦了多少、为何拦要能审计), 所以返回类型是 ``Any`` 而不是 ``int``。
        """
        settings = get_settings()
        stats: dict[str, Any] = {}
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
            stats["graph"] = await graph_store.upsert_entities(
                user_id,
                extraction.entities,
                extraction.relations,
                source=source,
                user_aliases=await self._user_self_aliases(user_id),
            )
            return stats

        profile_result = await get_profile_store().merge(user_id, extraction.profile)
        stats["profile_added"] = int(profile_result.get("added", 0))
        stats["profile_updated"] = int(profile_result.get("updated", 0))
        # 被拦下的历史陈述单独计数: "过去的事只入历史不改当前值"这个行为得可观测。
        stats["profile_superseded"] = int(profile_result.get("superseded", 0))
        # 知识桶不在这里写: 唯一入口是 remember_knowledge(显式"记一下"指令),
        # 情节 -> 知识的自动蒸馏也已下线。
        for bucket, items in (
            (MemoryBucket.PREFERENCE, [("", text) for text in extraction.preferences]),
            (MemoryBucket.HABIT, [("", text) for text in extraction.habits]),
            (
                MemoryBucket.EPISODE,
                [(episode.title, episode.content, episode.occurred_at) for episode in extraction.episodes],
            ),
        ):
            stats[bucket.value] = await self._upsert_bucket(
                store, user_id, session_id, bucket, items, source=source
            )
        stats["graph"] = await graph_store.upsert_entities(
            user_id,
            extraction.entities,
            extraction.relations,
            source=source,
            user_aliases=await self._user_self_aliases(user_id),
        )
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

    async def consolidate_stable_buckets(self, user_id: str) -> int:
        """偏好/习惯桶语义归并: 把存量重复条目合并成更完整的一条, 返回删除条数。

        提取器无状态时留下的历史重复(同一件事的不同说法)靠 cosine 阈值卡不住,
        这里用一次 LLM 把整桶交给模型分组, 只合并真正语义重复的组。只在手动
        "整理记忆"时跑(不在每轮写路径上), 因为它是整桶级别的 LLM 调用。
        """
        if not user_id or not get_settings().personal_memory_enabled:
            return 0
        store = get_long_term_store()
        merged = 0
        for bucket in (MemoryBucket.PREFERENCE, MemoryBucket.HABIT):
            try:
                hits = await store.list_recent(
                    user_id, [bucket.value], limit=_CONSOLIDATE_SCAN_LIMIT, order_by="created_at"
                )
                if len(hits) < 2:
                    continue
                merged += await self._consolidate_one_bucket(store, user_id, bucket, hits)
            except Exception as exc:  # noqa: BLE001 - 单桶归并失败不影响另一桶
                logger.warning("记忆桶 %s 归并失败, 跳过: %s", bucket.value, exc)
        return merged

    async def _consolidate_one_bucket(self, store, user_id: str, bucket, hits) -> int:
        """对一个桶的条目调一次 LLM 分组, 按组归并; 返回删除条数。"""
        by_id = {hit.id: hit for hit in hits}
        numbered = "\n".join(f"{i}. {hit.content}" for i, hit in enumerate(hits, start=1))
        resp = await _record_model().ainvoke(MEMORY_CONSOLIDATE_PROMPT.format(items=numbered))
        data = json.loads(str(resp.content))
        groups = data.get("groups") if isinstance(data, dict) else None
        merged = 0
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            content = str(group.get("content") or "").strip()
            idxs = group.get("ids")
            if not content or not isinstance(idxs, list):
                continue
            # 编号(1 基)映射回真实 id; 只认本次扫描到的条目, 越界编号直接忽略。
            ids = [
                hits[i - 1].id
                for i in idxs
                if isinstance(i, int) and 1 <= i <= len(hits)
            ]
            ids = [i for i in dict.fromkeys(ids) if i in by_id]
            if len(ids) < 2:
                continue
            # 保留原文最长的一条(留住它的 created_at/使用记录), 其余删除。
            keep_id = max(ids, key=lambda i: len(by_id[i].content))
            drop_ids = [i for i in ids if i != keep_id]
            try:
                merged += await store.merge_group(user_id, keep_id, content, drop_ids)
            except Exception as exc:  # noqa: BLE001 - 单组失败不影响其它组
                logger.warning("记忆组归并失败(跳过): bucket=%s ids=%s err=%s", bucket.value, ids, exc)
        return merged

    async def tidy(self, user_id: str) -> dict[str, int]:
        """手动"整理记忆": 只做偏好/习惯语义归并。

        情节 -> 知识的蒸馏已下线(知识桶只由显式"记一下"写入), 这里不再产生
        任何新知识条目。
        """
        merged = await self.consolidate_stable_buckets(user_id)
        return {"merged": merged}

    # ------------------------------------------------------ 显式知识记录

    async def remember_knowledge(
        self, user_id: str, session_id: str, message: str, answer: str
    ) -> dict[str, int]:
        """显式"记一下"指令的唯一写入口: 提炼 -> 整桶判同话题 -> 落盘。

        同话题且同视角才更新既有行(融合改写 + 重算 embedding), 其余一律新增;
        全新话题自然无候选, 不送归位 LLM。insert 仍过 ``upsert_memory`` 的 cosine
        查重, 近乎逐字的重复被免费兑掉。返回 ``{"inserted": a, "updated": b}``,
        任何失败只 warning 不抛出(与全层降级口径一致)。
        """
        stats = {"inserted": 0, "updated": 0}
        settings = get_settings()
        if (
            not user_id
            or not settings.long_term_memory_enabled
            or not settings.personal_memory_enabled
            or not settings.memory_record_enabled
        ):
            return stats
        store = get_long_term_store()
        try:
            existing = await store.list_recent(
                user_id, [MemoryBucket.KNOWLEDGE.value],
                limit=_DEDUP_CONTEXT_LIMIT, order_by="created_at",
            )
            items = await self._record_items(message, answer)
            for topic, content in items:
                candidates = [hit for hit in existing if _same_topic(hit.title, topic)]
                hit, merged_content = (
                    await self._decide_placement(candidates, topic, content) if candidates else (None, "")
                )
                if hit is not None:
                    merged_content = merged_content or self._fallback_merge(hit.content, content)
                    try:
                        if await store.update_memory_content(user_id, hit.id, merged_content, title=topic):
                            stats["updated"] += 1
                            # 整桶快照同步: 同轮多条新记录不能都去撞同一条旧行。
                            hit.content = merged_content
                            if topic.strip():
                                hit.title = topic.strip()
                            continue
                    except Exception as exc:  # noqa: BLE001 - 更新没落上就退化为新增一条
                        logger.warning("知识同话题更新失败, 退化为新增: id=%s err=%s", hit.id, exc)
                await store.upsert_memory(
                    user_id,
                    content,
                    kind=MemoryBucket.KNOWLEDGE.value,
                    source_session_id=session_id,
                    title=topic,
                    source=SOURCE_TURN,
                )
                stats["inserted"] += 1
            return stats
        except Exception as exc:  # noqa: BLE001 - 记录失败只是这轮没记上, 不阻断对话
            logger.warning("显式知识记录失败, 本轮跳过: %s", exc)
            return stats

    async def _record_items(self, message: str, answer: str) -> list[tuple[str, str]]:
        """一次 LLM 调用把"用户要我记的"提炼成 (topic, content) 列表; 失败返回空。"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            resp = await _record_model().ainvoke(
                MEMORY_RECORD_PROMPT.format(message=message, answer=answer, today=today)
            )
            data = json.loads(str(resp.content))
            raw = data.get("items") if isinstance(data, dict) else None
            return [
                (str(entry.get("topic") or "")[:60].strip(), str(entry.get("content") or "").strip())
                for entry in (raw or [])
                if isinstance(entry, dict) and str(entry.get("content") or "").strip()
            ]
        except Exception as exc:  # noqa: BLE001 - 提炼失败即本轮无显式记录
            logger.warning("显式记录提炼失败, 本轮跳过: %s", exc)
            return []

    async def _decide_placement(
        self, candidates: list[MemoryHit], topic: str, content: str
    ) -> tuple[MemoryHit | None, str]:
        """话题候选送归位 LLM 判 update/insert; 一次调用同时拿融合文本。

        只在明确 update 且编号有效时返回 (候选行, 融合后 content); 其余一律
        (None, "") 走新增 —— 判不了就新增, 宁可多一条不可错改既有知识。
        """
        numbered = "\n".join(
            f"{i}. {hit.title or '(无主题)'}: {hit.content}" for i, hit in enumerate(candidates, start=1)
        )
        try:
            resp = await _record_model().ainvoke(
                MEMORY_RECORD_MERGE_PROMPT.format(
                    existing=numbered, topic=topic or "(无主题)", content=content
                )
            )
            data = json.loads(str(resp.content))
            if not isinstance(data, dict) or str(data.get("action") or "") != "update":
                return None, ""
            idx = data.get("id")
            if not isinstance(idx, int) or not 1 <= idx <= len(candidates):
                return None, ""
            # 模型没给融合文本(或长到异常, 防它把整桶拄进来)时退回确定性拼接。
            merged = str(data.get("content") or "").strip()
            hit = candidates[idx - 1]
            if not merged or len(merged) > max(len(hit.content), len(content)) * 3:
                merged = self._fallback_merge(hit.content, content)
            return hit, merged
        except Exception as exc:  # noqa: BLE001
            logger.warning("知识归位判定失败, 按新增处理: %s", exc)
            return None, ""

    @staticmethod
    def _fallback_merge(old_content: str, new_content: str) -> str:
        """融合兑底: 新陈述已被旧文本涵盖就原样保留, 否则分号拼接, 信息不丢。"""
        if new_content in old_content:
            return old_content
        return f"{old_content}; {new_content}"

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
