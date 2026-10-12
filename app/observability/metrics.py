"""Prometheus 文本格式指标(``GET /metrics``), 数据源是问数留痕表的聚合。

为什么从库里读而不是在内存里计数:
1. **口径同源**: 与 ``/api/askdata/stats`` 看板用同一份聚合(``store.stats``), 不会出现
   "看板说澄清率 8%, 指标说 12%" 这种两件事实;
2. **重启不丢、多副本不分裂**: 内存计数器在每个进程各有一份, 拉哪个副本看运气;
3. 问数的量级(每人几十次/天)不需要纳秒级计数器, 一次 SELECT 聚合的代价可以忽略。

代价也说清: 指标的新鲜度等于抓取周期(库里刚写的行下一次抓才看见), 不适合做亚秒级告警;
真要实时告警应该在 MCP 进程里加计数器, 那是另一件事, 不在这一期。

暴露的内容只有计数/比率/延迟/版本字符串, **没有任何业务数据与用户原话**。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_HELP: dict[str, str] = {
    "askdata_queries": "AskData turns recorded in the query log",
    "askdata_latency_ms": "AskData end-to-end latency in milliseconds",
    "askdata_tokens": "Tokens spent by AskData turns",
    "askdata_rate_percent": "AskData dashboard rates in percent",
    "askdata_eval_score_percent": "Latest offline evaluation scores in percent",
    "askdata_alert_firing": "AskData alert line currently firing (1=firing)",
    "askdata_semantic_info": "Current semantic layer snapshot fingerprint",
    "askdata_engine_breaker_open_seconds": "Remaining open time of an askdata route breaker",
    "askdata_golden_cases": "Enabled golden evaluation cases",
    "askdata_sample_queue": "SFT/DPO sample queue size by state (gated_out = blocked by the gate)",
}


def render(*, days: int = 7) -> str:
    """整份 /metrics 文本(Prometheus exposition format)。

    任何一步读不出来都返回已经拼好的部分 + 一行 ``askdata_scrape_ok 0``: 抓取端能立刻
    看出"这次抓到的数不全", 比静默交出一份看着正常的空集好得多。
    """
    lines: list[str] = []
    ok = True
    try:
        from app.askdata import store

        stats = store.stats(days=days)
    except Exception as exc:  # noqa: BLE001 - 聚合失败也要交出一份能看的文本
        logger.warning("[askdata] /metrics 聚合失败: %s", exc)
        stats = {}
        ok = False
    if isinstance(stats, dict) and stats.get("error"):
        logger.warning("[askdata] /metrics 聚合报错: %s", stats["error"])
        ok = False

    lines.append("# HELP askdata_scrape_ok Whether the metrics scrape completed (1=yes).")
    lines.append("# TYPE askdata_scrape_ok gauge")
    lines.append(f"askdata_scrape_ok {1 if ok else 0}")

    total = int(stats.get("total") or 0) if isinstance(stats, dict) else 0
    lines.append(_help("askdata_queries"))
    lines.append("# TYPE askdata_queries counter")
    lines.append(f'askdata_queries_total{{window="days{days}"}} {total}')
    # 比率(百分数)一次出齐: 抓取端做告警表达式时直接比阈值, 不用再自己做除法。
    rates = {
        key: value for key, value in stats.items()
        if isinstance(value, (int, float)) and key.endswith("_rate")
    } if isinstance(stats, dict) else {}
    lines.append(_help("askdata_rate_percent"))
    lines.append("# TYPE askdata_rate_percent gauge")
    for key, value in sorted(rates.items()):
        lines.append(f'askdata_rate_percent{{metric="{key}"}} {float(value):.2f}')

    lines.append(_help("askdata_latency_ms"))
    lines.append("# TYPE askdata_latency_ms summary")
    lines.append(f'askdata_latency_ms{{quantile="0.95"}} {int(stats.get("p95_latency_ms") or 0)}')
    lines.append(f'askdata_latency_ms{{quantile="0.50"}} '
                 f'{int(stats.get("avg_latency_ms") or 0)}')

    lines.append(_help("askdata_tokens"))
    lines.append("# TYPE askdata_tokens counter")
    lines.append(f"askdata_tokens_total {int(stats.get('token_total') or 0)}")

    eval_info = (stats or {}).get("latest_eval") if isinstance(stats, dict) else None
    if isinstance(eval_info, dict) and eval_info.get("run_id"):
        lines.append(_help("askdata_eval_score_percent"))
        lines.append("# TYPE askdata_eval_score_percent gauge")
        for key in ("exec_accuracy", "dsl_exact_match", "slot_f1"):
            if key in eval_info:
                lines.append(
                    f'askdata_eval_score_percent{{metric="{key}",mode="{eval_info.get("mode", "")}"}} '
                    f'{float(eval_info.get(key) or 0):.2f}'
                )
        lines.append("# HELP askdata_eval_gate_status Latest evaluation CI gate (1=red).")
        lines.append("# TYPE askdata_eval_gate_status gauge")
        lines.append(
            f"askdata_eval_gate_status {1 if str(eval_info.get('gate_status') or '') == 'red' else 0}"
        )
        lines.append("# HELP askdata_eval_cases Latest evaluation case count.")
        lines.append("# TYPE askdata_eval_cases gauge")
        lines.append(f"askdata_eval_cases {int(eval_info.get('case_count') or 0)}")

    lines.extend(_semantic_lines())
    lines.extend(_alert_lines(stats if isinstance(stats, dict) else {}, days=days))

    # 熔断状态在 MCP 进程内存里, 网关看不到; 这里只出治理面能确认的部分。
    lines.append(_help("askdata_golden_cases"))
    lines.append("# TYPE askdata_golden_cases gauge")
    lines.append(f"askdata_golden_cases_enabled {_golden_count()}")
    # 已批准参与语义缓存的条数: 这个数为 0 是**正常且预期**的(合成 golden 不自动可复用),
    # 但看板需要能区分"缓存没命中因为没人批准"与"缓存坏了所以没命中"。
    lines.append(f"askdata_golden_cases_cacheable {store_golden_cacheable()}")
    lines.extend(_sample_lines())
    return "\n".join(lines) + "\n"


def _sample_lines() -> list[str]:
    """SFT/DPO 样本队列现状: 包括"被门禁拦住了多少条"(它决定这一轮值不值得去复核)。"""
    from app.askdata import store

    try:
        stats = store.sample_stats()
    except Exception as exc:  # noqa: BLE001 - 队列读不出就是少一组指标
        logger.debug("[askdata] /metrics 读样本队列失败: %s", exc)
        return []
    if not stats:
        return []
    out = [
        _help("askdata_sample_queue"),
        "# TYPE askdata_sample_queue gauge",
    ]
    for key in ("total", "draft", "approved", "exported", "gated_out"):
        if key in stats:
            out.append(f'askdata_sample_queue{{state="{key}"}} {int(stats[key])}')
    return out


def store_golden_cacheable() -> int:
    """已批准可复用的问法条数(读不出就 0, 不影响其余指标抓取)。"""
    from app.askdata import store

    try:
        return int(store.golden_cacheable_count())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[askdata] /metrics 读可复用条数失败: %s", exc)
        return 0


def _help(name: str) -> str:
    return f"# HELP {name} {_HELP.get(name, '')}"


def _semantic_lines() -> list[str]:
    """当前语义层版本(做成一个值恒为 1 的 info 指标, 版本串放在 label 里)。"""
    from app.config import get_settings

    settings = get_settings()
    if not settings.askdata_enabled:
        return []
    try:
        from app.semantic import registry

        catalog = registry.get_catalog()
    except Exception as exc:  # noqa: BLE001 - 库不可用时这个指标缺就行, 不拖整份文本
        logger.debug("[askdata] /metrics 读语义层快照失败: %s", exc)
        return []
    payload = catalog.to_payload()
    missing = ",".join(payload.get("missing_tables") or [])
    # 这里不用 .format(): 样例行里已经有 `{semver="..."}` 这种字面大括号, .format 会把
    # `{semver=` 当成字段名报 "unexpected '{' in field name"(实测试过: 直接 500)。
    info = ('askdata_semantic_info{semver="' + catalog.semver + '",origin="' + catalog.origin
            + '",missing="' + missing + '"} 1')
    return [
        _help("askdata_semantic_info"),
        "# TYPE askdata_semantic_info gauge",
        info,
        "# HELP askdata_semantic_metrics Number of active metrics in the semantic layer.",
        "# TYPE askdata_semantic_metrics gauge",
        f"askdata_semantic_metrics {int(payload.get('metric_count') or 0)}",
        "# HELP askdata_semantic_row_policies Enabled row policies pushed down by the compiler.",
        "# TYPE askdata_semantic_row_policies gauge",
        f"askdata_semantic_row_policies {int(payload.get('row_policies') or 0)}",
    ]


def _alert_lines(stats: dict[str, Any], *, days: int) -> list[str]:
    from app.observability import alerts

    fired = alerts.evaluate(stats)
    out = [_help("askdata_alert_firing"), "# TYPE askdata_alert_firing gauge"]
    for rule in fired:
        out.append(
            f'askdata_alert_firing{{rule="{rule["rule"]}",window="days{days}"}} '
            f'{1 if rule["fired"] else 0}'
        )
    return out


def _golden_count() -> int:
    try:
        from app.askdata import store

        return int(store.golden_count())
    except Exception:  # noqa: BLE001
        return 0
