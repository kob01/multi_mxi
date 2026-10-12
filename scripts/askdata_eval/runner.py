"""评测执行: 两条模式产出"实际结果", 判据交给 :mod:`scripts.askdata_eval.judge`。

- ``dsl`` 模式: golden 里存好的 DSL 直接编译 + 执行。整条链路没有 LLM, 所以同一份代码
  必然同一个结论, 适合当回归门禁; 它测的是**编译器/校验器/语义层**有没有把结果改坏。
- ``agent`` 模式: 走完整状态机(槽位抽取 + 三路链接 + DSL 生成 + 编译 + 取数)。
  它需要 askdata MCP server 可达(取语义层与取数都在那边), 测的是"人话到数字"整链路。

两条模式都用同一个全员作用域(``resolve_scope_sync``), 与 golden 灌入时一致 ——
换了作用域就会比出"权限差异当准确率差异"的假失败。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import get_settings
from app.db import sync as dbsync
from app.db.rls import read_role
from app.db.scope import DataScope, ScopeError, resolve_scope_sync
from app.semantic import compiler
from app.semantic.dsl import parse_dsl
from app.semantic.registry import get_catalog
from app.semantic.validate import CallerScope, validate

logger = logging.getLogger(__name__)

EVAL_USER = "eval-runner"
EVAL_ROLE = "admin"


def eval_scope() -> DataScope:
    return resolve_scope_sync(EVAL_USER, EVAL_ROLE)


def _execute(compiled: compiler.Compiled, scope: DataScope) -> dict[str, Any]:
    settings = get_settings()
    payload, _scan = dbsync.execute_scoped_select(
        compiled.sql, compiled.params, db_role=read_role(),
        session_settings=scope.session_settings(),
        max_scan_rows=settings.sqlguard_max_scan_rows,
    )
    block = payload[0] if payload else {"columns": [], "rows": [], "rowcount": 0}
    return {
        "columns": list(block.get("columns") or []),
        "rows": list(block.get("rows") or [])[:20],
        "rowcount": int(block.get("rowcount") or 0),
        "route": compiled.route,
        "sql": compiled.sql,
    }


def run_dsl(case: dict[str, Any], *, scope: DataScope | None = None) -> dict[str, Any]:
    """编译 + 执行 golden 里存好的 DSL。"""
    started = time.monotonic()
    catalog = get_catalog()
    scope = scope or eval_scope()
    try:
        spec = parse_dsl(case.get("dsl") or {})
        check = validate(spec, catalog=catalog, scope=CallerScope.from_scope(scope),
                         max_rows=get_settings().askdata_max_rows,
                         max_time_span_days=get_settings().askdata_max_time_span_days)
        if not check.ok:
            return _error(check.as_text(), started)
        compiled = compiler.compile_analytical(spec, catalog=catalog, scope=scope, check=check,
                                              max_rows=get_settings().askdata_max_rows)
        out = _execute(compiled, scope)
        out["latency_ms"] = int((time.monotonic() - started) * 1000)
        return out
    except (compiler.CompileError, ScopeError) as exc:
        return _error(f"{type(exc).__name__}: {exc}", started)
    except Exception as exc:  # noqa: BLE001 - 单条用例失败不能毁掉整轮评测
        logger.exception("用例执行异常: %s", case.get("question"))
        return _error(f"{type(exc).__name__}: {str(exc)[:200]}", started)


def _error(reason: str, started: float, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"columns": [], "rows": [], "rowcount": 0, "route": "", "sql": "",
                           "error": reason[:400],
                           "latency_ms": int((time.monotonic() - started) * 1000)}
    out.update(extra or {})
    return out


async def run_agent(case: dict[str, Any], *, agent: Any = None) -> dict[str, Any]:
    """端到端跑一轮问数状态机(需要 askdata MCP server 与 LLM 都可达)。

    ``agent`` 传入则复用同一个实例: 每条用例新建一个 AskDataAgent 就等于每条都重连
    一次 MCP 并重建工具清单, 几百条用例会先被自己的冷启开销拖死。
    """
    from app.agents.askdata_agent.executor import AskDataAgent
    from app.schemas import Role
    from app.security.caller import Caller, reset_caller, set_caller

    started = time.monotonic()
    token = set_caller(Caller(user_id=EVAL_USER, role=EVAL_ROLE,
                              intent_text=case.get("question", ""), trace_id=f"eval-{case.get('case_id', '')}"))
    try:
        runner_agent = agent or AskDataAgent()
        answer, payload = await runner_agent.invoke(
            case.get("question", ""), user_id=EVAL_USER, role=Role(EVAL_ROLE),
            trace_id=f"eval-{case.get('case_id', '')}", session_id="askdata-eval",
        )
    except Exception as exc:  # noqa: BLE001 - 同上: 单条不毁整轮
        logger.exception("agent 模式执行异常")
        return _error(f"{type(exc).__name__}: {str(exc)[:200]}", started)
    finally:
        reset_caller(token)
    verdict = str(payload.get("verdict") or "")
    kind = str(payload.get("kind") or "")
    if not verdict:
        # 澄清除非轮在载荷里写的是 `kind=clarify` 而不是 `verdict`。不补这一步,
        # 评测会把"反问了"当成"结果缺列"报(本轮实测过: 10 条实际是澄清, 而报告写着"澄清 0 条")。
        verdict = {"clarify": "clarify", "refuse": "fail", "degraded": "degraded"}.get(kind, "")
    common = {
        "verdict": verdict,
        "kind": kind,
        "slots": payload.get("slots") or {},
        "candidates": payload.get("metrics") or [],
        "cache_hit": bool(payload.get("cache_hit")),
        "semver": str(payload.get("semver") or ""),
        "policies": list(payload.get("policies") or []),
        "token_cost": int(payload.get("token_cost") or 0),
    }
    if verdict != "ok":
        # 澄清轮要把候选带回去: 误澄清的判据就是"被拦下了, 但最高分候选恰好就是期望口径"。
        return _error(f"未出数(verdict={verdict or '空'}, route={payload.get('route')}): "
                      f"{'; '.join(payload.get('error_codes') or []) or (answer[:160] or '')}",
                      started, extra=common)
    return {
        "columns": list(payload.get("columns") or []),
        "rows": list(payload.get("rows") or [])[:20],
        "rowcount": int(payload.get("rowcount") or 0),
        "route": str(payload.get("route") or ""),
        "sql": str(payload.get("sql") or ""),
        "dsl": payload.get("dsl") or {},
        "latency_ms": int((time.monotonic() - started) * 1000),
        **common,
    }


async def probe_agent_mode() -> str:
    """跑 agent 模式前的健康探测: 不通过就直接降级, 不要 500 条用例全超时后才报告失败。"""
    catalog = get_catalog()
    if catalog.origin != "db":
        return (f"语义层快照来自兜底集(origin={catalog.origin}): askdata-mcp 不可达或库未灌入, "
                "agent 模式没有意义")
    return ""
