"""评测报告: 一次运行落 ``reports/askdata_eval_<stamp>.{md,json}`` 并同步写最新副本。

与 ``scripts/msmarco_eval`` 同一套约定(带时间戳的历史文件 + ``_latest`` 指针):
只看 latest 会丢掉"改了什么导致分数变化"的对照, 只留历史文件又每次都要人去找文件名。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_CST = timezone(timedelta(hours=8))


def reports_dir(base: Path | None = None) -> Path:
    root = base or Path(__file__).resolve().parents[2]
    out = root / "reports"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return int(ordered[index])


def summarize(runs: list[dict[str, Any]], *, mode: str, judge_enabled: bool,
              degrade_note: str = "") -> dict[str, Any]:
    total = len(runs)
    passed = [r for r in runs if r.get("ex_pass")]
    rescued = [r for r in runs if not r.get("ex_pass") and r.get("judge") == "equivalent"]
    errored = [r for r in runs if r.get("error")]
    clarified = [r for r in runs if _is_clarify(r)]
    mis_clarified = [r for r in runs if r.get("mis_clarify")]
    em_passed = [r for r in runs if r.get("dsl_em")]
    f1_scores = [float(r.get("slot_f1")) for r in runs if r.get("slot_f1") is not None]
    fallback = [r for r in runs if str(r.get("route") or "") in ("sandbox", "degrade", "repair")
                or str(r.get("verdict") or "") == "degraded"]
    cache_hits = [r for r in runs if r.get("cache_hit")]
    latencies = [int(r.get("latency_ms") or 0) for r in runs if r.get("latency_ms") is not None]
    tokens = sum(int(r.get("token_cost") or 0) for r in runs)
    semvers = sorted({str(r.get("semver")) for r in runs if r.get("semver")})
    by_tag: dict[str, dict[str, int]] = {}
    for run in runs:
        for tag in [t for t in str(run.get("tags") or "").split(",") if t]:
            bucket = by_tag.setdefault(tag, {"total": 0, "pass": 0})
            bucket["total"] += 1
            bucket["pass"] += 1 if run.get("ex_pass") else 0

    def _bucket(field: str) -> dict[str, dict[str, float]]:
        """按 intent / 难度分层: 只看总分会把"简单题满分 + 难题全错"抹成一个中等分数。"""
        out: dict[str, dict[str, float]] = {}
        for run in runs:
            key = str(run.get(field) or "(未标)")
            item = out.setdefault(key, {"total": 0, "pass": 0, "ex": 0.0})
            item["total"] += 1
            item["pass"] += 1 if run.get("ex_pass") else 0
        for item in out.values():
            item["ex"] = round(item["pass"] * 100.0 / item["total"], 2) if item["total"] else 0.0
        return dict(sorted(out.items()))

    def _pct(count: int, base: int) -> float:
        return round(count * 100.0 / base, 2) if base else 0.0

    return {
        "mode": mode,
        "judge_enabled": judge_enabled,
        "degrade_note": degrade_note,
        "total": total,
        "ex_pass": len(passed),
        "exec_accuracy": _pct(len(passed), total),
        "judge_rescued": len(rescued),
        "judged_accuracy": _pct(len(passed) + len(rescued), total),
        "dsl_exact_match": _pct(len(em_passed), total),
        # slot-F1 只在"标了期望槽位"的用例上平均(没标的当 0 分就是假分数)。
        "slot_f1": round(sum(f1_scores) / len(f1_scores), 4) if f1_scores else None,
        "slot_f1_samples": len(f1_scores),
        "errored": len(errored),
        "clarified": len(clarified),
        "clarify_rate": _pct(len(clarified), total),
        "mis_clarified": len(mis_clarified),
        "mis_clarify_rate": _pct(len(mis_clarified), len(clarified)),
        "fallback": len(fallback),
        "fallback_rate": _pct(len(fallback), total),
        "cache_hits": len(cache_hits),
        "cache_hit_rate": _pct(len(cache_hits), total),
        "token_total": tokens,
        "semvers": semvers,
        "p95_latency_ms": _p95(latencies),
        "by_tag": {
            tag: {**counts, "accuracy": round(counts["pass"] * 100.0 / counts["total"], 2) if counts["total"] else 0.0}
            for tag, counts in sorted(by_tag.items())
        },
        "by_intent": _bucket("intent"),
        "by_difficulty": _bucket("difficulty"),
        "worst": [
            {"question": r.get("question"), "reason": r.get("reason") or r.get("error"),
             "route": r.get("route", ""), "dsl_em_note": r.get("dsl_em_note", "")}
            for r in runs if not r.get("ex_pass")
        ][:25],
        "rescued_detail": [
            {"question": r.get("question"), "suspect": r.get("suspect"),
             "judge_reason": r.get("judge_reason", "")}
            for r in rescued
        ][:25],
        "runs": runs,
    }


def _is_clarify(run: dict[str, Any]) -> bool:
    """本轮是不是被澄清拦下(新数据看 verdict, 旧报告里没有这个字段就看错误文本)。

    额外看 `kind == "clarify"`: 载荷里曾只写了 kind 没写 verdict, 那时这些轮次会被
    当成"结果缺列"计入 EX 失败, 于是报告一边报 10 条缺列一边写"澄清 0 条"。
    """
    if str(run.get("kind") or "") == "clarify":
        return True
    if str(run.get("verdict") or "") == "clarify":
        return True
    return "verdict=clarify" in str(run.get("error") or "")


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 智能问数评测报告",
        "",
        f"- 模式: ``{summary['mode']}``"
        f"; 判据: EX + {'两级 LLM judge' if summary['judge_enabled'] else '仅列比对/judge 关闭'}",
        f"- 用例数: {summary['total']}  **EX={summary['exec_accuracy']}%**"
        f"  (judge 判疑似等价另计 {summary['judge_rescued']} 条 → 上限 {summary['judged_accuracy']}%)",
        f"- DSL-EM={summary['dsl_exact_match']}%"
        f"; slot-F1={summary['slot_f1'] if summary['slot_f1'] is not None else '(未标注)'}"
        f" (参与样本 {summary['slot_f1_samples']})",
        f"- 澄清 {summary['clarified']} 条(触发率 {summary['clarify_rate']}%)"
        f", 其中误澄清 {summary['mis_clarified']} 条({summary['mis_clarify_rate']}%)"
        f"; 降级 {summary['fallback']} 条({summary['fallback_rate']}%)"
        f"; 缓存命中 {summary['cache_hits']} 条({summary['cache_hit_rate']}%)",
        f"- 执行异常 {summary['errored']} 条; P95 {summary['p95_latency_ms']}ms"
        f"; tokens 合计 {summary['token_total']}",
    ]
    if summary.get("semvers"):
        lines.append(f"- 本轮语义层版本: ``{', '.join(summary['semvers'])}``"
                     " (badcase 回查当时口径就靠这个号)")
    if summary.get("degrade_note"):
        lines += ["", f"> **降级说明**: {summary['degrade_note']} —— 本轮分数与全量跑不可直接比较。"]
    if summary.get("gate"):
        gate = summary["gate"]
        lines += ["", f"> **CI 门禁**: {gate}"]
    lines += [
        "",
        "主指标是 **EX(执行准确率)**: 结果集比对通过才算对。judge 只能把失败标成「疑似等价」",
        "提示**标准答案本身过期了**需要人复核, 不参与主指标 —— 让模型给模型打分是把尺子",
        "自己也晃一遍。按标签/意图/难度分组是找短板用的(哪一类问法在掉分), 不要只看总分。",
        "",
        "## 按标签",
        "",
        "| 标签 | 用例 | 通过 | 准确率 |",
        "| --- | --- | --- | --- |",
    ]
    for tag, bucket in summary["by_tag"].items():
        lines.append(f"| {tag} | {bucket['total']} | {bucket['pass']} | {bucket['accuracy']}% |")
    if not summary["by_tag"]:
        lines.append("| (无标签) | - | - | - |")
    for title, field in (("按意图", "by_intent"), ("按难度", "by_difficulty")):
        bucket = summary.get(field) or {}
        if not bucket:
            continue
        lines += ["", f"## {title}", "", "| 分组 | 用例 | 通过 | EX |", "| --- | --- | --- | --- |"]
        for key, item in bucket.items():
            lines.append(f"| {key} | {item['total']} | {item['pass']} | {item['ex']}% |")
    lines += ["", "## 未通过的用例(前 25 条)", ""]
    if summary["worst"]:
        for item in summary["worst"]:
            lines.append(f"- {item['question']}: {item['reason']}")
            if item.get("dsl_em_note"):
                lines.append(f"  - DSL-EM 差异: {item['dsl_em_note']}")
    else:
        lines.append("- 全部通过")
    if summary["rescued_detail"]:
        lines += ["", "## judge 判疑似等价(需人复核标准答案)", ""]
        for item in summary["rescued_detail"]:
            lines.append(f"- {item['question']} (suspect={item['suspect']}): {item['judge_reason']}")
    lines += ["", f"生成时间(北京时间): {datetime.now(_CST).strftime('%Y-%m-%d %H:%M:%S')}"]
    return "\n".join(lines) + "\n"


def write_reports(summary: dict[str, Any], base: Path | None = None) -> tuple[Path, Path]:
    out_dir = reports_dir(base)
    stamp = datetime.now(_CST).strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"askdata_eval_{stamp}.md"
    json_path = out_dir / f"askdata_eval_{stamp}.json"
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    for src, name in ((md_path, "askdata_eval_latest.md"), (json_path, "askdata_eval_latest.json")):
        (out_dir / name).write_bytes(src.read_bytes())
    return md_path, json_path
