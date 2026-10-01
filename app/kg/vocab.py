"""文档知识图谱的受控词表: 实体类型与关系词的唯一事实源。

图谱页"连线草率"的两个数据侧根因都收在这里:

1. **关系词不受控** —— 提示词原先只说"简短关系词", LLM 会对同一条关系写出
   "属于/隶属于/归属于/所在部门"四种说法。``KG_REL`` 的 MERGE 键含 ``relation``,
   于是同一对实体上挂出多条语义重复的边; 前端用直线渲染时它们几何完全重合,
   看上去就是一条线挂了一堆看不见的副本。
2. **同名实体的类型不确定** —— ``:KgEntity`` 的唯一键是 ``(name, type)``, 但 LLM
   在 relations 里只用**名字**引用端点。一个名字在图里挂着两个类型时, 按名字
   匹配会产生笛卡尔积错边(A(policy)→B 与 A(document)→B 一起连上)。所以写侧
   必须把名字解析成**唯一类型**, 解析规则也放在这里, 让抽取与落库共用一份口径。

与 ``app/memory/taxonomy.py`` 同样的定位: 枚举与归一函数只在代码里定义一份,
提示词、落库、前端配色/图例都从这里取口径。把词表做成环境变量会把"改词表"
变成宿主轨/容器轨双份同步问题, 故刻意不做成配置项 —— 配置里只留
``kg_relation_vocab_enabled`` 这个回退闸。
"""

from __future__ import annotations

import re
import unicodedata

# 实体类型枚举: 前端 ``ENTITY_COLORS``/``TYPE_LABELS`` 的配色与中文口径以这份为准,
# 增删取值时三处(这里、GraphView.vue 的配色表、下面的提示词片段)要一起改。
ENTITY_TYPES: tuple[str, ...] = (
    "person",
    "department",
    "system",
    "document",
    "policy",
    "term",
    "other",
)

# 类型缺失时的兜底值(与历史行为一致: 空类型写 "entity", 未知类型归 "other")。
FALLBACK_TYPE = "entity"
GENERIC_TYPE = "other"

# 同一个名字被抽出多个类型时按此序取一个确定类型: 越靠前越"硬"(可定位的真体),
# document 排在 policy 之后是因为"报销制度"这类名字当制度理解比当文档更有用。
TYPE_PRIORITY: tuple[str, ...] = (
    "person",
    "department",
    "system",
    "policy",
    "document",
    "term",
    GENERIC_TYPE,
    FALLBACK_TYPE,
)

# 兜底关系词: 词表外的说法一律归到它, 而不是像过去那样原样入库(原样入库 = 每篇
# 文档都可能造一个新关系词, 图的边类型不可枚举, 前端也解释不了)。
GENERIC_RELATION = "相关"

RELATION_VOCAB: tuple[str, ...] = (
    "属于",
    "包含",
    "依赖",
    "适用于",
    "审批",
    "负责",
    "引用",
    "产出",
    "约束",
    GENERIC_RELATION,
)

# 同义映射(不改变方向): 这些说法与规范词描述的是同一个谓词、同一组端点顺序。
# 只收"确定同义"的词; 拿不准的一律交给兜底词, 宁可粗不可错并。
RELATION_SYNONYMS: dict[str, str] = {
    "隶属": "属于",
    "隶属于": "属于",
    "归属于": "属于",
    "归属": "属于",
    "所在部门": "属于",
    "任职于": "属于",
    "就职于": "属于",
    "依赖于": "依赖",
    "需要": "依赖",
    "依靠": "依赖",
    "适用": "适用于",
    "适用范围": "适用于",
    "审核": "审批",
    "批准": "审批",
    "复核": "审批",
    "提及": "引用",
    "参考": "引用",
    "参见": "引用",
    "援引": "引用",
    "限制": "约束",
    "受制于": "约束",
    "产生": "产出",
    "生成": "产出",
    "输出": "产出",
    "分管": "负责",
    "牵头": "负责",
    "管理": "负责",
    # 历史边(词表上线前入库)的默认关系词就是这两个值, 归一后存量才能收敛。
    "related": GENERIC_RELATION,
    "关系": GENERIC_RELATION,
}

# 互逆映射: ``词 -> (规范词, 是否翻转端点)``。登记标准很窄 —— 只有"A 谓词 B"与
# "B 规范词 A"在语义上确实等价时才登记, 这样翻转方向合并才不会把"谁依赖谁"讲反。
# 正因如此, 计划里提到的"产出↔依赖"没有登记: 它们是同向的不同谓词, 不是互逆关系。
# 规范方向统一取"子 -> 父"(属于/依赖/引用/产出为常用方向), 存储时翻转掉"父 -> 子"写法。
RELATION_INVERSE: dict[str, tuple[str, bool]] = {
    "包含": ("属于", True),
    "被依赖": ("依赖", True),
    "支撑": ("依赖", True),
    "被引用": ("引用", True),
    "被适用于": ("适用于", True),
}

# 关系词长度上限: LLM 偶尔写出整句("属于本部门的员工需要"), 截断后归不进词表就走兜底。
_RELATION_MAX_CHARS = 12

_NAME_DROP_CHARS = "《》〈〉\"'‘’“”「」『』`\t\r\n"
_WS_RE = re.compile(r"\s+")
_NAME_TAIL_PUNCT_RE = re.compile(r"[。；;，,、.!！?？]+$")


def normalize_entity_name(raw: object) -> str:
    """实体名归一: 全角/半角统一、去书名号与引号、压缩内部空白、去尾随标点。

    括号里的限定语("财务制度(试行)")**保留**: 那是区分实体的信息, 去掉会把两回事并成
    一个节点。而书名号/引号只是行文装饰, 留着会让同一实体的名字对不上。
    """
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not text:
        return ""
    text = text.translate({ord(ch): None for ch in _NAME_DROP_CHARS if ch not in " \t"})
    text = _WS_RE.sub(" ", text).strip()
    return _NAME_TAIL_PUNCT_RE.sub("", text).strip()


def normalize_entity_type(raw: object) -> str:
    """实体类型收敛到枚举: 未知归 other, 空值归 entity(与历史写入口径一致)。"""
    value = unicodedata.normalize("NFKC", str(raw or "")).strip().lower()
    if not value:
        return FALLBACK_TYPE
    return value if value in ENTITY_TYPES else GENERIC_TYPE


def pick_type(types: object) -> str:
    """同名实体的多个候选类型里按 ``TYPE_PRIORITY`` 取一个确定类型。

    这是"按名字连边"能安全成立的前提: 一个名字只对应一个类型, 写侧 MATCH 才不会
    命中多个节点而长出笛卡尔积错边。
    """
    candidates = {normalize_entity_type(t) for t in (types or [])}
    for candidate in TYPE_PRIORITY:
        if candidate in candidates:
            return candidate
    return FALLBACK_TYPE


def normalize_relation(raw: object, *, enabled: bool = True) -> tuple[str, bool]:
    """关系词归一为 ``(规范词, 是否翻转端点)``; ``enabled=False`` 时退回原词不翻转。

    三级判定: 命中互逆表(翻转) > 命中同义表(不翻转) > 在词表内(原样) > 兜底词。
    """
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    text = _WS_RE.sub("", text)
    if len(text) > _RELATION_MAX_CHARS:
        text = text[:_RELATION_MAX_CHARS]
    if not text:
        return (GENERIC_RELATION if enabled else "related"), False
    lowered = text.lower()
    if not enabled:
        return text, False
    # 命中判定允许"原词或 lower 后的词", 取位也必须用**同一个**键: 旧写法条件里写
    # `lowered in RELATION_INVERSE` 却用 `text` 取字典, 一旦登记了含大小写差异的互逆
    # 词(如英文 contains)就是 KeyError。当前词表全中文故不可达, 但不能靠这个假设。
    inverse_key = text if text in RELATION_INVERSE else lowered
    if inverse_key in RELATION_INVERSE:
        canonical, flip = RELATION_INVERSE[inverse_key]
        return canonical, flip
    for source, target in RELATION_SYNONYMS.items():
        if text == source or lowered == source.lower():
            return target, False
    if text in RELATION_VOCAB:
        return text, False
    return GENERIC_RELATION, False


def relation_is_controlled(relation: str) -> bool:
    """规范词表内判定(供迁移脚本/自检统计"还有多少边落在词表外")。"""
    return relation in RELATION_VOCAB


def entity_type_hint() -> str:
    """给提示词用的类型枚举片段(避免提示词与代码各写一份而漂移)。"""
    return "/".join(ENTITY_TYPES)


def relation_vocab_hint() -> str:
    """给提示词用的关系词表片段。"""
    return "/".join(RELATION_VOCAB)
