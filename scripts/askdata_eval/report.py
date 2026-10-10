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
    clarified = [r for r in runs if "verdict=clarify" in str(r.get("error") or "")]
    latencies = [int(r.get("latency_ms") or 0) for r in runs if r.get("latency_ms") is not None]
    by_tag: dict[str, dict[str, int]] = {}
    for run in runs:
        for tag in [t for t in str(run.get("tags") or "").split(",") if t]:
            bucket = by_tag.setdefault(tag, {"total": 0, "pass": 0})
            bucket["total"] += 1
            bucket["pass"] += 1 if run.get("ex_pass") else 0
    return {
        "mode": mode,
        "judge_enabled": judge_enabled,
        "degrade_note": degrade_note,
        "total": total,
        "ex_pass": len(passed),
        "exec_accuracy": round(len(passed) * 100.0 / total, 2) if total else 0.0,
        "judge_rescued": len(rescued),
        "judged_accuracy": round((len(passed) + len(rescued)) * 100.0 / total, 2) if total else 0.0,
        "errored": len(errored),
        "clarified": len(clarified),
        "p95_latency_ms": _p95(latencies),
        "by_tag": {
            tag: {**counts, "accuracy": round(counts["pass"] * 100.0 / counts["total"], 2) if counts["total"] else 0.0}
            for tag, counts in sorted(by_tag.items())
        },
        "worst": [
            {"question": r.get("question"), "reason": r.get("reason") or r.get("error"),
             "route": r.get("route", "")}
            for r in runs if not r.get("ex_pass")
        ][:25],
        "rescued_detail": [
            {"question": r.get("question"), "suspect": r.get("suspect"),
             "judge_reason": r.get("judge_reason", "")}
            for r in rescued
        ][:25],
        "runs": runs,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 智能问数评测报告",
        "",
        f"- 模式: ``{summary['mode']}``"
        f"; 判据: EX + {'两级 LLM judge' if summary['judge_enabled'] else '仅列比对/judge 关闭'}",
        f"- 用例数: {summary['total']}  **EX={summary['exec_accuracy']}%**"
        f"  (judge 判疑似等价另计 {summary['judge_rescued']} 条 → 上限 {summary['judged_accuracy']}%)",
        f"- 执行异常 {summary['errored']} 条; 其中被澄清拦下 {summary['clarified']} 条; P95 {summary['p95_latency_ms']}ms",
    ]
    if summary.get("degrade_note"):
        lines += ["", f"> **降级说明**: {summary['degrade_note']} —— 本轮分数与全量跑不可直接比较。"]
    lines += [
        "",
        "主指标是 **EX(执行准确率)**: 结果集比对通过才算对。judge 只能把失败标成「疑似等价」",
        "提示**标准答案本身过期了**需要人复核, 不参与主指标 —— 让模型给模型打分是把尺子",
        "自己也晃一遍。按标签分组是找短板用的(哪一类问法在掉分), 不要只看总分。",
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
    lines += ["", "## 未通过的用例(前 25 条)", ""]
    if summary["worst"]:
        for item in summary["worst"]:
            lines.append(f"- {item['question']}: {item['reason']}")
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
