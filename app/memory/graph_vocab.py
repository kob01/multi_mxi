"""个人图谱的受控词表与写入口径: 什么实体、什么关系才配进"以用户为中心"的图。

定位与 ``app/kg/vocab.py`` 对称(那边管文档知识图谱, 这边管个人图谱), 枚举与判定
只在代码里定义一份, 提示词、落库、清理脚本、前端标签都从这里取口径。

立这份口径的原因是实测到的四类污染(单用户 49 个节点里 26 个是孤立点):

1. **提到即建点** —— 旧写入把 ``entities`` 照单全收挂到 ``:MemoryUser`` 锚点上, 于是
   联网检索回来的公共知识(运动员、计时网站、马拉松赛)也成了"用户的实体"。
2. **对话产物入图** —— 助手生成的报告标题、``webdocgen-20260928-195344`` 这类产物编号
   被当成 document 节点; 它们属于会话记录, 不属于人的关系网。
3. **关系词不受控** —— "获得2026年柏林马拉松男子冠军"、"报销情况展示于"、"涉及" 这种
   整句被写进 ``relation``, 边类型不可枚举, 单值关系的失效判定也永远命中不了。
4. **话题/文档当实体** —— 单板滑雪、Eval、差旅报销规定 这类内容各有归属: 兴趣进偏好桶、
   经验进知识桶、文件是对话产物, 都不该占图谱节点。

因此四条硬口径(全部是可离线判定的纯函数):

- 实体类型白名单: 人 / 部门 / 组织 / 系统 / 职位 / 地点; 明确标成 topic·document 的不入库;
- 关系词受控: 表外说法宁可不写(**不做** kg 侧那种"归到兜底词"处理 —— 个人图谱里一堆
  "相关"边同样是污染, 丢掉比留下准);
- 对话产物名(文件名/产物编号/单号/《报告》标题)一律不进图;
- 边必须锚定在用户身上: 至少一端是"我"(姓名/工号归一而来)或该用户图里**已存在**的节点,
  并且只为最终保留的边建点 —— 孤立节点从写入侧根除。

方向口径: 上下级/亲属这类词不做互逆翻转("王总是我的上级"与"我的上级是王总"两种写法
都会出现, 翻转规则无法区分), 只按同义映射统一到规范词, 方向交给提示词约定"src 是主体"。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.memory.taxonomy import is_conversation_product

# 用户自己的节点统一叫这个名字: 姓名/工号/"本人"都归一到它, 否则同一个人会在图里
# 裂成"我"与"朱斌"两个中心, 锚定判定也会因为写法不同而漏边。
SELF_NODE = "我"
# 只收"肯定是指说话人"的词: 不把"用户"这类行业词放进来(它可能真是一个实体名)。
_SELF_ALIASES = {"我", "本人", "自己", "我自己", "me", "i"}

# 允许进图的实体类型(前端图谱页的中文标签与此一一对应)。
ENTITY_TYPES: tuple[str, ...] = (
    "person",
    "department",
    "organization",
    "system",
    "position",
    "place",
)
# 明确标成这些类型 = 模型告诉我们"这不是关系网素材", 整条拦掉。
REJECTED_TYPES: frozenset[str] = frozenset({"topic", "document", "policy", "term", "concept"})
# 关系端点没被列进 entities 时的类型(小模型常见写法): 允许建点, 只影响展示配色。
UNKNOWN_TYPE = "unknown"

# 类型同义/中英归一: 模型爱写 company/school/城市 这类集合外的词。
_TYPE_SYNONYMS: dict[str, str] = {
    "org": "organization",
    "organisation": "organization",
    "company": "organization",
    "corp": "organization",
    "university": "organization",
    "school": "organization",
    "college": "organization",
    "institution": "organization",
    "公司": "organization",
    "企业": "organization",
    "单位": "organization",
    "机构": "organization",
    "学校": "organization",
    "院校": "organization",
    "组织": "organization",
    "tool": "system",
    "platform": "system",
    "software": "system",
    "app": "system",
    "系统": "system",
    "平台": "system",
    "工具": "system",
    "软件": "system",
    "job": "position",
    "title": "position",
    "role": "position",
    "岗位": "position",
    "职务": "position",
    "职称": "position",
    "city": "place",
    "location": "place",
    "address": "place",
    "venue": "place",
    "地点": "place",
    "城市": "place",
    "地区": "place",
    "省份": "place",
    "国家": "place",
    "场馆": "place",
    "场所": "place",
    "人物": "person",
    "人": "person",
    "员工": "person",
    "人员": "person",
    "部门": "department",
    "团队": "department",
    "科室": "department",
    "group": "department",
    "dept": "department",
}

# 受控关系词表: 个人图谱的边类型必须可枚举, 否则失效规则(单值关系)与前端展示都失效。
RELATION_VOCAB: tuple[str, ...] = (
    # 组织与岗位
    "任职于",
    "属于",
    "汇报给",
    "管理",
    "担任",
    "负责",
    "协作",
    "审批",
    "使用",
    "毕业于",
    "就读于",
    # 地点
    "现居",
    "老家",
    "常去",
    # 人际
    "家人",
    "配偶",
    "恋人",
    "前任",
    "朋友",
    "同学",
    "同事",
    "教练",
    "医生",
    "老师",
)

# 同义映射(不翻转端点): 只收"确定同义"的词, 拿不准的一律不进表(表外即拦)。
_RELATION_SYNONYMS: dict[str, str] = {
    # 任职于
    "就职于": "任职于",
    "入职": "任职于",
    "入职于": "任职于",
    "供职于": "任职于",
    "工作于": "任职于",
    "服务于": "任职于",
    "在任": "任职于",
    "效力于": "任职于",
    # 属于(部门)
    "所在部门": "属于",
    "部门": "属于",
    "归属": "属于",
    "归属于": "属于",
    "隶属": "属于",
    "隶属于": "属于",
    "属于部门": "属于",
    # 汇报给
    "上级": "汇报给",
    "直属上级": "汇报给",
    "汇报对象": "汇报给",
    "汇报": "汇报给",
    # 管理
    "下属": "管理",
    "下级": "管理",
    "带领": "管理",
    "领导": "管理",
    # 担任
    "职位": "担任",
    "岗位": "担任",
    "职务": "担任",
    "任": "担任",
    "担任职位": "担任",
    # 负责
    "分管": "负责",
    "牵头": "负责",
    "负责事务": "负责",
    "维护": "负责",
    "运维": "负责",
    # 协作
    "合作": "协作",
    "对接": "协作",
    "协作于": "协作",
    # 审批
    "审批人": "审批",
    "审核": "审批",
    # 使用
    "常用": "使用",
    "使用系统": "使用",
    "操作": "使用",
    "依赖": "使用",
    # 学校
    "母校": "毕业于",
    "毕业": "毕业于",
    "就读": "就读于",
    "上学于": "就读于",
    # 地点
    "住在": "现居",
    "居住于": "现居",
    "定居": "现居",
    "定居于": "现居",
    "所在地": "现居",
    "现居地": "现居",
    "家乡": "老家",
    "籍贯": "老家",
    "经常去": "常去",
    "常去地": "常去",
    "喜欢去": "常去",
    # 亲属与人际
    "父亲": "家人",
    "母亲": "家人",
    "爸爸": "家人",
    "妈妈": "家人",
    "儿子": "家人",
    "女儿": "家人",
    "哥哥": "家人",
    "弟弟": "家人",
    "姐姐": "家人",
    "妹妹": "家人",
    "爷爷": "家人",
    "奶奶": "家人",
    "外公": "家人",
    "外婆": "家人",
    "叔叔": "家人",
    "阿姨": "家人",
    "舅舅": "家人",
    "姑姑": "家人",
    "亲属": "家人",
    "老婆": "配偶",
    "老公": "配偶",
    "妻子": "配偶",
    "丈夫": "配偶",
    "爱人": "配偶",
    "女朋友": "恋人",
    "男朋友": "恋人",
    "女友": "恋人",
    "男友": "恋人",
    "对象": "恋人",
    "前女友": "前任",
    "前男友": "前任",
    "初恋": "前任",
    "好友": "朋友",
    "闺蜜": "朋友",
    "哥们": "朋友",
    "校友": "同学",
    "导师": "老师",
}

# 单值关系(函数式): 同一个 (主体, 关系) 现实上只能成立一个客体, 新事实要把旧边
# 打失效而不是并存。只列真正函数式的关系词: "负责"/"管理"/"审批" 天然多值, 不进表
# (宁可漏失效不可错失效)。
SINGLE_VALUED_RELATIONS: frozenset[str] = frozenset(
    {"任职于", "属于", "汇报给", "担任", "现居", "老家", "配偶", "恋人", "毕业于"}
)

# 实体名长度上限: 超过基本是整句(小模型会把"用户负责的系统开发工作"整个塞进 name)。
MAX_NAME_CHARS = 24

# 对话产物的形态特征: 文件名 / 产物编号(webdocgen-20260928-195344) / 业务单号(BX2026...)。
# 类型标注已经能拦掉大部分(document 被拒), 这一层兜住"没标类型的端点"。
_ARTIFACT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\.(xlsx|xls|docx|doc|pptx|ppt|pdf|csv|md|zip|png|html)\b", re.I),
    re.compile(r"[A-Za-z][A-Za-z_-]*-\d{6,}"),
    re.compile(r"[A-Z]{2,}\d{6,}"),
)

_WS_RE = re.compile(r"\s+")
_NAME_TAIL_PUNCT_RE = re.compile(r"[。；;，,、.!！?？]+$")


def clean_name(raw: object) -> str:
    """实体名归一: 全角半角统一、压缩空白、去尾随标点, 并折叠成"我"。

    书名号在这里**保留**(与 kg 侧不同): 它是"这是个文件/报告标题"的判据之一, 只在
    产物拦截里用, 归一后仍会带着走下一步。
    """
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    text = _WS_RE.sub(" ", text).strip()
    text = _NAME_TAIL_PUNCT_RE.sub("", text).strip()
    return fold_self(text)


def fold_self(name: str) -> str:
    """用户自己的各种写法 -> 统一节点名"我"。"""
    if not name:
        return ""
    lowered = name.lower()
    return SELF_NODE if name in _SELF_ALIASES or lowered in _SELF_ALIASES else name


def looks_like_artifact(raw: object) -> bool:
    """名字是否更像本轮对话的产物/文件/单号, 而不是一个可指向的实体。"""
    text = str(raw or "").strip()
    if not text:
        return False
    if "《" in text or "》" in text:
        return True
    return any(pattern.search(text) for pattern in _ARTIFACT_RES)


def entity_type(raw: object) -> str | None:
    """实体类型收敛: 返回可用类型; ``None`` 表示"明确不该进图"。

    未标类型/未知类型给 ``unknown``(允许建点, 只影响配色), 只有模型明确说"这是
    topic/document/policy/term"才拦 —— 那类内容在记忆体系里另有归属桶。
    """
    value = unicodedata.normalize("NFKC", str(raw or "")).strip().lower()
    if not value:
        return UNKNOWN_TYPE
    value = _TYPE_SYNONYMS.get(value, value)
    if value in REJECTED_TYPES:
        return None
    return value if value in ENTITY_TYPES else UNKNOWN_TYPE


def relation_is_controlled(relation: object) -> bool:
    """规范词表内判定(供清理脚本/自检统计"还有多少边落在词表外")。"""
    return str(relation or "") in RELATION_VOCAB


def entity_type_hint() -> str:
    """给提示词用的实体类型枚举(避免提示词与代码各写一份而漂移)。"""
    return "/".join(ENTITY_TYPES)


def relation_hint() -> str:
    """给提示词用的关系词表片段。"""
    return "/".join(RELATION_VOCAB)


# 实体类型 -> 中文标签: 前端图谱页与审计展示都从这里取, 不再各自维护一份。
# 含几个"新写入不会再产生"的旧类型(document/topic/policy/term/other/entity): 存量
# 节点在清理前仍要能渲染成中文, 不能回到英文字面量。
_TYPE_LABELS: dict[str, str] = {
    "person": "人物",
    "department": "部门",
    "organization": "组织",
    "system": "系统",
    "position": "职位",
    "place": "地点",
    "document": "文档",
    "topic": "话题",
    "policy": "制度",
    "term": "术语",
    "other": "其他",
    "entity": "未分类",
    UNKNOWN_TYPE: "未分类",
}


def type_label(kind: str) -> str:
    """实体类型的中文口径。"""
    return _TYPE_LABELS.get(str(kind or ""), str(kind or UNKNOWN_TYPE))


def canonical_relation(raw: object) -> str | None:
    """关系词归一为规范词; 词表外(含整句描述)返回 ``None`` 表示这条边不要了。"""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    text = _WS_RE.sub("", text)
    if not text:
        return None
    lowered = text.lower()
    if text in RELATION_VOCAB:
        return text
    for source, target in _RELATION_SYNONYMS.items():
        if text == source or lowered == source.lower():
            return target
    return text if text in RELATION_VOCAB else None


@dataclass(frozen=True)
class GraphPlan:
    """语义过滤的中间结果(还没做"是否锚定在用户身上"的连通性判定)。"""

    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    # 被类型/产物规则明确否掉的名字: 端点哪怕没列进 entities, 也不能靠"未知类型"复活。
    rejected_names: frozenset[str] = frozenset()

    def endpoint_names(self) -> set[str]:
        return {name for row in self.relations for name in (row["src"], row["dst"])}

    def type_of(self, name: str) -> str:
        for entry in self.entities:
            if entry["name"] == name:
                return str(entry["type"])
        return UNKNOWN_TYPE


def plan(
    entities: Sequence[dict],
    relations: Sequence[dict],
    *,
    user_aliases: Iterable[str] = (),
) -> GraphPlan:
    """把 LLM 给的 entities/relations 过一遍语义口径(纯函数, 不碰图)。

    ``user_aliases`` 是该用户已知的自称(画像里的姓名、工号), 用于把这些端点折叠成
    "我" —— 折叠后"朱斌-毕业于-X"与"我-毕业于-X"才会并成同一条边。
    """
    aliases = {clean_name(a) for a in user_aliases if str(a or "").strip()}
    aliases.discard("")
    dropped: dict[str, int] = {}
    typed: dict[str, str] = {}
    rejected: set[str] = set()

    def reject(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    def blocked_endpoint(name: str) -> bool:
        """端点是否不该进图: 被实体规则否过、或名字本身就是产物/整句。

        必须对**未列进 entities 的端点**也生效: 小模型经常只写关系不列实体, 只查
        ``typed``/``rejected`` 会让"我-负责->《XX 报告》"这种边靠"未知类型"复活。
        """
        return (
            name in rejected
            or len(name) > MAX_NAME_CHARS
            or looks_like_artifact(name)
            or is_conversation_product(name)
        )

    for entry in entities if isinstance(entities, Sequence) else []:
        if not isinstance(entry, dict):
            continue
        raw_name = entry.get("name")
        name = clean_name(raw_name)
        if not name:
            continue
        if name in aliases:
            name = SELF_NODE
        if looks_like_artifact(raw_name):
            rejected.add(name)
            reject("artifact")
            continue
        if len(name) > MAX_NAME_CHARS:
            rejected.add(name)
            reject("too_long")
            continue
        etype = entity_type(entry.get("type"))
        if etype is None:
            rejected.add(name)
            reject("not_graph_type")
            continue
        typed[name] = etype

    kept: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in relations if isinstance(relations, Sequence) else []:
        if not isinstance(entry, dict):
            continue
        raw_src, raw_dst = entry.get("src"), entry.get("dst")
        src, dst = clean_name(raw_src), clean_name(raw_dst)
        if src in aliases:
            src = SELF_NODE
        if dst in aliases:
            dst = SELF_NODE
        if not src or not dst or src == dst:
            reject("bad_endpoint")
            continue
        relation = canonical_relation(entry.get("relation"))
        if not relation:
            reject("relation_off_vocab")
            continue
        if blocked_endpoint(src) or blocked_endpoint(dst):
            reject("artifact_endpoint")
            continue
        key = (src, relation, dst)
        if key in seen:
            continue
        seen.add(key)
        kept.append({**entry, "src": src, "dst": dst, "relation": relation})

    return GraphPlan(
        entities=[{"name": name, "type": etype} for name, etype in typed.items()],
        relations=kept,
        dropped=dropped,
        rejected_names=frozenset(rejected),
    )


def anchor(plan_result: GraphPlan, existing_names: Iterable[str] = ()) -> GraphPlan:
    """连通性口径: 只保留能挂到用户身上的边, 并把节点收敛到这些边的端点。

    "锚定"的判据是三者之一: 端点就是"我"; 端点已存在于该用户图里(不变式: 图里已有的
    节点都是当年通过本口径写入的, 天然锚定); 端点通过本批另一条已保留的边间接连上"我"
    (跑两遍是为了接住批内顺序颠倒的链, 如先写"研发部-使用->OA"再写"我-属于->研发部")。
    """
    anchored = {SELF_NODE} | {clean_name(name) for name in existing_names if str(name or "").strip()}
    kept: list[dict] = []
    pending = list(plan_result.relations)
    for _round in range(2):
        rest: list[dict] = []
        for row in pending:
            if row["src"] in anchored or row["dst"] in anchored:
                anchored.add(row["src"])
                anchored.add(row["dst"])
                kept.append(row)
            else:
                rest.append(row)
        pending = rest
        if not pending:
            break
    dropped = dict(plan_result.dropped)
    if pending:
        dropped["unanchored"] = dropped.get("unanchored", 0) + len(pending)
    names = {row["src"] for row in kept} | {row["dst"] for row in kept}
    entities = [entry for entry in plan_result.entities if entry["name"] in names]
    dropped["isolated"] = dropped.get("isolated", 0) + len(plan_result.entities) - len(entities)
    return GraphPlan(entities=entities, relations=kept, dropped=dropped, rejected_names=plan_result.rejected_names)
