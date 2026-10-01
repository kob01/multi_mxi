"""文档知识图谱"连线质量"的离线单测: 不需要整栈, 不需要 LLM/Neo4j/网络。

跑法::

    uv run python -m scripts.test_kg_edges_offline

覆盖的是两类会让图上的线变得草率的判定, 全部是可离线算的纯函数:

1. ``extract._parse``: 同名多型实体只能解出一个类型(否则写侧按名字 MATCH 会连出
   笛卡尔积错边)、关系词归一到受控词表、互逆词翻转端点后与已有的规范边并成一条、
   自关系丢弃、条数硬截断、``kg_relation_vocab_enabled=false`` 时退回原词;
2. ``migrate_kg_edges._plan_rewrites``: 存量边归一后的"先合并再删旧"分组, 尤其
   **规范边自己必须留在组里不被删** —— 这是迁移最容易出的事故。

边级 ACL、随篇回收、MERGE 落库这些真依赖 Neo4j 的行为在容器里走整栈验证(README
文档知识图谱小节), 本脚本不碰库。
"""

from __future__ import annotations

import json
import sys

from app.config import get_settings
from app.kg import vocab
from app.kg.extract import _parse
from scripts.migrate_kg_edges import _plan_rewrites

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))


def _payload(entities: list[dict], relations: list[dict]) -> str:
    return json.dumps({"entities": entities, "relations": relations}, ensure_ascii=False)


def test_entity_identity() -> None:
    """同名多型只能解出一个类型, 且关系只连出一条(不产生多对多)。"""
    raw = _payload(
        [
            {"name": "财务制度", "type": "policy"},
            {"name": "财务制度", "type": "document"},
            {"name": "财务部", "type": "department"},
            {"name": "未知角色", "type": "whatever"},
        ],
        [{"src": "财务制度", "relation": "属于", "dst": "财务部"}],
    )
    kg = _parse(raw)
    types = {e["type"] for e in kg.entities if e["name"] == "财务制度"}
    check("同名多型解出唯一类型(policy 优先于 document)", types == {"policy"}, str(types))
    check(
        "关系只落一条且带两端类型",
        len(kg.relations) == 1
        and kg.relations[0]["src_type"] == "policy"
        and kg.relations[0]["dst_type"] == "department",
        str(kg.relations),
    )
    unknown = [e for e in kg.entities if e["name"] == "未知角色"]
    check("未知实体类型收敛到 other", bool(unknown) and unknown[0]["type"] == "other", str(unknown))


def test_name_normalize() -> None:
    """书名号/引号与尾部标点是装饰, 要去掉; 括号限定语是区分信息, 要保留。"""
    got = vocab.normalize_entity_name("《财务制度》。")
    check("书名号与尾部句号被剥掉", got == "财务制度", repr(got))
    got2 = vocab.normalize_entity_name("财务   制度(试行)")
    check("内部多余空格压缩但括号限定语保留", got2 == "财务 制度(试行)", repr(got2))


def test_relation_synonym() -> None:
    """同义关系词归一到规范词, 端点顺序不动。"""
    kg = _parse(
        _payload(
            [{"name": "员工", "type": "person"}, {"name": "财务部", "type": "department"}],
            [
                {"src": "员工", "relation": "隶属于", "dst": "财务部"},
                {"src": "员工", "relation": "归属于", "dst": "财务部"},
            ],
        )
    )
    relations = [r["relation"] for r in kg.relations]
    check("同义词归一并合并成一条边", bool(kg.relations) and relations == ["属于"], str(kg.relations))


def test_relation_inverse() -> None:
    """互逆词翻转端点后与规范方向的边并成一条。"""
    kg = _parse(
        _payload(
            [{"name": "财务制度", "type": "policy"}, {"name": "报销条款", "type": "policy"}],
            [
                {"src": "财务制度", "relation": "包含", "dst": "报销条款"},
                {"src": "报销条款", "relation": "属于", "dst": "财务制度"},
            ],
        )
    )
    check(
        "包含 翻成 属于 并与已有规范边合并(只剩一条)",
        len(kg.relations) == 1
        and kg.relations[0]["relation"] == "属于"
        and kg.relations[0]["src"] == "报销条款",
        str(kg.relations),
    )


def test_self_relation_dropped() -> None:
    kg = _parse(
        _payload(
            [{"name": "财务部", "type": "department"}],
            [{"src": "财务部", "relation": "属于", "dst": "财务部"}],
        )
    )
    check("自关系被丢弃(前端画不成有意义的线)", kg.relations == [], str(kg.relations))


def test_uncontrolled_word_fallback() -> None:
    kg = _parse(
        _payload(
            [{"name": "A", "type": "system"}, {"name": "B", "type": "system"}],
            [{"src": "A", "relation": "在特定场景下可能会被部分调用", "dst": "B"}],
        )
    )
    check("词表外的关系词归入兜底词", bool(kg.relations) and kg.relations[0]["relation"] == "相关", str(kg.relations))


def test_relation_cap() -> None:
    """条数上限按代码侧硬截断, 提示词里的数字不算数。"""
    cap = get_settings().kg_max_relations_per_doc
    entities = [{"name": f"实体{i}", "type": "term"} for i in range(cap + 20)]
    relations = [
        {"src": f"实体{i}", "relation": "依赖", "dst": f"实体{i + 1}"} for i in range(cap + 20)
    ]
    kg = _parse(_payload(entities, relations))
    check(f"关系条数截断到 kg_max_relations_per_doc={cap}", len(kg.relations) == cap, str(len(kg.relations)))


def test_vocab_switch_off() -> None:
    """回退闸: 关掉词表后关系词原样入库, 也不翻端点。"""
    settings = get_settings()
    old = settings.kg_relation_vocab_enabled
    settings.kg_relation_vocab_enabled = False
    try:
        kg = _parse(
            _payload(
                [{"name": "财务制度", "type": "policy"}, {"name": "报销条款", "type": "policy"}],
                [{"src": "财务制度", "relation": "包含", "dst": "报销条款"}],
            )
        )
        check(
            "词表关闭时保留原关系词与原方向",
            bool(kg.relations)
            and kg.relations[0]["relation"] == "包含"
            and kg.relations[0]["src"] == "财务制度",
            str(kg.relations),
        )
    finally:
        settings.kg_relation_vocab_enabled = old


def _edge(eid: str, src: str, dst: str, relation: str, docs: list[str] | None = None) -> dict:
    return {
        "eid": eid,
        "src": src,
        "dst": dst,
        "src_name": f"name-{src}",
        "src_type": "policy",
        "dst_name": f"name-{dst}",
        "dst_type": "term",
        "relation": relation,
        "docs": docs if docs is not None else ["doc-a"],
        "evidence": "",
    }


def test_migration_grouping() -> None:
    """存量边归一分组: 规范边自己不能被删, 其余汇入的旧边才要删。"""
    rows = [
        _edge("e1", "A", "B", "属于"),                # 已经是规范形式
        _edge("e2", "B", "A", "包含", ["doc-b"]),      # 互逆词, 翻转后与 e1 同一条
        _edge("e3", "C", "D", "隶属于", ["doc-c"]),    # 同义词, 没有对应的规范边
        _edge("e4", "E", "F", "依赖"),                # 规范且无人汇入: 不需要动
    ]
    groups = _plan_rewrites(rows)
    by_key = {(g["src"], g["relation"], g["dst"]): g for g in groups}

    check("本来就是规范形式且无人汇入的组不参与改写", ("E", "依赖", "F") not in by_key, str(list(by_key)))
    ab = by_key.get(("A", "属于", "B"))
    check(
        "翻转后的组保留规范边 e1、只删 e2",
        ab is not None and ab["target_eid"] == "e1" and ab["delete_eids"] == ["e2"],
        str(ab),
    )
    check(
        "同义词组没有规范边时建新删旧",
        ("C", "属于", "D") in by_key
        and by_key[("C", "属于", "D")]["target_eid"] == ""
        and by_key[("C", "属于", "D")]["delete_eids"] == ["e3"],
        str(by_key.get(("C", "属于", "D"))),
    )
    check(
        "合并后的 docs 取并集",
        ab is not None and set(ab["docs"]) == {"doc-a", "doc-b"},
        str(ab["docs"] if ab else None),
    )


def main() -> int:
    test_entity_identity()
    test_name_normalize()
    test_relation_synonym()
    test_relation_inverse()
    test_self_relation_dropped()
    test_uncontrolled_word_fallback()
    test_relation_cap()
    test_vocab_switch_off()
    test_migration_grouping()

    failed = 0
    for ok, name, detail in _results:
        print(f"  [{PASS if ok else FAIL}] {name}" + ("" if ok else f"  -> {detail}"))
        failed += 0 if ok else 1
    print(f"\n{len(_results) - failed}/{len(_results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
