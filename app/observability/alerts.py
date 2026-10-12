"""告警线: 对同一份问数聚合判阈值, 越线就报出来(不自动动手改任何东西)。

四条规则就是方案 §7 列的那四类信号:

- ``fail_rate`` 执行/权限错误率: 数算不出来是最直接的失效;
- ``clarify_rate`` 澄清触发率突增: 它通常意味着**语义层被改动或口径漂移**(而不是用户变笨了);
- ``degrade_rate`` 降级率: 走到沙箱/自修复的比例上升 = 前两级覆盖不住了;
- ``eval_gate`` EX 掉分: 直接取评测 run 的 CI 门禁结论(与评测脚本用同一个判据, 不开第二套)。

阈值全在 config 里(不在这里写死), 因为"多少算异常"是运营口径而不是技术事实。样本太少时
一律不告警: 三条问数里有一条失败就发告警, 结果只会是没人再看它。
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings

# 低于这个样本量不判任何比率类告警(避免"1/3 失败 = 33%"这种噪声)。
MIN_SAMPLES = 20


def evaluate(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """返回每条规则的当前状态(不论是否越线都列出来, 页面与 /metrics 都要能显示阈值)。"""
    settings = get_settings()
    total = int(stats.get("total") or 0)
    out: list[dict[str, Any]] = []

    def _rate_rule(rule: str, value: Any, threshold: float, note: str) -> None:
        current = float(value or 0)
        fired = total >= MIN_SAMPLES and current > threshold
        out.append({
            "rule": rule, "value": round(current, 2), "threshold": threshold,
            "fired": fired, "samples": total, "note": note,
        })

    _rate_rule(
        "fail_rate", stats.get("fail_rate"), float(settings.askdata_alert_fail_rate),
        "执行/权限错误率: 数算不出来的比例",
    )
    _rate_rule(
        "degrade_rate", stats.get("degrade_rate"), float(settings.askdata_alert_degrade_rate),
        "降级率: 走到沙箱或自修复的比例(前两级覆盖不住了)",
    )
    clarify_target = float(settings.askdata_clarify_rate_target or 0.15) * 100.0
    _rate_rule(
        "clarify_rate", stats.get("clarify_rate"), round(clarify_target, 2),
        "澄清触发率: 突增通常意味着语义层被改动或口径漂移",
    )
    # P95 是毫秒不是百分数, 单独一条(复用上面的结构, 字段名叫 value/threshold 保持一致)。
    p95 = int(stats.get("p95_latency_ms") or 0)
    out.append({
        "rule": "p95_latency_ms", "value": p95,
        "threshold": int(settings.askdata_alert_p95_ms),
        "fired": total >= MIN_SAMPLES and p95 > int(settings.askdata_alert_p95_ms),
        "samples": total, "note": "P95 延迟: 问数是人在等的场景",
    })
    eval_info = stats.get("latest_eval") if isinstance(stats, dict) else None
    if isinstance(eval_info, dict):
        gate = str(eval_info.get("gate_status") or "")
        out.append({
            "rule": "eval_gate", "value": float(eval_info.get("exec_accuracy") or 0),
            "threshold": float(settings.askdata_eval_gate_max_drop),
            "fired": gate == "red", "samples": int(eval_info.get("case_count") or 0),
            "note": f"最近一次评测 CI 门禁结论={gate or '(未记录)'}",
        })
    if settings.askdata_golden_min_cases:
        golden = int(stats.get("golden_enabled") or 0)
        out.append({
            "rule": "golden_size", "value": golden,
            "threshold": int(settings.askdata_golden_min_cases),
            "fired": 0 < golden < int(settings.askdata_golden_min_cases),
            "samples": golden, "note": "评测集规模低于下限: 分层结论不可信",
        })
    return out
