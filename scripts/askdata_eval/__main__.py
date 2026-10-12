"""智能问数评测入口。

用法::

    uv run python -m scripts.askdata_eval                      # DSL 编译回归(无 LLM)
    uv run python -m scripts.askdata_eval --mode agent          # 端到端
    uv run python -m scripts.askdata_eval --limit 30 --tag 环比 --no-judge
    uv run python -m scripts.askdata_eval --gate                # CI 门禁: EX 降幅超阈值退码 3

退出码: 0 正常完成(哪怕有失败用例, 失败信息在报告里); 2 = 前置条件不满足(库不可达/
评测集为空/agent 模式探测失败); 3 = **CI 门禁红**(仅 ``--gate`` 时判)。把"分数不好"与
"跑不起来"分两个码: CI 看到 2 去查环境, 看到 3 才去查回归。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from scripts.askdata_eval import judge, report, runner

logger = logging.getLogger("askdata_eval")

_CST = timezone(timedelta(hours=8))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="智能问数离线评测(EX + 两级 LLM judge)")
    parser.add_argument("--mode", choices=("dsl", "agent"), default="dsl",
                        help="dsl=只测编译与取数(无 LLM, 可当回归门禁); agent=端到端含槽位与 DSL 生成")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条(0=全部)")
    parser.add_argument("--tag", default="", help="按用例标签过滤(子串匹配)")
    parser.add_argument("--no-judge", action="store_true",
                        help="关闭 LLM judge(只报确定性 EX, 零额外模型调用)")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="agent 模式并发数(受下游 LLM/MCP 容量限制, 别调大)")
    parser.add_argument("--no-record", action="store_true",
                        help="不写 askdata_eval_run(只看报告, 不动看板)")
    parser.add_argument("--gate", action="store_true",
                        help="CI 门禁: 与上一条同模式 run 比 EX, 降幅超 ASKDATA_EVAL_GATE_MAX_DROP 退码 3")
    return parser.parse_args(argv)


async def _run_one(case: dict[str, Any], *, mode: str, use_judge: bool, agent: Any = None,
                   scope: Any = None) -> dict[str, Any]:
    actual = (await runner.run_agent(case, agent=agent)) if mode == "agent" else runner.run_dsl(case, scope=scope)
    graded = judge.grade(case, actual, use_judge=use_judge)
    graded["latency_ms"] = actual.get("latency_ms")
    graded["route"] = actual.get("route", "")
    graded["error"] = actual.get("error", "")
    graded["dsl"] = actual.get("dsl") or case.get("dsl")
    return graded


async def run(args: argparse.Namespace) -> int:
    mode = args.mode
    use_judge = not args.no_judge
    degrade_note = ""
    if use_judge:
        # judge 依赖 LLM: 先探一次, 探不通就明确降级成"只跑 EX", 而不是每条用例里各挂一次
        from app.config import get_settings
        from app.llm import get_chat_model

        try:
            get_chat_model(get_settings().llm_model)
        except Exception as exc:  # noqa: BLE001
            use_judge = False
            degrade_note = f"LLM 不可用, judge 已关闭: {type(exc).__name__}: {str(exc)[:160]}"
    if mode == "agent":
        note = await runner.probe_agent_mode()
        if note:
            print(f"前置条件不满足: {note}", file=sys.stderr)
            return 2

    try:
        cases = load_cases_for(args)
    except Exception as exc:  # noqa: BLE001 - 装载失败要说清下一步做什么
        print(f"评测集装载失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        scope = runner.eval_scope() if mode == "dsl" else None
    except Exception as exc:  # noqa: BLE001
        print(f"作用域解析失败(检查 DEFAULT_TENANT_ID 与 sys_departments): {exc}", file=sys.stderr)
        return 2

    print(f"模式={mode}  用例={len(cases)}  judge={'开' if use_judge else '关'}  "
          f"作用域={scope.note() if scope else '(agent 侧自行解析)'}")
    agent = None
    if mode == "agent":
        from app.agents.askdata_agent.executor import AskDataAgent

        agent = AskDataAgent()
    semaphore = asyncio.Semaphore(max(1, args.concurrency if mode == "agent" else 1))

    async def _bounded(case: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            graded = await _run_one(case, mode=mode, use_judge=use_judge, agent=agent, scope=scope)
        flag = "OK " if graded["ex_pass"] else ("RES" if graded.get("judge") == "equivalent" else "NG ")
        print(f"  [{flag}] {case['question']}  <- {graded['reason'][:120]}")
        return graded

    runs = list(await asyncio.gather(*(_bounded(c) for c in cases)))
    summary = report.summarize(runs, mode=mode, judge_enabled=use_judge, degrade_note=degrade_note)
    gate_status, gate_note, baseline_run_id = "", "", ""
    if args.gate:
        gate_status, gate_note, baseline_run_id = _gate(summary, mode=mode)
        summary["gate"] = f"{gate_status}: {gate_note}"
    summary["gate_status"], summary["gate_note"], summary["baseline_run_id"] = (
        gate_status, gate_note, baseline_run_id,
    )
    md_path, json_path = report.write_reports(summary)
    print(f"\nEX={summary['exec_accuracy']}%  DSL-EM={summary['dsl_exact_match']}%"
          f"  slot-F1={summary['slot_f1']}  judge 判疑似等价另 {summary['judge_rescued']} 条  "
          f"P95={summary['p95_latency_ms']}ms")
    if gate_note:
        print(f"CI 门禁: {gate_status} — {gate_note}")
    print(f"报告: {md_path}\n      {json_path}")
    if not args.no_record:
        record(summary, md_path)
    return 3 if gate_status == "red" else 0


def _gate(summary: dict[str, Any], *, mode: str) -> tuple[str, str, str]:
    """CI 门禁: 与上一条**同模式**的 run 比 EX 降幅(方案 §6 的硬图)。

    只比同模式: dsl 模式没有 LLM 必然高分, 拿它当 agent 模式的基线会把每一轮都判红。
    没有基线(首次跑)就是 green 并明写原因 —— "没得比"不是"回归"。
    """
    from app.askdata import store
    from app.config import get_settings

    max_drop = float(get_settings().askdata_eval_gate_max_drop or 0)
    if max_drop <= 0:
        return "off", "ASKDATA_EVAL_GATE_MAX_DROP=0, 门禁已关闭", ""
    eval_mode = f"{summary['mode']}-{'full' if summary['judge_enabled'] else 'ex-only'}"
    baseline = store.latest_eval_run(eval_mode) or store.latest_eval_run(mode)
    if not baseline:
        return "green", "没有同模式的历史 run, 本轮作为基线", ""
    before = float(baseline.get("exec_accuracy") or 0)
    now = float(summary["exec_accuracy"] or 0)
    drop = before - now
    run_id = str(baseline.get("run_id") or "")
    if drop > max_drop:
        return (
            "red",
            f"EX 从 {before}% 降到 {now}% (降幅 {drop:.2f}pp > 阈值 {max_drop}pp), "
            f"基线 run={run_id}",
            run_id,
        )
    return (
        "green",
        f"EX {now}% vs 基线 {before}% (差 {abs(drop):.2f}pp, 阈值 {max_drop}pp), 基线 run={run_id}",
        run_id,
    )


def load_cases_for(args: argparse.Namespace) -> list[dict[str, Any]]:
    from scripts.askdata_eval import cases

    return cases.load_cases(limit=args.limit or None, tag=args.tag)


def record(summary: dict[str, Any], md_path) -> None:
    """把结论写进 ``askdata_eval_run``: 看板上的 EX 与 CI 门禁基线就取这一行。"""
    from app.askdata import store

    run_id = uuid.uuid4().hex[:24]
    total = int(summary["total"] or 0)
    written = store.record_eval_run({
        "run_id": run_id,
        "started_at": datetime.now(_CST),
        "case_count": total,
        "exec_accuracy": summary["exec_accuracy"],
        "dsl_exact_match": summary["dsl_exact_match"],
        "slot_f1": summary["slot_f1"] or 0,
        # 评测里"一次出数且正确"就是 EX 口径(没有人工重试这个机会)。
        "first_pass_rate": summary["exec_accuracy"],
        "clarify_rate": summary["clarify_rate"],
        "mis_clarify_rate": summary["mis_clarify_rate"],
        "degrade_rate": summary["fallback_rate"],
        "fallback_rate": summary["fallback_rate"],
        "p95_latency_ms": summary["p95_latency_ms"],
        "gate_status": summary.get("gate_status") or "",
        "gate_note": summary.get("gate_note") or "",
        "baseline_run_id": summary.get("baseline_run_id") or "",
        "semver": ",".join(summary.get("semvers") or [])[:32],
        "mode": f"{summary['mode']}-{'full' if summary['judge_enabled'] else 'ex-only'}",
        "report_path": str(md_path),
        "detail": {"by_tag": summary["by_tag"], "by_intent": summary["by_intent"],
                   "by_difficulty": summary["by_difficulty"],
                   "cache_hit_rate": summary["cache_hit_rate"],
                   "token_total": summary["token_total"]},
        "created_by": "scripts.askdata_eval",
    })
    print("已写入 askdata_eval_run 结论行" if written else "结论未入库(看板 EX 仍是上一次的值)")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
