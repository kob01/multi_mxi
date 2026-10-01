"""按 ``app/memory/graph_vocab.py`` 的口径清理个人图谱里的存量脏数据。

写入侧已经收口(见 ``graph_store.upsert_entities``), 但**存量不会自己变干净**:
实测单用户 49 个节点里 26 个是孤立点, 边上挂着"获得2026年柏林马拉松男子冠军"这类
整句关系词与报告标题/产物编号。本脚本用同一份口径把这些行删掉, 判据不另写一份。

删什么:
  1. 关系词在受控词表外的 ``:REL`` 边(整句描述、"涉及/提供"这类第三方谓词);
  2. 类型明确不该进图的节点(topic/document/policy/term)与产物名节点(文件名、
     产物编号、单号、《报告》标题)、名字长过上限的"整句节点";
  3. 前两步之后**没有任何 REL 边的孤立实体**(连同 ``MENTIONS`` 一起删) —— 这一条
     是旧"提到即建点"留下的主要污染。

跑法::

    uv run python -m scripts.clean_personal_graph                 # 只看报告, 不动数据
    uv run python -m scripts.clean_personal_graph --apply         # 全部用户执行删除
    uv run python -m scripts.clean_personal_graph --user E90000 --apply

默认只打印将要删什么(dry-run); ``--apply`` 才真删。Neo4j 走 compose 发布的 HTTP
端口(宿主 17474), 不需要容器内网络。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from app.memory.graph_vocab import (
    MAX_NAME_CHARS,
    UNKNOWN_TYPE,
    canonical_relation,
    entity_type,
    looks_like_artifact,
)
from app.memory.taxonomy import is_conversation_product

DEFAULT_URL = "http://localhost:17474/db/neo4j/tx/commit"


def _run(url: str, statement: str, params: dict | None = None, *, write: bool = False):
    body = json.dumps(
        {"statements": [{"statement": statement, "parameters": params or {}, "mode": "w" if write else "r"}]}
    ).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    payload = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    res = payload["results"][0]
    cols = res["columns"]
    return [dict(zip(cols, row["row"])) for row in res["data"]]


def should_drop_node(name: str, etype: str) -> str:
    """该节点是否该删; 返回原因, 空串表示保留。"""
    if looks_like_artifact(name):
        return "artifact"
    if len(name) > MAX_NAME_CHARS:
        return "too_long"
    if is_conversation_product(name):
        return "conversation_product"
    if entity_type(etype) is None:
        return "not_graph_type"
    return ""


def clean_user(url: str, uid: str, *, apply: bool) -> dict[str, int]:
    """清理一个用户的个人图谱, 返回各类删除计数。"""
    nodes = _run(
        url,
        "MATCH (u:MemoryUser {user_id: $uid})-[:MENTIONS]->(e:MemoryEntity) "
        "RETURN e.name AS name, e.type AS type",
        {"uid": uid},
    )
    edges = _run(
        url,
        "MATCH (a:MemoryEntity {user_id: $uid})-[r:REL]->(b:MemoryEntity {user_id: $uid}) "
        "RETURN a.name AS src, a.type AS src_type, r.relation AS relation, "
        "       b.name AS dst, b.type AS dst_type",
        {"uid": uid},
    )
    stats: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        stats[key] = stats.get(key, 0) + n

    # 1) 关系词表外的边。
    bad_edges = [e for e in edges if canonical_relation(e.get("relation")) is None]
    bump("edge_off_vocab_relation", len(bad_edges))
    # 2) 该删的节点(连同它的全部边一起消失)。
    bad_nodes = []
    for node in nodes:
        reason = should_drop_node(str(node.get("name") or ""), str(node.get("type") or ""))
        if reason:
            bad_nodes.append(node)
            bump(f"node_{reason}")
    # 3) 前两步之后没有 REL 边的孤立实体(旧"提到即建点"留下的主体污染)。
    live_edges = [
        e
        for e in edges
        if canonical_relation(e.get("relation")) is not None
        and not should_drop_node(str(e["src"]), str(e["src_type"]))
        and not should_drop_node(str(e["dst"]), str(e["dst_type"]))
    ]
    connected = {str(e["src"]) for e in live_edges} | {str(e["dst"]) for e in live_edges}
    doomed_names = {(str(n["name"]), str(n.get("type") or "")) for n in bad_nodes}
    isolated = [
        n
        for n in nodes
        if str(n["name"]) not in connected and (str(n["name"]), str(n.get("type") or "")) not in doomed_names
    ]
    bump("node_isolated", len(isolated))

    if not apply:
        return stats

    # 执行顺序: 先删边(含被保留节点之间的表外边), 再删节点(DETACH 兜住剩余 MENTIONS)。
    if bad_edges:
        _run(
            url,
            "UNWIND $rows AS row "
            "MATCH (a:MemoryEntity {user_id: $uid, name: row.src, type: row.src_type})"
            "-[r:REL {relation: row.relation}]->"
            "(b:MemoryEntity {user_id: $uid, name: row.dst, type: row.dst_type}) "
            "DELETE r",
            {"uid": uid, "rows": bad_edges},
            write=True,
        )
    doomed = sorted(doomed_names | {(str(n["name"]), str(n.get("type") or UNKNOWN_TYPE)) for n in isolated})
    if doomed:
        _run(
            url,
            "UNWIND $rows AS row "
            "MATCH (e:MemoryEntity {user_id: $uid, name: row[0], type: row[1]}) "
            "DETACH DELETE e",
            {"uid": uid, "rows": [list(pair) for pair in doomed]},
            write=True,
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="按 graph_vocab 口径清理个人图谱存量脏数据")
    parser.add_argument("--url", default=DEFAULT_URL, help="Neo4j HTTP tx 端点")
    parser.add_argument("--user", action="append", default=[], help="只清理指定 user_id(可重复)")
    parser.add_argument("--apply", action="store_true", help="真正执行删除(默认只出报告)")
    args = parser.parse_args()

    if args.user:
        users = list(dict.fromkeys(args.user))
    else:
        rows = _run(args.url, "MATCH (u:MemoryUser) RETURN u.user_id AS uid ORDER BY uid")
        users = [str(r["uid"]) for r in rows if r.get("uid")]
    if not users:
        print("图里没有任何用户锚点, 无需清理。")
        return 0

    mode = "APPLY(已删除)" if args.apply else "DRY-RUN(仅报告)"
    print(f"个人图谱存量清理 · {mode} · {len(users)} 个用户\n")
    totals: dict[str, int] = {}
    for uid in users:
        stats = clean_user(args.url, uid, apply=args.apply)
        for key, value in stats.items():
            totals[key] = totals.get(key, 0) + value
        if stats:
            print(f"{uid:<10} " + "  ".join(f"{key}={value}" for key, value in sorted(stats.items())))
        else:
            print(f"{uid:<10} 干净")
    print("\n合计: " + ("  ".join(f"{key}={value}" for key, value in sorted(totals.items())) or "无需清理"))
    if not args.apply:
        print("确认无误后加 --apply 执行删除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
