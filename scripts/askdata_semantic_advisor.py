"""语义层编辑建议器(方案 §4⑥ 的 V3 "semantic layer editing agent", 离线、只出建议)。

它读的是问数自己的失败现场(留痕 + 事件 + 草案词), 产出的是**可以 review 的 YAML diff 建议**,
绝不直接改口径 —— 这是刻意的: 让模型自动写口径等于把"数字的定义权"交给一次概率输出,
而本系统全部设计都在避免这件事。

四类建议(全部由结构事实推出, 不靠模型猜):
- ``add_term``: 反复有说法命中不到任何指标(候选为空的轮次) → 提议把该说法登记成某指标的别名;
- ``adjust_weight``: 某说法被澄清了但用户点的正是最高分候选(误澄清) → 提议调别名权重;
- ``add_dim_value``: 过滤值因不在枚举内被拒(E_ENUM_INVALID) → 提议给维度补取值/黑话别名;
- ``compat``: 维度被 E_INCOMPATIBLE_DIM 反复拒 → 提议登记兼容规则(可带 default_filter)。

用法::

    uv run python -m scripts.askdata_semantic_advisor --days 30
    uv run python -m scripts.askdata_semantic_advisor --days 30 --apply   # 显式才写进 semantic-defs
    uv run python -m scripts.askdata_semantic_advisor --days 30 --stats

写进 YAML 后仍要跑 ``scripts.sync_semantic_defs.py`` 才进库(真值→库的单向路径不变)。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.askdata import store  # noqa: E402
from app.db import sync as dbsync  # noqa: E402
from app.semantic import registry  # noqa: E402
from app.semantic.link import _norm  # noqa: E402

_CST = timezone(timedelta(hours=8))
# 一条建议至少要有的证据条数: 一次就提等于把个例当规律, review 的人会被噪声埋掉。
MIN_EVIDENCE = 2


def _clusters(days: int) -> dict[str, list[dict[str, Any]]]:
    """把最近的失败轮次按"说法/维度"聚成簇(结构事实, 不猜语义)。"""
    since = (datetime.now(_CST) - timedelta(days=max(1, days))).isoformat()
    sql = (
        "SELECT query_id, question, verdict, route, error_codes, candidates_json, dsl_json, semver "
        "FROM askdata_query_log WHERE created_at >= :since "
        "AND (verdict IN ('clarify', 'fail', 'denied') OR feedback = 'down')"
    )
    with dbsync.get_sync_engine().connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(text(sql), {"since": since}).all()]
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        codes = _as_list(row.get("error_codes"))
        candidates = _as_list(row.get("candidates"))
        if not candidates and str(row.get("verdict")) in ("clarify", "fail"):
            buckets["no_link"].append(row)
        if "E_ENUM_INVALID" in codes:
            buckets["enum"].append(row)
        if "E_INCOMPATIBLE_DIM" in codes:
            buckets["compat"].append(row)
        if str(row.get("verdict")) == "clarify" and candidates:
            buckets["clarified"].append(row)
    return buckets


def _proposals(days: int) -> list[dict[str, Any]]:
    catalog = registry.get_catalog()
    buckets = _clusters(days)
    out: list[dict[str, Any]] = []

    # 1) 完全没命中的说法 → 补词(候选用文本包含 + 标签重叠挑最接近的登记指标)
    phrase_hits: Counter[str] = Counter()
    phrase_evidence: dict[str, list[str]] = defaultdict(list)
    for row in buckets["no_link"]:
        phrase = _norm(str(row.get("question") or ""))
        if not phrase or len(phrase) < 2:
            continue
        phrase_hits[phrase] += 1
        phrase_evidence[phrase].append(str(row.get("query_id")))
    for phrase, count in phrase_hits.items():
        if count < MIN_EVIDENCE:
            continue
        target, overlap = _closest_metric(catalog, phrase)
        if not target or overlap <= 0:
            continue
        out.append({
            "kind": "add_term",
            "term": phrase[:64],
            "term_type": "metric",
            "target_id": target,
            "weight": 0.85,
            "evidence": {"轮次": count, "query_ids": phrase_evidence[phrase][:5]},
            "note": f"有 {count} 轮的说法「{phrase[:40]}」一个口径都没链接上, 建议登记为别名",
        })

    # 2) 误澄清(澄清了但用户点的正是最高分) → 调权重
    pick_stats: dict[tuple[str, str], int] = Counter()
    for row in buckets["clarified"]:
        top = _top_candidate(row)
        chosen = _chosen_metric(row)
        if top and chosen and top == chosen:
            pick_stats[(chosen, top)] += 1
    for (chosen, _top), count in pick_stats.items():
        if count < MIN_EVIDENCE:
            continue
        out.append({
            "kind": "adjust_weight",
            "target_id": chosen,
            "weight": 1.0,
            "evidence": {"轮次": count},
            "note": f"指标 {chosen} 被误澄清 {count} 次(用户点的就是它), 建议把对应别名权重提满或放宽 gap",
        })

    # 3) 枚举值被拒 → 给维度补取值
    enum_hits: Counter[tuple[str, str]] = Counter()
    for row in buckets["enum"]:
        for dim_id, value in _rejected_values(row):
            enum_hits[(dim_id, value)] += 1
    for (dim_id, value), count in enum_hits.items():
        if count < MIN_EVIDENCE or catalog.get_dim(dim_id) is None:
            continue
        out.append({
            "kind": "add_dim_value",
            "dim_id": dim_id,
            "value": value,
            "evidence": {"轮次": count},
            "note": f"维度 {dim_id} 被用取值「{value}」问过 {count} 次但不在枚举内, 请确认它是新值还是黑话",
        })

    # 4) 维度反复不兼容 → 兼容规则(人决定是标 no 还是给 default_filter)
    compat_hits: Counter[tuple[str, str]] = Counter()
    for row in buckets["compat"]:
        for metric_id, dim_id in _incompatible_pairs(row):
            compat_hits[(metric_id, dim_id)] += 1
    for (metric_id, dim_id), count in compat_hits.items():
        if count < MIN_EVIDENCE:
            continue
        out.append({
            "kind": "compat",
            "metric_id": metric_id,
            "dim_id": dim_id,
            "compat": "no",
            "evidence": {"轮次": count},
            "note": f"{metric_id}×{dim_id} 被拒 {count} 次: 确认是真的不能同看, 还是该登记一条 default_filter 救回",
        })
    return sorted(out, key=lambda item: (-item["evidence"]["轮次"], item["kind"]))


def _closest_metric(catalog, phrase: str) -> tuple[str, int]:
    """按"说法与指标标签的最长公共片段"挑最接近的登记指标(不做语义猜测)。"""
    best, best_overlap = "", 0
    for metric in catalog.active_metrics:
        label = _norm(metric.label)
        overlap = len(_common_prefix_run(phrase, label))
        if overlap > best_overlap:
            best, best_overlap = metric.metric_id, overlap
    return best, best_overlap


def _common_prefix_run(a: str, b: str) -> str:
    """两串的最长公共子串(短问题与标签上都够用, O(n*m) 但输入都很短)。"""
    if not a or not b:
        return ""
    best_len, best_end = 0, 0
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best_len:
                    best_len, best_end = current[j], j
        previous = current
    return b[best_end - best_len:best_end]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _top_candidate(row: dict[str, Any]) -> str:
    candidates = [c for c in _as_list(row.get("candidates_json")) if isinstance(c, dict)]
    metrics = [c for c in candidates if c.get("kind") == "metric"]
    return str(metrics[0].get("id") or "") if metrics else ""


def _chosen_metric(row: dict[str, Any]) -> str:
    dsl = row.get("dsl_json")
    if isinstance(dsl, str):
        try:
            dsl = json.loads(dsl)
        except ValueError:
            return ""
    for item in ((dsl or {}).get("metrics") or []):
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return ""


def _rejected_values(row: dict[str, Any]) -> list[tuple[str, str]]:
    """从拒因文本里取 (维度, 被拒取值): 枚举校验的消息形如 "X 没有取值 ['Y']"。"""
    import re

    dsl = row.get("dsl_json")
    if isinstance(dsl, str):
        try:
            dsl = json.loads(dsl)
        except ValueError:
            dsl = {}
    out: list[tuple[str, str]] = []
    for item in ((dsl or {}).get("filters") or []):
        if isinstance(item, dict) and item.get("dim") and isinstance(item.get("value"), str):
            out.append((str(item["dim"]), str(item["value"])))
    if out:
        return out
    for code in _as_list(row.get("error_codes")):
        _ = code
    match = re.search(r"没有取值 \[([^\]]+)\]", str(row.get("notes") or ""))
    return [("", match.group(1).strip("' "))] if match else []


def _incompatible_pairs(row: dict[str, Any]) -> list[tuple[str, str]]:
    dsl = row.get("dsl_json")
    if isinstance(dsl, str):
        try:
            dsl = json.loads(dsl)
        except ValueError:
            return []
    metrics = [str((item or {}).get("id")) for item in ((dsl or {}).get("metrics") or [])
               if isinstance(item, dict) and item.get("id")]
    dims = [str(d) for d in ((dsl or {}).get("dims") or [])]
    return [(metric, dim) for metric in metrics for dim in dims]


def _write(out_path: Path, proposals: list[dict[str, Any]]) -> None:
    import yaml

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 智能问数语义层修订建议(由 scripts/askdata_semantic_advisor.py 产生)\n"
        "# 这是**建议文件**, 不是真值: 人工核对后手工并入 semantic-defs/*.yaml,\n"
        "# 再跑 scripts/sync_semantic_defs.py 才进库。自动改口径等于把定义权交给脚本。\n"
    )
    body = {"generated_at": datetime.now(_CST).isoformat(timespec="seconds"),
            "proposals": proposals}
    out_path.write_text(header + yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                                                width=4096),
                        encoding="utf-8")


def _apply(catalog_dir: Path, proposals: list[dict[str, Any]]) -> int:
    """把 add_term 与 add_dim_value 两类**追加**进真值文件(其余类别留给人判)。

    只追加不覆盖: 已有同名条目跳过。权重低于既有条目时不无脑拉高 —— 词表是人维护的,
    脚本只负责把"没人登记过的说法"补到桌面上。
    """
    written = 0
    terms_path = catalog_dir / "terms.yaml"
    values_path = catalog_dir / "dim_values.yaml"
    for item in proposals:
        kind = item.get("kind")
        if kind == "add_term":
            rows = _read_rows(terms_path, "terms")
            key = (str(item["term"]).strip(), str(item.get("target_id")))
            if any((str(r.get("term")).strip(), str(r.get("target_id"))) == key for r in rows):
                continue
            rows.append({"term": item["term"], "term_type": item.get("term_type") or "metric",
                         "target_id": item["target_id"], "weight": item.get("weight", 0.85),
                         "source": "advisor", "status": "draft",
                         "note": str(item.get("note") or "")[:200]})
            _write_rows(terms_path, "terms", rows)
            written += 1
        elif kind == "add_dim_value":
            rows = _read_rows(values_path, "dim_values")
            key = (str(item.get("dim_id")), str(item.get("value")).strip())
            if not key[0] or any((str(r.get("dim_id")), str(r.get("value")).strip()) == key
                                  for r in rows):
                continue
            rows.append({"dim_id": key[0], "value": key[1], "aliases": [], "popularity": 0,
                         "status": "draft"})
            _write_rows(values_path, "dim_values", rows)
            written += 1
    return written


def _read_rows(path: Path, key: str) -> list[dict[str, Any]]:
    import yaml

    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get(key) or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _write_rows(path: Path, key: str, rows: list[dict[str, Any]]) -> None:
    import yaml

    header = (
        "# 智能问数语义层真值(由 scripts/export_semantic_defs.py 生成/维护)\n"
        "# 改这里 = 改口径: 必须走 scripts/sync_semantic_defs.py 同步进库并留版本记录。\n"
    )
    path.write_text(header + yaml.safe_dump({key: rows}, allow_unicode=True, sort_keys=False,
                                             width=4096),
                    encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="智能问数语义层修订建议器(离线, 只出建议)")
    parser.add_argument("--days", type=int, default=30, help="回看窗口(天)")
    parser.add_argument("--out", default="", help="建议文件路径(默认 semantic-defs/proposals/)")
    parser.add_argument("--stats", action="store_true", help="只打印簇的大小, 不写文件")
    parser.add_argument("--apply", action="store_true",
                        help="把 add_term/add_dim_value 追加进真值 YAML(其余类别仍留给人判)")
    args = parser.parse_args(argv)

    catalog = registry.get_catalog()
    print(f"语义层: origin={catalog.origin} semver={catalog.semver} 指标 {len(catalog.active_metrics)} 个")
    buckets = _clusters(max(1, int(args.days or 30)))
    print("失败簇大小: " + ", ".join(f"{name}={len(rows)}" for name, rows in sorted(buckets.items())))
    if args.stats:
        return 0
    proposals = _proposals(max(1, int(args.days or 30)))
    if not proposals:
        print("没有达到证据下限的修订建议(说明最近的失败都是零散的, 不构成口径问题)。")
        return 0
    print(f"\n建议 {len(proposals)} 条(按证据条数排序):")
    for item in proposals[:20]:
        print(f"  [{item['kind']}] x{item['evidence']['轮次']} {item['note']}")
    stamp = datetime.now(_CST).strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else (
        Path(store.__file__).resolve().parents[1] / "semantic-defs" / "proposals"
        / f"proposals-{stamp}.yaml")
    _write(out_path, proposals)
    print(f"\n已写建议文件: {out_path}")
    if args.apply:
        written = _apply(out_path.parents[1] if out_path.parent.name == "proposals"
                         else out_path.parent, proposals)
        print(f"已追加 {written} 条到真值 YAML(status=draft, 未生效)")
        print("下一步: 人工核对 → python -m scripts.sync_semantic_defs --check 再 --actor <谁>")
    else:
        print("未改动真值文件: 加 --apply 才会把 add_term/add_dim_value 追加进 YAML。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
