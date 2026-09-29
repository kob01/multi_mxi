"""个人级记忆的分桶taxonomy: 桶语义的唯一事实源。

架构图里的 User Memory(profile / preference / habit)、Episodic Memory、Personal
Knowledge 落到实现上都是同一张 ``long_term_memories`` 表的 ``kind`` 取值, 但每个
桶的**注入方式**完全不同: 画像每轮全量带、偏好/习惯按最近使用直读、情节/知识按
语义召回。这些差异(以及中文标签、prompt 小节标题)全部声明在这里, 召回侧与展示侧
都从这里取, 避免同一份桶定义在 prompt 拼接、审计、前端接口三处各写一遍而漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MemoryBucket(str, Enum):
    """记忆桶; 枚举值即 ``long_term_memories.kind`` 列的取值。"""

    PROFILE = "profile"
    PREFERENCE = "preference"
    HABIT = "habit"
    EPISODE = "episode"
    KNOWLEDGE = "knowledge"


# 引入分桶前的历史 kind 取值: 仍可被召回/展示, 但新写入不再产生。
LEGACY_KIND_FACT = "fact"


@dataclass(frozen=True)
class BucketSpec:
    """一个桶的元信息。

    ``inject`` 决定读路径怎么取:
    - ``full``: 全量注入(画像, 一人一条, 不做检索);
    - ``list``: 标量直读, 按 last_accessed_at 取 Top-N(偏好/习惯, 与当轮问题
      措辞无关, 走 embedding 反而会因为问法不同而漏掉);
    - ``vector``: 语义召回(情节/知识, 与本轮 query 相关才值得进 prompt)。
    """

    bucket: MemoryBucket
    label: str
    inject: str
    top_k_key: str
    prompt_header: str
    time_scoped: bool = False
    order_by: str = "last_accessed_at"


BUCKET_SPECS: dict[MemoryBucket, BucketSpec] = {
    MemoryBucket.PROFILE: BucketSpec(
        bucket=MemoryBucket.PROFILE,
        label="画像",
        inject="full",
        top_k_key="",
        # 第二行是给模型的阅读口径: 画像里的值是"按生效时间派生的当前态", 避免模型
        # 把带日期的旧值(只会在用户明说过时间时才出现)当成现在的状况。
        prompt_header=(
            "[用户画像]\n"
            "(以下为当前态; 括号内是该值的生效时间; 同一属性另有历史值时以当前态为准)"
        ),
    ),
    MemoryBucket.PREFERENCE: BucketSpec(
        bucket=MemoryBucket.PREFERENCE,
        label="偏好",
        inject="list",
        top_k_key="memory_preference_top_k",
        prompt_header="[用户偏好]",
    ),
    MemoryBucket.HABIT: BucketSpec(
        bucket=MemoryBucket.HABIT,
        label="习惯",
        inject="list",
        top_k_key="memory_habit_top_k",
        prompt_header="[用户习惯]",
    ),
    MemoryBucket.EPISODE: BucketSpec(
        bucket=MemoryBucket.EPISODE,
        label="情节",
        inject="vector",
        top_k_key="memory_episode_top_k",
        prompt_header="[相关经历]",
        time_scoped=True,
        order_by="occurred_at",
    ),
    MemoryBucket.KNOWLEDGE: BucketSpec(
        bucket=MemoryBucket.KNOWLEDGE,
        label="知识",
        inject="vector",
        top_k_key="memory_knowledge_top_k",
        prompt_header="[个人知识]",
    ),
}

# 写入侧允许出现的桶(fact 只是历史遗留, 不接受新写入)。
WRITABLE_BUCKETS = tuple(BUCKET_SPECS)

# 实际落在 long_term_memories 里的 kind(profile 有独立表, 不在此列), 含 legacy fact:
# 召回/展示时按这组值做 IN 过滤, 不会误把 "profile" 当成一个查不到的桶。
MEMORY_ITEM_KINDS = tuple(
    [b.value for b in BUCKET_SPECS if b is not MemoryBucket.PROFILE] + [LEGACY_KIND_FACT]
)

# 允许被"清空"的桶: 五个记忆桶 + 遗留的 fact(前端要能清掉历史记录)。
CLEARABLE_KINDS = (MemoryBucket.PROFILE.value, *MEMORY_ITEM_KINDS)

# 写入来源: 对话轮提取 / 会话摘要折叠 / 情节蒸馏。
SOURCE_TURN = "turn"
SOURCE_SESSION_SUMMARY = "session_summary"
SOURCE_REFLECTION = "reflection"


def spec_of(bucket: MemoryBucket | str) -> BucketSpec:
    """按桶(或裸 kind 字符串)取元信息; 未知 kind 归入 KNOWLEDGE 的展示口径。"""
    key = bucket.value if isinstance(bucket, MemoryBucket) else str(bucket)
    try:
        return BUCKET_SPECS[MemoryBucket(key)]
    except ValueError:
        return BUCKET_SPECS[MemoryBucket.KNOWLEDGE]


def label_of(kind: str) -> str:
    """kind -> 中文标签; legacy ``fact`` 单独给名, 不走 spec_of 的兜底。"""
    if kind == LEGACY_KIND_FACT:
        return "事实"
    return spec_of(kind).label
