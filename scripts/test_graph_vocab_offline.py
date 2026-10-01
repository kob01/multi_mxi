"""个人图谱写入口径的离线单测: 不连 Neo4j, 不调 LLM。

跑法::

    uv run python -m scripts.test_graph_vocab_offline

覆盖 ``app/memory/graph_vocab.py`` 的四条口径(全是纯函数判定):
  1. 实体类型白名单 + 类型同义归一(topic/document 明确不进图);
  2. 关系词受控(同义归一 + 表外词丢弃)与单值关系集合的一致性;
  3. 对话产物名(文件名/产物编号/单号/《报告》)不进图, 端点也一样;
  4. 锚定闭包: 边必须连得到"我"或该用户图里已有的节点, 孤立实体不建点。
"""

from __future__ import annotations

import sys

from app.assistant.prompts import MEMORY_EXTRACTION_PROMPT
from app.memory.graph_vocab import (
    ENTITY_TYPES,
    RELATION_VOCAB,
    SELF_NODE,
    SINGLE_VALUED_RELATIONS,
    anchor,
    canonical_relation,
    clean_name,
    entity_type,
    entity_type_hint,
    looks_like_artifact,
    plan,
    relation_hint,
)

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def test_entity_types() -> None:
    check("六类实体都能进图", all(entity_type(t) == t for t in ENTITY_TYPES), str(ENTITY_TYPES))
    check("学校归 organization", entity_type("university") == "organization")
    check("中文类型词也能认", entity_type("城市") == "place" and entity_type("部门") == "department")
    check("话题明确不进图", entity_type("topic") is None and entity_type("document") is None)
    check("未标类型给 unknown(不拦边)", entity_type("") == "unknown" and entity_type("whatever") == "unknown")


def test_relation_vocab() -> None:
    check("规范词原样通过", canonical_relation("任职于") == "任职于")
    check("同义写法归一", canonical_relation("就职于") == "任职于" and canonical_relation("直属上级") == "汇报给")
    check("亲属称谓归一", canonical_relation("女朋友") == "恋人" and canonical_relation("母亲") == "家人")
    check("整句描述被丢弃", canonical_relation("获得2026年柏林马拉松男子冠军") is None)
    check("表外短词被丢弃", canonical_relation("涉及") is None and canonical_relation("提供") is None)
    check("空关系词被丢弃", canonical_relation("") is None)
    check(
        "单值关系集合与词表一致(否则失效规则永不命中)",
        SINGLE_VALUED_RELATIONS <= set(RELATION_VOCAB),
        str(SINGLE_VALUED_RELATIONS - set(RELATION_VOCAB)),
    )


def test_artifact_names() -> None:
    check("文件名是产物", looks_like_artifact("男子100米历史前10好成绩.xlsx"))
    check("产物编号是产物", looks_like_artifact("webdocgen-20260928-195344"))
    check("业务单号是产物", looks_like_artifact("BX20260928001"))
    check("书名号标题是产物", looks_like_artifact("《Agent 实用技术发展调研报告》"))
    check("正常实体名不误伤", not looks_like_artifact("研发部") and not looks_like_artifact("江苏理工学院"))
    check("全角与尾随标点归一", clean_name("研发部。") == "研发部" and clean_name("ＡＰＰ") == "APP")


def test_plan_drops_world_knowledge_and_artifacts() -> None:
    """实测污染样本: 联网检索回来的运动员/网站/报告标题整批不该进图。"""
    planned = plan(
        [
            {"name": "博尔特", "type": "person"},
            {"name": "男子100米历史前10好成绩", "type": "document"},
            {"name": "alltime-athletics.com", "type": "system"},
            {"name": "BMW柏林马拉松", "type": "topic"},
            {"name": "webdocgen-20260928-195344", "type": "document"},
        ],
        [
            {"src": "博尔特", "relation": "保持", "dst": "男子100米历史前10好成绩"},
            {"src": "alltime-athletics.com", "relation": "提供", "dst": "男子100米历史前10好成绩"},
            {"src": "我", "relation": "负责", "dst": "《Agent 实用技术发展调研报告（6–8 月）》"},
            {"src": "博尔特", "relation": "属于", "dst": "BMW柏林马拉松"},
        ],
        user_aliases=["朱斌", "E90000"],
    )
    check("语义过滤后一条边都不剩", not planned.relations, str(planned.relations))
    check(
        "丢弃原因可观测",
        planned.dropped.get("relation_off_vocab") == 2 and planned.dropped.get("artifact_endpoint") == 2,
        str(planned.dropped),
    )
    check("被拦下的轮次不往图里写任何节点", not anchor(planned, ()).entities, str(planned.entities))


def test_plan_keeps_user_relations() -> None:
    """用户本人的一跳关系要完整保留, 且姓名折叠成"我"。"""
    planned = plan(
        [
            {"name": "朱斌", "type": "person"},
            {"name": "江苏理工学院", "type": "university"},
            {"name": "研发部", "type": "department"},
            {"name": "李总", "type": "person"},
            {"name": "HR系统", "type": "system"},
        ],
        [
            {"src": "朱斌", "relation": "毕业于", "dst": "江苏理工学院"},
            {"src": "我", "relation": "所在部门", "dst": "研发部"},
            {"src": "我", "relation": "直属上级", "dst": "李总"},
            {"src": "我", "relation": "常用", "dst": "HR系统"},
        ],
        user_aliases=["朱斌", "E90000"],
    )
    final = anchor(planned, ())
    paths = [f"{r['src']} -{r['relation']}-> {r['dst']}" for r in final.relations]
    check(
        "自述边全部保留且关系词归一",
        paths
        == [
            f"{SELF_NODE} -毕业于-> 江苏理工学院",
            f"{SELF_NODE} -属于-> 研发部",
            f"{SELF_NODE} -汇报给-> 李总",
            f"{SELF_NODE} -使用-> HR系统",
        ],
        str(paths),
    )
    types = {e["name"]: e["type"] for e in final.entities}
    check("学校节点类型是 organization", types.get("江苏理工学院") == "organization", str(types))
    check("用户自己只留一个中心节点", SELF_NODE in types and "朱斌" not in types, str(types))


def test_anchor_requires_connectivity() -> None:
    """第三方关系即使措辞规范也不进图; 跨轮的链式关系靠"图里已有"接上。"""
    planned = plan(
        [
            {"name": "Peter Larsson", "type": "person"},
            {"name": "mikatiming.com", "type": "system"},
            {"name": "研发部", "type": "department"},
            {"name": "OA系统", "type": "system"},
        ],
        [
            {"src": "Peter Larsson", "relation": "维护", "dst": "mikatiming.com"},
            {"src": "研发部", "relation": "使用", "dst": "OA系统"},
        ],
    )
    fresh = anchor(planned, ())
    check("图里什么都没有时, 第三方关系不建点", not fresh.relations and not fresh.entities, str(fresh.dropped))
    check(
        "两条第三方边全部被拦下(词表外或未锚定)",
        fresh.dropped.get("unanchored", 0) + fresh.dropped.get("relation_off_vocab", 0) == 2,
        str(fresh.dropped),
    )

    # 研发部已经通过上一轮的"我-属于->研发部"进了图 -> 这一轮的链式关系应当接上。
    chained = anchor(planned, {"研发部"})
    edges = [f"{r['src']} -{r['relation']}-> {r['dst']}" for r in chained.relations]
    check("一端已在图里的链式关系被保留", edges == ["研发部 -使用-> OA系统"], str(edges))
    check("链式关系的两端都建点", {e["name"] for e in chained.entities} == {"研发部", "OA系统"}, str(chained.entities))

    # 批内顺序颠倒的链: 先写"研发部-使用->OA", 再写"我-属于->研发部"。
    ordered = plan(
        [{"name": "研发部", "type": "department"}, {"name": "OA系统", "type": "system"}],
        [
            {"src": "研发部", "relation": "使用", "dst": "OA系统"},
            {"src": "我", "relation": "属于", "dst": "研发部"},
        ],
    )
    two_pass = anchor(ordered, ())
    check("两遍闭包接住批内倒序的链", len(two_pass.relations) == 2, str(two_pass.relations))


def test_isolated_entities_are_not_written() -> None:
    """只被提到、没有任何关系的实体不再挂到用户锚点上(旧版污染主因)。"""
    planned = plan(
        [
            {"name": "人事部", "type": "department"},
            {"name": "市场部", "type": "department"},
            {"name": "携程商旅", "type": "system"},
            {"name": "朱斌", "type": "person"},
        ],
        [{"src": "我", "relation": "任职于", "dst": "携程商旅"}],
        user_aliases=["朱斌"],
    )
    final = anchor(planned, ())
    check("节点只从保留的边端点产生", {e["name"] for e in final.entities} == {"我", "携程商旅"}, str(final.entities))
    check("被丢掉的孤立实体计数可见", final.dropped.get("isolated") == 2, str(final.dropped))


def test_prompt_renders_vocab() -> None:
    """提示词里的类型/关系枚举由 graph_vocab 渲染, 不能各写一份。"""
    text = MEMORY_EXTRACTION_PROMPT.format(
        message="m", answer="a", today="2026-09-30", existing="(无)",
        entity_types=entity_type_hint(), relation_words=relation_hint(),
    )
    check("提示词带全实体类型", all(t in text for t in ENTITY_TYPES))
    check("提示词带全关系词表", all(word in text for word in RELATION_VOCAB))
    check("提示词不再出现 document/topic 类型", "type(person/department/system/document" not in text)


def main() -> int:
    test_entity_types()
    test_relation_vocab()
    test_artifact_names()
    test_plan_drops_world_knowledge_and_artifacts()
    test_plan_keeps_user_relations()
    test_anchor_requires_connectivity()
    test_isolated_entities_are_not_written()
    test_prompt_renders_vocab()
    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
