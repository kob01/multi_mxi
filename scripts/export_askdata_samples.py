"""导出 SFT/DPO 训练样本为 JSONL(方案 §4⑥ 的"样本入队 → 人工批准 → 导出"最后一步)。

门禁是这个脚本存在的理由: 只导出 **status=approved 且两道门禁都过** 的样本 ——
① chosen DSL 真能过校验器并编成 SQL(可验证的口径), ② 用户给过肯定反馈或亲自改写过
(人认可的答案)。``--no-strict`` 会把没过关的也导出, 那是刻意留的应急口子而不是默认值。

为什么导出而不是让训练管线直连库: 样本一旦进了训练就是"模型以后都会这么做"的口径,
它必须是一份能落盘、能 review、能对上一次 diff 的文件(与 semantic-defs 同一个思路)。

用法::

    uv run python -m scripts.export_askdata_samples --dry-run
    uv run python -m scripts.export_askdata_samples --out data/askdata_samples.jsonl
    uv run python -m scripts.export_askdata_samples --stats      # 只看队列现状

默认不翻转状态: 只有加 ``--mark-exported`` 才把导出的行标成 exported(否则重跑一次就会
把同一批样本重复计入训练集)。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.askdata import store  # noqa: E402
from app.config import get_settings  # noqa: E402


def _line(row: dict[str, Any]) -> str:
    """一行的 JSONL 形状(sft 与 dpo 共用同一批键, 缺的就写 null)。"""
    payload = {
        "sample_id": row.get("sample_id"),
        "kind": row.get("kind"),
        "question": row.get("question"),
        "chosen_dsl": _as_obj(row.get("chosen_json")),
        "rejected_dsl": _as_obj(row.get("rejected_json")),
        "slots": _as_obj(row.get("slots_json")),
        "category": row.get("category"),
        "semver": row.get("semver"),
        # 溯源三件套: 出事时要能回到"哪一轮、哪条留痕、谁确认的"
        "query_id": row.get("query_id"),
        "trace_id": row.get("trace_id"),
        "confirmed_by": row.get("user_id"),
        "exported_from": "askdata_sample",
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _as_obj(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出智能问数 SFT/DPO 样本(JSONL)")
    parser.add_argument("--out", default="data/askdata_samples.jsonl", help="输出文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要导出的条目, 不写文件")
    parser.add_argument("--no-strict", action="store_true",
                        help="连门禁未过的样本也导出(应急口子, 默认关)")
    parser.add_argument("--mark-exported", action="store_true", help="导出后把状态标为 exported")
    parser.add_argument("--stats", action="store_true", help="只打印队列现状")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args(argv)

    stats = store.sample_stats()
    print(f"队列现状: {stats or '(读不出)'}")
    if args.stats:
        return 0
    strict = not args.no_strict
    if strict and get_settings().askdata_sample_gate_strict is False:
        print("提示: ASKDATA_SAMPLE_GATE_STRICT=false 被显式关掉了, 本次仍按 strict 导出"
              "(要真的放宽请加 --no-strict)。")
    rows = store.exportable_samples(strict=strict, limit=max(1, int(args.limit or 5000)))
    if not rows:
        print("没有可导出的样本: 队列里没有 approved 且过两道门禁的行。")
        print("下一步通常是: GET /api/askdata/samples 看 draft 队列, 复核后再批准。")
        return 0
    kinds = Counter(str(row.get("kind")) for row in rows)
    print(f"待导出 {len(rows)} 条: " + ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())))
    lines = [_line(row) for row in rows]
    if args.dry_run:
        for text in lines[:5]:
            print("  [dry]", text[:200])
        if len(lines) > 5:
            print(f"  ... 另有 {len(lines) - 5} 条")
        return 0
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写出 {target}({len(lines)} 行)")
    if args.mark_exported:
        marked = store.mark_samples_exported([str(row.get("sample_id")) for row in rows])
        print(f"已标记 {marked} 条为 exported(下次导出不再包含它们)")
    else:
        print("未翻状态: 这些样本仍是 approved(要一次性导出请加 --mark-exported)。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
