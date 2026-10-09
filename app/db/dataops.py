"""层 2+4+6: 写通道的服务端实现(模板生成 SQL、影响行数梯度、软删、镜像、审批、审计)。

模型永远拿不到这一层的 SQL 文本拼装权: 它给 :mod:`app.db.datadsl` 的结构化计划,
这里查名单、注入域谓词、绑定参数、跑预演、按梯度决定"谁批准才能执行", 最后把变更
与变更前镜像放进同一个事务落库。

四条不可让的口径:
1. **值永不进 SQL 文本**: 本文件里出现的 f-string 只拼"来自固定字典的标识符 + 冒号占位符"。
2. **DELETE 不是物理删除**: ``action=delete`` 一律改写成软删 UPDATE; 真删只由保留期任务
   在归档之后执行, 且那个角色没有被授过 DELETE。
3. **没有"确认一下就执行"的一刀切**: 0 / 1~N / N+1~M / >M 四档各有去向, 审批人必须
   与发起人不同。
4. **每一笔都留得下证据**: 计划、最终 SQL、参数、预估行数、影响行数、决策与审批人都
   进 ``sql_audit_records``, 同时进 JSONL 给 SIEM; 变更前镜像支持回滚。

执行连接是同步的(FastMCP 工具是普通函数), REST 审批台在同一进程里用 threadpool 调。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import sync as dbsync
from app.db.ast_guard import ASTGuardError, WRITE_MODE, estimate_scan_rows, validate_sql
from app.db.datadsl import DslError, Filter, has_write_intent, parse_plan, predicate_sql
from app.db.models import (
    DataOpBeforeImage,
    Department,
    PendingDataOp,
    SqlAuditRecord,
)
from app.db.policy import WRITE_ENTITIES, PolicyError, WriteSpec, spec_for
from app.db.rls import write_role
from app.db.scope import DataScope, ScopeError, resolve_scope_sync

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


def _utcnow() -> datetime:
    """带时区的当前时刻(与 app/db/models.py 的列定义同一口径)。"""
    return datetime.now(timezone.utc)


# 决策/状态字面量(与 models 的注释同一套)。
PENDING_CONFIRM = "PENDING_CONFIRM"
PENDING_APPROVAL = "PENDING_APPROVAL"
NEED_REVIEW = "NEED_REVIEW"
DENIED = "DENIED"
EXECUTED = "EXECUTED"
EXPIRED = "EXPIRED"
FAILED = "FAILED"

_DATEISH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class DataOpsError(ValueError):
    """写计划在本层被拒(拒因都是确定性的, 原样回给模型与用户)。"""


@dataclass(frozen=True)
class CompiledOp:
    """一个已经可以执行(但还没批准)的写计划。"""

    table: str
    entity: str
    label: str
    action: str
    sql: str
    params: dict[str, Any]
    count_sql: str
    select_sql: str
    preview: str
    dsl_json: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    scope: DataScope | None = None


# ---------------------------------------------------------------------------
# 模板生成
# ---------------------------------------------------------------------------
def _cn_condition(filt: Filter) -> str:
    """把一条过滤条件说成人话(给人看的回显, 不参与 SQL 生成)。"""
    field_label = filt.field
    dateish = (
        filt.field.endswith(("_at", "_date"))
        or isinstance(filt.value, str)
        and _DATEISH_RE.match(filt.value or "") is not None
    )
    if filt.op.value == "eq":
        return f"{field_label} 为 {filt.value}"
    if filt.op.value == "ne":
        return f"{field_label} 不为 {filt.value}"
    if filt.op.value == "is_null":
        return f"{field_label} 为空"
    if filt.op.value == "in":
        return f"{field_label} 属于 {{{', '.join(str(v) for v in filt.value)}}}"
    word = {"lt": "早于" if dateish else "小于", "lte": "不晚于" if dateish else "不大于",
            "gt": "晚于" if dateish else "大于", "gte": "不早于" if dateish else "不小于"}[filt.op.value]
    return f"{field_label} {word} {filt.value}"


def compile_plan(plan: Any, scope: DataScope, *, nl_question: str = "") -> CompiledOp:
    """把 DSL 计划编译成"模板 SQL + 绑定参数 + 人话回显"。

    三条注入是固定的: 作用域谓词(层 1)、``is_deleted = false``(不把已停用的行再算进去)、
    软删的 SET 子句(层 4-3)。模型给的条件只能作为**附加**谓词出现在它们之间。
    """
    spec: WriteSpec = spec_for(plan.entity)
    table = spec.table

    where_parts: list[str] = []
    params: dict[str, Any] = dict(scope.scope_params())  # 域谓词的值: 只走绑参通道
    for i, filt in enumerate(plan.filters):
        if not _IDENT_RE.match(filt.field):
            raise DataOpsError(f"非法字段名: {filt.field!r}")
        ph_name, names, values = predicate_sql(filt, f"p{i}")
        if not names:
            where_parts.append(ph_name)
            continue
        for name, value in zip(names, values):
            params[name] = value
        where_parts.append(ph_name)

    if spec.soft_delete:
        where_parts.append("is_deleted = false")
    where_parts.append(scope.scope_clause())
    where_clause = " AND ".join(where_parts)

    if plan.action == "delete":
        set_parts = ["is_deleted = TRUE", "deleted_at = now()"]
        set_sql = ", ".join(set_parts)
        params["scope_actor"] = scope.user_id or "unknown"
        verb = "停用"
        set_clause = f"{set_sql}, deleted_by = :scope_actor"
    else:
        set_parts = []
        for i, (key, value) in enumerate(sorted(plan.sets.items())):
            if not _IDENT_RE.match(key):
                raise DataOpsError(f"非法字段名: {key!r}")
            name = f"s{i}"
            set_parts.append(f"{key} = :{name}")
            params[name] = value
        set_clause = ", ".join(set_parts)
        verb = "更新"

    sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
    count_sql = f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"
    # 变更前镜像的取行语句: 与写语句同一 WHERE, 并锁住命中的行(FOR UPDATE)。
    select_sql = f"SELECT * FROM {table} WHERE {where_clause} FOR UPDATE"

    conditions = "、".join(_cn_condition(f) for f in plan.filters)
    retention = get_settings().dataops_retention_days
    preview = (
        f"即将{verb} {spec.label}({table}) 的记录, 范围: {scope.department}"
        f"(dept_id={scope.dept_scope_text()}), 条件: {conditions}。"
    )
    if plan.action == "delete":
        preview += f"本操作为软删除, {retention} 天内可回滚。确认执行？"
    else:
        changes = "、".join(f"{k}={v}" for k, v in sorted(plan.sets.items()))
        preview += f"变更内容: {changes}。确认执行？"

    return CompiledOp(
        table=table,
        entity=spec.entity,
        label=spec.label,
        action=plan.action,
        sql=sql,
        params=params,
        count_sql=count_sql,
        select_sql=select_sql,
        preview=preview,
        dsl_json=plan.model_dump(mode="json"),
        reason=getattr(plan, "reason", "") or "",
        scope=scope,
    )


# ---------------------------------------------------------------------------
# 预演(dry-run)与梯度
# ---------------------------------------------------------------------------
def _dry_run(compiled: CompiledOp) -> tuple[int, dict[str, Any]]:
    """用同一份 WHERE 跑一次 COUNT, 并顺手取 EXPLAIN 预估扫描行数。

    这两件事都在"生效角色 + 作用域"的约束下做: 预演看到的行数必须与真实执行看到的
    行数同源, 否则梯度判定是在猜。
    """
    scope = compiled.scope
    settings = get_settings()
    with dbsync.scoped_connection(
        db_role=write_role(),
        session_settings=scope.session_settings() if scope else None,
        timeout_ms=settings.dataops_statement_timeout_ms,
    ) as conn:
        est_rows = int(conn.execute(text(compiled.count_sql), compiled.params).scalar_one() or 0)
        cost = {"explain": None, "estimated_scan_rows": None}
        scan = estimate_scan_rows(conn, compiled.count_sql, compiled.params)
        if scan is not None:
            cost["estimated_scan_rows"] = scan
            if scan > settings.sqlguard_max_scan_rows:
                raise DataOpsError(
                    f"成本预估超阈值(预估扫描 {scan} 行 > {settings.sqlguard_max_scan_rows}), "
                    "已拒; 请缩小条件范围"
                )
    return est_rows, cost


def decide_tier(est_rows: int) -> tuple[str, str]:
    """影响行数 -> ``(状态, 说明)``。梯度是层 4 的核心, 不能一刀切。"""
    s = get_settings()
    auto, approval = s.dataops_auto_execute_max_rows, s.dataops_approval_max_rows
    if est_rows <= 0:
        return NEED_REVIEW, "命中 0 行: 条件很可能写错了(时间范围/状态值), 请回显核对后重试"
    if est_rows <= auto:
        return PENDING_CONFIRM, f"命中 {est_rows} 行(<= {auto}), 需发起人二次确认"
    if est_rows <= approval:
        return PENDING_APPROVAL, f"命中 {est_rows} 行, 超出自动档, 需人工审批"
    return DENIED, f"命中 {est_rows} 行 > {approval}, 本通道不处理, 请提工单"


# ---------------------------------------------------------------------------
# 审计落库(层 6)
# ---------------------------------------------------------------------------
def record_audit(
    *,
    trace_id: str,
    scope: DataScope | None,
    user_id: str,
    role: str,
    nl_question: str,
    generated: str,
    final_sql: str,
    decision: str,
    reason: str = "",
    approver_id: str = "",
    rows_affected: int = 0,
    before_image_ref: str = "",
    cost: dict[str, Any] | None = None,
) -> str:
    """写一条结构化审计行(与 JSONL 双写, 本表供回滚/追责/异常检测查询)。"""
    audit_id = uuid.uuid4().hex
    with Session(dbsync.get_sync_engine()) as session:
        session.add(
            SqlAuditRecord(
                audit_id=audit_id,
                trace_id=trace_id or "",
                tenant_id=(scope.tenant_id if scope else ""),
                dept_id=(scope.dept_scope_text() if scope else ""),
                user_id=user_id or "",
                role=role or "",
                nl_question=nl_question or "",
                generated_sql=generated,
                final_sql=final_sql,
                policy_decision=decision,
                decision_reason=reason,
                approver_id=approver_id,
                rows_affected=rows_affected,
                before_image_ref=before_image_ref,
                cost_estimate=cost or None,
                engine="postgresql",
            )
        )
        session.commit()
    _audit_jsonl(
        trace_id,
        "dataops_audit",
        {"audit_id": audit_id, "decision": decision, "reason": reason,
         "user_id": user_id, "role": role, "rows_affected": rows_affected,
         "final_sql": final_sql[:400]},
    )
    return audit_id


def _audit_jsonl(trace_id: str, action: str, detail: dict[str, Any]) -> None:
    """JSONL 审计通道(SIEM 采集口); 失败只降级不影响主流程。"""
    try:
        from app.security.audit import get_audit_logger

        get_audit_logger().log(trace_id or "", "analyst_agent", action, detail)
    except Exception as exc:  # noqa: BLE001 - 留痕通道自身不阻断写治理
        logger.warning("dataops 审计写入失败(%s): %s", action, exc)


def detect_anomalies(user_id: str, trace_id: str = "") -> list[dict[str, Any]]:
    """实时异常检测(层 6): 审计不是事后追责用的, 这三种形状要当场告警。

    1. 同一用户 5 分钟内写计划 > 3 次(批量误操作的典型形状);
    2. 同一用户 10 分钟内被拒 > 5 次(在试探防线);
    3. 出现过一次"作用域/跨部门"被拒(越权尝试, 一条就告警)。
    """
    if not user_id:
        return []
    now = _utcnow()
    anomalies: list[dict[str, Any]] = []
    try:
        with Session(dbsync.get_sync_engine()) as session:
            plan_count = session.scalar(
                text("SELECT COUNT(*) FROM sql_audit_records WHERE user_id = :u "
                     "AND ts >= :since AND policy_decision IN ('allow','approval')"),
                {"u": user_id, "since": now - timedelta(minutes=5)},
            )
            deny_count = session.scalar(
                text("SELECT COUNT(*) FROM sql_audit_records WHERE user_id = :u "
                     "AND ts >= :since AND policy_decision = 'deny'"),
                {"u": user_id, "since": now - timedelta(minutes=10)},
            )
            scope_denials = session.scalar(
                text("SELECT COUNT(*) FROM sql_audit_records WHERE user_id = :u "
                     "AND ts >= :since AND policy_decision = 'deny' "
                     "AND (decision_reason LIKE '%作用域%' OR decision_reason LIKE '%租户%' "
                     "OR decision_reason LIKE '%dept%')"),
                {"u": user_id, "since": now - timedelta(minutes=60)},
            )
        if int(plan_count or 0) > 3:
            anomalies.append({"kind": "burst_write_plans", "count": int(plan_count), "window_min": 5})
        if int(deny_count or 0) > 5:
            anomalies.append({"kind": "guard_denials_spike", "count": int(deny_count), "window_min": 10})
        if int(scope_denials or 0) > 0:
            anomalies.append({"kind": "cross_scope_attempt", "count": int(scope_denials), "window_min": 60})
    except Exception as exc:  # noqa: BLE001 - 异常检测失败不能挡住业务
        logger.warning("dataops 异常检测失败(忽略): %s", exc)
        return []
    for item in anomalies:
        _audit_jsonl(trace_id, "dataops_anomaly", {"user_id": user_id, **item})
        logger.warning("dataops 异常: user=%s %s", user_id, item)
    return anomalies


# ---------------------------------------------------------------------------
# 计划 / 确认 / 审批 / 执行
# ---------------------------------------------------------------------------
def _forbidden(reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": f"越权拒绝: {reason}", "forbidden": True}
    payload.update(extra)
    return payload


def _denied(reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": reason, "denied": True}
    payload.update(extra)
    return payload


def _writable_roles() -> set[str]:
    raw = (get_settings().dataops_writable_roles or "").lower()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _approver_roles() -> set[str]:
    raw = (get_settings().dataops_approver_roles or "").lower()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _expires_at() -> datetime:
    minutes = int(get_settings().dataops_pending_ttl_minutes)
    return _utcnow() + timedelta(minutes=max(1, minutes))


def plan_data_op(
    payload: dict | str,
    *,
    caller_user_id: str,
    caller_role: str,
    nl_question: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """模型唯一的写入口: 只生成计划, 不执行(执行必须再过确认/审批)。"""
    settings = get_settings()
    role = (caller_role or "").strip().lower()
    if not settings.dataops_enabled:
        return _denied("写通道当前关闭(DATAOPS_ENABLED=false), 只允许查询")
    if role not in _writable_roles():
        _audit_jsonl(trace_id, "dataops_role_denied",
                     {"user_id": caller_user_id, "role": role})
        return _forbidden(
            f"角色 {role} 无数据变更权限(可发起角色: {sorted(_writable_roles())})",
            user_id=caller_user_id,
        )
    # 层 5-C 的计划偏移检测(降噪层): 本轮原句不含写意图就不该出现写计划。
    if settings.dataops_write_intent_guard and not has_write_intent(nl_question):
        _audit_jsonl(trace_id, "plan_drift_blocked",
                     {"user_id": caller_user_id, "question": (nl_question or "")[:200]})
        return _denied(
            "本轮请求里没有明确的数据变更表述, 已拒绝生成写计划; "
            "如确需变更数据, 请让用户直接说明要改哪张表的什么条件。"
        )
    try:
        plan = parse_plan(payload)
    except DslError as exc:
        record_audit(
            trace_id=trace_id, scope=None, user_id=caller_user_id, role=role,
            nl_question=nl_question, generated=str(payload)[:2000] if not isinstance(payload, str) else payload[:2000],
            final_sql="", decision="deny", reason=f"DSL 校验失败: {exc}",
        )
        _audit_jsonl(trace_id, "dataops_dsl_rejected", {"reason": str(exc)})
        return _denied(f"写计划被拒: {exc}")
    try:
        scope = resolve_scope_sync(caller_user_id, role)
    except ScopeError as exc:
        return _denied(f"无法确定数据作用域: {exc}")
    compiled: CompiledOp | None = None
    try:
        compiled = compile_plan(plan, scope, nl_question=nl_question)
        # 层 3: 模板自校验(防模板退化 + 确认形状齐备)。normalize=False:
        # 执行的是自己写的 :name 模板, 不要让 sqlglot 重写占位符。
        validate_sql(
            compiled.sql, allowed_tables={compiled.table}, mode=WRITE_MODE, normalize=False
        )
        est_rows, cost = _dry_run(compiled)
    except (ASTGuardError, DataOpsError, PolicyError) as exc:
        record_audit(
            trace_id=trace_id, scope=scope, user_id=caller_user_id, role=role,
            nl_question=nl_question,
            generated=json.dumps(
                compiled.dsl_json if compiled is not None else plan.model_dump(mode="json"),
                ensure_ascii=False,
            ),
            final_sql=compiled.sql if compiled is not None else "",
            decision="deny", reason=str(exc),
        )
        return _denied(f"写计划被拒: {exc}")

    status, note = decide_tier(est_rows)
    decision = "approval" if status == PENDING_APPROVAL else (
        "deny" if status == DENIED else "allow"
    )
    expires = _expires_at()
    op_id = _persist(compiled, est_rows, cost, status, note,
                     trace_id=trace_id, nl_question=nl_question, scope=scope,
                     expires=expires)
    record_audit(
        trace_id=trace_id, scope=scope, user_id=caller_user_id, role=role,
        nl_question=nl_question, generated=json.dumps(compiled.dsl_json, ensure_ascii=False),
        final_sql=compiled.sql, decision=decision, reason=note, cost=cost,
    )
    detect_anomalies(caller_user_id, trace_id)
    if status == DENIED:
        return {"status": status, "op_id": op_id, "est_rows": est_rows,
                "preview": compiled.preview, "note": note, "denied": True}
    return {
        "status": status,
        "op_id": op_id,
        "est_rows": est_rows,
        "preview": compiled.preview,
        "reason": compiled.reason,
        "scope_note": scope.note(),
        "note": note,
        "expires_at": expires.isoformat(timespec="seconds"),
        "next_action": (
            "把 preview 原样回显给用户, 告知回复\u201c确认\u201d即可执行(confirm_data_op)"
            if status == PENDING_CONFIRM else
            "已提交人工审批, 请到\u201c数据变更审批\u201d页处理; 审批通过后由系统执行"
        ),
    }


def _persist(
    compiled: CompiledOp,
    est_rows: int,
    cost: dict[str, Any],
    status: str,
    note: str,
    *,
    trace_id: str,
    nl_question: str,
    scope: DataScope,
    expires: datetime | None = None,
) -> str:
    """把计划落进 dataops_pending(确认/审批与执行都只认库里的这一份)。"""
    op_id = uuid.uuid4().hex
    with Session(dbsync.get_sync_engine()) as session:
        session.add(
            PendingDataOp(
                op_id=op_id,
                trace_id=trace_id or "",
                tenant_id=scope.tenant_id,
                dept_scope=scope.dept_scope_text(),
                actor_user_id=scope.user_id,
                actor_role=scope.role,
                action=compiled.action,
                entity=compiled.entity,
                dsl_json=compiled.dsl_json,
                final_sql=compiled.sql,
                params_json={k: _jsonable(v) for k, v in compiled.params.items()},
                preview_text=compiled.preview,
                nl_question=nl_question or "",
                reason=compiled.reason,
                est_rows=est_rows,
                cost_estimate=cost or None,
                status=status,
                decision_reason=note,
                expires_at=expires or _expires_at(),
            )
        )
        session.commit()
    return op_id


def _jsonable(value: Any) -> Any:
    """参数要能进 JSON 列: datetime/date 转 ISO, 其余原样。"""
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _scope_of(op: PendingDataOp) -> DataScope:
    """按**存储时**的作用域重建执行上下文。

    审批人/确认人只决定"批不批", 不改变数据范围: 范围永远取计划里那一份, 否则会
    出现"经理审批却让数据按审批人的部门过滤"这种串味。
    """
    dept_ids = tuple(p for p in (op.dept_scope or "").split(",") if p)
    return DataScope(
        tenant_id=op.tenant_id,
        dept_ids=dept_ids,
        all_depts=False,
        user_id=op.actor_user_id,
        role=op.actor_role,
        department=_department_label(dept_ids),
    )


def _department_label(dept_ids: tuple[str, ...]) -> str:
    try:
        with Session(dbsync.get_sync_engine()) as session:
            names = session.scalars(
                select(Department.name).where(Department.dept_id.in_(list(dept_ids)))
            ).all()
        return "、".join(names) or "本部门"
    except Exception:  # noqa: BLE001 - 回显用名取不到不影响执行
        return "本部门"


def _load_op(session: Session, op_id: str) -> PendingDataOp | None:
    return session.get(PendingDataOp, op_id)


def _expired(op: PendingDataOp) -> bool:
    return op.expires_at is not None and op.expires_at < _utcnow()


def execute_op(
    op: PendingDataOp,
    *,
    trace_id: str = "",
    approver_id: str = "",
    decided_by: str = "",
) -> dict[str, Any]:
    """真正执行: 一个事务内取镜像 -> 变更 -> 记行数; 全成或全不动。

    锁与镜像的顺序是刻意的: 先 ``SELECT ... FOR UPDATE`` 锁住命中行再写, 保证
    "被镜像的行"与"被改的行"是同一批 —— 反过来做会得到一份对不上账的快照。

    ``decided_by`` 只进审计理由(谁批的这件事已有 approver_id / 审计行), 不给
    ``dataops_pending`` 加第二套"决策人"列: 同一个事实只该有一个存放位置。
    """
    settings = get_settings()
    recompiled = _recompile(op)
    scope = recompiled.scope or _scope_of(op)
    table = recompiled.table
    params = dict(recompiled.params)
    image_ref = ""
    try:
        with dbsync.scoped_connection(
            db_role=write_role(),
            session_settings=scope.session_settings(),
            timeout_ms=settings.dataops_statement_timeout_ms,
        ) as conn:
            # 变更前镜像: 与写语句同一 WHERE、同一事务、同一作用域, FOR UPDATE 锁住这一批。
            locked = conn.execute(text(recompiled.select_sql), params).mappings().all()
            rows = [dict(r) for r in locked]
            if not rows:
                raise DataOpsError("执行时已无命中行(数据可能已被他人变更), 本次不动任何数据")
            for r in rows:
                pk = {k: v for k, v in r.items() if k in _pk_columns(table)}
                conn.execute(
                    text("INSERT INTO dataop_before_image (op_id, table_name, pk_json, row_json, captured_at) "
                         "VALUES (:op_id, :table_name, CAST(:pk_json AS JSON), CAST(:row_json AS JSON), now())"),
                    {
                        "op_id": op.op_id,
                        "table_name": table,
                        "pk_json": json.dumps(_jsonify(pk), ensure_ascii=False, default=str),
                        "row_json": json.dumps(_jsonify(r), ensure_ascii=False, default=str),
                    },
                )
            image_ref = op.op_id
            result = conn.execute(text(recompiled.sql), params)
            affected = int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 - 执行失败要把状态留在库里并可追溯
        _update_op(op.op_id, status=FAILED, decision_reason=f"执行失败: {exc}")
        record_audit(
            trace_id=trace_id, scope=scope, user_id=op.actor_user_id, role=op.actor_role,
            nl_question=op.nl_question, generated=json.dumps(op.dsl_json, ensure_ascii=False),
            final_sql=op.final_sql, decision="deny", reason=f"执行失败: {exc}",
            approver_id=approver_id, before_image_ref=image_ref,
        )
        raise DataOpsError(f"执行失败: {exc}") from exc

    _update_op(
        op.op_id, status=EXECUTED, rows_affected=affected, approver_id=approver_id,
        before_image_ref=image_ref, executed_at=_utcnow(),
        decision_reason=f"已执行({decided_by or 'actor'}), 影响 {affected} 行",
    )
    record_audit(
        trace_id=trace_id or op.trace_id, scope=scope, user_id=op.actor_user_id,
        role=op.actor_role, nl_question=op.nl_question,
        generated=json.dumps(op.dsl_json, ensure_ascii=False),
        final_sql=op.final_sql, decision="allow",
        reason=f"已执行, 影响 {affected} 行", approver_id=approver_id,
        rows_affected=affected, before_image_ref=image_ref,
    )
    return {
        "status": EXECUTED,
        "op_id": op.op_id,
        "rows_affected": affected,
        "preview": op.preview_text,
        "rollback_hint": f"7 天内可用 rollback_data_op(op_id={op.op_id}) 按镜像还原",
    }


def _recompile(op: PendingDataOp) -> CompiledOp:
    """按库里的 DSL + 作用域重新编译一次, 并钉住"审批看到的 SQL == 执行的 SQL"。

    重新编译是确定性的(同一份 dsl_json + 同一个 scope 得到同一串文本), 所以任何不一致
    都说明存量行被改过或模板已变 —— 那种情况下宁可不执行。参数也从编译器取而不从
    库里的 params_json 取: 避免"存储形式"变成第二个真相。
    """
    scope = _scope_of(op)
    plan = parse_plan(op.dsl_json or {})
    compiled = compile_plan(plan, scope, nl_question=op.nl_question or "")
    if compiled.sql != op.final_sql:
        raise DataOpsError(
            "执行前自校验失败: 重新编译出的 SQL 与库存计划不一致(模板或数据被改动), 已停止"
        )
    return compiled


def confirm_data_op(
    op_id: str, *, caller_user_id: str, caller_role: str, trace_id: str = ""
) -> dict[str, Any]:
    """发起人二次确认: 只有"同一发起人 + 同一计划 + 未过期 + 仍在自动档"才执行。"""
    settings = get_settings()
    role = (caller_role or "").strip().lower()
    with Session(dbsync.get_sync_engine()) as session:
        op = _load_op(session, op_id)
    if op is None:
        return _denied(f"写计划 {op_id} 不存在(可能已过期清理)")
    if op.status not in (PENDING_CONFIRM, PENDING_APPROVAL):
        return _denied(f"写计划当前状态是 {op.status}, 不能重复确认")
    if _expired(op):
        _update_op(op.op_id, status=EXPIRED, decision_reason="确认令牌已过期")
        return _denied("确认令牌已过期, 请重新发起")
    if op.actor_user_id != (caller_user_id or "").strip():
        _audit_jsonl(trace_id, "dataops_confirm_denied_actor",
                     {"op_id": op_id, "asked_by": caller_user_id, "actor": op.actor_user_id})
        return _forbidden("只有发起人本人能确认这个写计划")
    if role not in _writable_roles():
        return _forbidden(f"角色 {role} 无数据变更权限")
    # 执行前复核影响行数: 从"生成计划"到"确认"之间数据可能变了, 跳了档就得升级去向。
    recompiled = _recompile(op)
    scope = recompiled.scope or _scope_of(op)
    with dbsync.scoped_connection(
        db_role=write_role(), session_settings=scope.session_settings(),
        timeout_ms=settings.dataops_statement_timeout_ms,
    ) as conn:
        est_rows = int(
            conn.execute(text(recompiled.count_sql), recompiled.params).scalar_one() or 0
        )
    status, note = decide_tier(est_rows)
    if status == DENIED:
        _update_op(op.op_id, status=DENIED, est_rows=est_rows, decision_reason=note)
        return _denied(f"执行前复核: {note}")
    if status == PENDING_APPROVAL and op.status == PENDING_CONFIRM:
        _update_op(op.op_id, status=PENDING_APPROVAL, est_rows=est_rows, decision_reason=note)
        return {
            "status": PENDING_APPROVAL, "op_id": op.op_id, "est_rows": est_rows,
            "preview": op.preview_text, "note": f"复核后影响行数升到审批档: {note}",
        }
    if op.status == PENDING_APPROVAL:
        return _denied("该计划需人工审批, 请到数据变更审批页处理(不能由发起人自行确认)")
    fresh = _reload(op.op_id)
    return execute_op(fresh, trace_id=trace_id, decided_by="actor_confirm")


def approval_allowed(approver_role: str, approver_user_id: str, actor_user_id: str) -> str | None:
    """审批资格的纯判定(返回拒因或 None)。

    抽出来只为了能被离线断言: “审批人不能是发起人”是层 4 里最容易被改错的一条
    (它看起来可省), 实际是防止“自己改自己批”的唯一屏障。
    """
    role = (approver_role or "").strip().lower()
    if role not in _approver_roles():
        return f"角色 {approver_role} 无权审批数据变更"
    if (approver_user_id or "").strip() and (
        (approver_user_id or "").strip() == (actor_user_id or "").strip()
    ):
        return "审批人不能与发起人相同"
    return None


def approve_data_op(
    op_id: str, *, approver_user_id: str, approver_role: str, note: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """人工审批通过: 审批人角色要合法, 且**必须不是发起人**。"""
    with Session(dbsync.get_sync_engine()) as session:
        op = _load_op(session, op_id)
    if op is None:
        return _denied(f"写计划 {op_id} 不存在")
    reason = approval_allowed(approver_role, approver_user_id, op.actor_user_id)
    if reason:
        return _forbidden(reason)
    if op.status not in (PENDING_APPROVAL, PENDING_CONFIRM):
        return _denied(f"当前状态 {op.status} 不可审批")
    if _expired(op):
        _update_op(op.op_id, status=EXPIRED, decision_reason="审批令牌已过期")
        return _denied("该计划已过期, 请重新发起")
    record_audit(
        trace_id=trace_id or op.trace_id, scope=_scope_of(op), user_id=op.actor_user_id,
        role=op.actor_role, nl_question=op.nl_question,
        generated=json.dumps(op.dsl_json, ensure_ascii=False), final_sql=op.final_sql,
        decision="approval", reason=f"审批通过: {note or '(无备注)'}",
        approver_id=approver_user_id,
    )
    fresh = _reload(op.op_id)
    result = execute_op(fresh, trace_id=trace_id, approver_id=approver_user_id,
                        decided_by="approval")
    _update_op(op_id, approve_note=note, approver_id=approver_user_id)
    return result


def reject_data_op(
    op_id: str, *, approver_user_id: str, approver_role: str, note: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """审批驳回: 只改状态, 不碰数据。"""
    with Session(dbsync.get_sync_engine()) as session:
        op = _load_op(session, op_id)
    if op is None:
        return _denied(f"写计划 {op_id} 不存在")
    reason = approval_allowed(approver_role, approver_user_id, op.actor_user_id)
    if reason:
        return _forbidden(reason)
    if op.status not in (PENDING_APPROVAL, PENDING_CONFIRM):
        return _denied(f"当前状态 {op.status} 不可驳回")
    _update_op(op.op_id, status=DENIED, approver_id=approver_user_id,
               approve_note=note or "(未填理由)", decision_reason="人工驳回")
    record_audit(
        trace_id=trace_id or op.trace_id, scope=_scope_of(op), user_id=op.actor_user_id,
        role=op.actor_role, nl_question=op.nl_question,
        generated=json.dumps(op.dsl_json, ensure_ascii=False), final_sql=op.final_sql,
        decision="deny", reason=f"人工驳回: {note}", approver_id=approver_user_id,
    )
    return {"status": DENIED, "op_id": op_id, "note": "已驳回, 未改动任何数据"}


def list_ops(
    *, user_id: str, role: str, status: str = "", limit: int = 30
) -> list[dict[str, Any]]:
    """待办列表: 本人发起的全部可见; 有审批权的角色额外能看到全部待办。"""
    role = (role or "").strip().lower()
    can_approve = role in _approver_roles()
    stmt = select(PendingDataOp).order_by(PendingDataOp.created_at.desc()).limit(
        max(1, min(200, int(limit or 30)))
    )
    if status:
        stmt = stmt.where(PendingDataOp.status == status.upper())
    with Session(dbsync.get_sync_engine()) as session:
        ops = session.scalars(stmt).all()
    out: list[dict[str, Any]] = []
    for op in ops:
        mine = op.actor_user_id == (user_id or "")
        pending = op.status in (PENDING_CONFIRM, PENDING_APPROVAL, NEED_REVIEW)
        if not (mine or (can_approve and pending)):
            continue
        out.append(_op_dict(op, mine=mine, can_approve=can_approve))
    return out


def recent_audit_records(
    *, actor_user_id: str = "", requester: str = "", requester_role: str = "",
    limit: int = 30,
) -> list[dict[str, Any]]:
    """审计回查(层 6): 普通角色只能看自己的记录, 审批角色可看全部。

    把"谁能读审计"当成与"谁能写数据"同级的问题: 审计行里带着 SQL、用户原句、
    影响行数, 本身就是敏感数据。
    """
    can_see_all = (requester_role or "").strip().lower() in _approver_roles()
    who = (actor_user_id or "").strip() if can_see_all else (requester or "").strip()
    stmt = select(SqlAuditRecord).order_by(SqlAuditRecord.ts.desc()).limit(
        max(1, min(200, int(limit or 30)))
    )
    if who:
        stmt = stmt.where(SqlAuditRecord.user_id == who)
    with Session(dbsync.get_sync_engine()) as session:
        rows = session.scalars(stmt).all()
    return [
        {
            "audit_id": r.audit_id,
            "trace_id": r.trace_id,
            "ts": r.ts.isoformat(timespec="seconds") if r.ts else "",
            "user_id": r.user_id,
            "role": r.role,
            "tenant_id": r.tenant_id,
            "dept_id": r.dept_id,
            "nl_question": r.nl_question,
            "generated_sql": r.generated_sql,
            "final_sql": r.final_sql,
            "policy_decision": r.policy_decision,
            "decision_reason": r.decision_reason,
            "approver_id": r.approver_id,
            "rows_affected": r.rows_affected,
            "before_image_ref": r.before_image_ref,
            "cost_estimate": r.cost_estimate,
        }
        for r in rows
    ]


def list_before_images(op_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """取变更前镜像摘要(审批人要看到"到底改哪几行、改前长什么样")。

    只回主键 + 可写字段的旧值, 不回整行: 整行里带着其他部门的联系人/银行账户这类不属于
    本次变更范围的内容, 把它们交给另一个部门的审批人就是一次横向泄露。
    """
    with Session(dbsync.get_sync_engine()) as session:
        op = session.get(PendingDataOp, op_id)
        images = session.scalars(
            select(DataOpBeforeImage)
            .where(DataOpBeforeImage.op_id == op_id)
            .order_by(DataOpBeforeImage.image_id)
            .limit(max(1, min(200, int(limit or 20))))
        ).all()
    if op is None:
        return []
    try:
        fields = sorted(spec_for(op.entity).writable_fields)
    except PolicyError:
        fields = []
    out: list[dict[str, Any]] = []
    for image in images:
        row = image.row_json or {}
        out.append({
            "pk": image.pk_json or {},
            "before": {k: row.get(k) for k in fields if k in row},
            "captured_at": image.captured_at.isoformat(timespec="seconds") if image.captured_at else "",
        })
    return out


def get_op(op_id: str, *, user_id: str, role: str) -> dict[str, Any]:
    role = (role or "").strip().lower()
    with Session(dbsync.get_sync_engine()) as session:
        op = _load_op(session, op_id)
    if op is None:
        return _denied(f"写计划 {op_id} 不存在")
    mine = op.actor_user_id == (user_id or "")
    return _op_dict(op, mine=mine, can_approve=role in _approver_roles())


def _op_dict(op: PendingDataOp, *, mine: bool, can_approve: bool) -> dict[str, Any]:
    return {
        "op_id": op.op_id,
        "status": op.status,
        "action": op.action,
        "entity": op.entity,
        "label": op.preview_text,
        "preview": op.preview_text,
        "final_sql": op.final_sql,
        "est_rows": op.est_rows,
        "rows_affected": op.rows_affected,
        "reason": op.reason,
        "nl_question": op.nl_question,
        "actor_user_id": op.actor_user_id,
        "actor_role": op.actor_role,
        "dept_scope": op.dept_scope,
        "approver_id": op.approver_id,
        "approve_note": op.approve_note,
        "decision_reason": op.decision_reason,
        "before_image_ref": op.before_image_ref,
        "created_at": op.created_at.isoformat(timespec="seconds") if op.created_at else "",
        "expires_at": op.expires_at.isoformat(timespec="seconds") if op.expires_at else "",
        "can_confirm": mine and op.status == PENDING_CONFIRM,
        "can_approve": can_approve and op.status == PENDING_APPROVAL and not mine,
    }


def _reload(op_id: str) -> PendingDataOp:
    with Session(dbsync.get_sync_engine()) as session:
        op = session.get(PendingDataOp, op_id)
    if op is None:
        raise DataOpsError(f"写计划 {op_id} 已不存在")
    return op


def _update_op(op_id: str, **fields: Any) -> None:
    with Session(dbsync.get_sync_engine()) as session:
        op = session.get(PendingDataOp, op_id)
        if op is None:
            return
        for key, value in fields.items():
            setattr(op, key, value)
        session.commit()


# ---------------------------------------------------------------------------
# 回滚与保留期清理
# ---------------------------------------------------------------------------
def rollback_data_op(op_id: str, *, user_id: str, role: str, trace_id: str = "") -> dict[str, Any]:
    """照变更前镜像写回去(层 4-4: 有镜像, 回滚就不是灾难而是重放)。"""
    role = (role or "").strip().lower()
    if role not in _writable_roles():
        return _forbidden(f"角色 {role} 无数据变更权限")
    with Session(dbsync.get_sync_engine()) as session:
        op = session.get(PendingDataOp, op_id)
        images = session.scalars(
            select(DataOpBeforeImage).where(DataOpBeforeImage.op_id == op_id)
        ).all()
    if op is None or op.status != EXECUTED:
        return _denied("只有已执行的计划可以回滚")
    if not images:
        return _denied("该计划没有变更前镜像, 无法回滚")
    table = images[0].table_name
    pk_columns = _pk_columns(table)
    spec = spec_for(op.entity)
    scope = _scope_of(op)
    restored = 0
    with dbsync.scoped_connection(
        db_role=write_role(), session_settings=scope.session_settings()
    ) as conn:
        for image in images:
            row = image.row_json or {}
            pk = image.pk_json or {}
            set_parts, params = [], {}
            for i, (key, value) in enumerate(sorted(row.items())):
                if key not in spec.writable_fields and key not in ("is_deleted", "deleted_at", "deleted_by"):
                    continue
                if not _IDENT_RE.match(key):
                    continue
                set_parts.append(f"{key} = :v{i}")
                params[f"v{i}"] = value
            if not set_parts:
                continue
            where_parts, pks = [], {}
            for i, key in enumerate(sorted(pk)):
                where_parts.append(f"{key} = :k{i}")
                pks[f"k{i}"] = pk[key]
            sql = (
                f"UPDATE {table} SET {', '.join(set_parts)} "
                f"WHERE {' AND '.join(where_parts)} AND tenant_id = :scope_tenant "
                "AND dept_id = ANY(string_to_array(:scope_depts, ','))"
            )
            conn.execute(
                text(sql),
                {**params, **pks,
                 "scope_tenant": scope.tenant_id, "scope_depts": scope.dept_scope_text()},
            )
            restored += 1
    record_audit(
        trace_id=trace_id or op.trace_id, scope=scope, user_id=user_id, role=role,
        nl_question=op.nl_question, generated=f"rollback {op_id}",
        final_sql=f"按镜像回写 {restored} 行到 {table}", decision="allow",
        reason="回滚变更前镜像", rows_affected=restored, before_image_ref=op_id,
    )
    _update_op(op_id, status="ROLLED_BACK", decision_reason=f"已回滚 {restored} 行")
    return {"status": "ROLLED_BACK", "op_id": op_id, "rows_restored": restored}


def purge_expired_soft_deletes() -> dict[str, Any]:
    """保留期到点的软删行 -> 搬进归档表 -> 物理删除(任务函数, 本轮不排程)。

    刻意不给任何角色 DELETE 权限: 这一步只能由属主(维护任务/人工运行)执行, 于是
    "智能体能物理删数据"在权限层就不成立, 而不只是在校验层被拒。
    """
    settings = get_settings()
    cutoff = _utcnow() - timedelta(days=max(1, settings.dataops_retention_days))
    moved = deleted = 0
    with Session(dbsync.get_sync_engine()) as session:
        for entity, spec in spec_table_pairs():
            table = spec.table
            rows = session.execute(
                text(f"SELECT * FROM {table} WHERE is_deleted = true AND deleted_at < :cutoff"),
                {"cutoff": cutoff},
            ).mappings().all()
            for r in rows:
                d = dict(r)
                pk = {k: d[k] for k in _pk_columns(table)}
                session.execute(
                    text("INSERT INTO dataop_archive (op_id, table_name, pk_json, row_json, "
                         "deleted_by, archived_at, purge_after) VALUES (:op_id, :table, "
                         "CAST(:pk AS JSON), CAST(:row AS JSON), :deleted_by, now(), "
                         "now() + interval '365 days')"),
                    {"op_id": "", "table": table,
                     "pk": json.dumps(_jsonify(pk), ensure_ascii=False, default=str),
                     "row": json.dumps(_jsonify(d), ensure_ascii=False, default=str),
                     "deleted_by": d.get("deleted_by") or ""},
                )
                moved += 1
                session.execute(
                    text(f"DELETE FROM {table} WHERE {' AND '.join(f'{k} = :{k}' for k in pk)}"),
                    {k: v for k, v in pk.items()},
                )
                deleted += 1
        session.commit()
    return {"archived": moved, "deleted": deleted, "cutoff": cutoff.isoformat()}


def spec_table_pairs():
    """可写实体清单(保留期清理遍历用)。"""
    return list(WRITE_ENTITIES.values())


_PK_CACHE: dict[str, tuple[str, ...]] = {}


def _pk_columns(table: str) -> tuple[str, ...]:
    """表主键列(镜像/归档/回滚都要按主键定位行)。"""
    if table not in _PK_CACHE:
        from app.db.models import Base

        pk = Base.metadata.tables[table].primary_key.columns.keys()
        _PK_CACHE[table] = tuple(pk)
    return _PK_CACHE[table]


def _jsonify(row: dict[str, Any]) -> dict[str, Any]:
    """镜像/归档要能进 JSON 列: Decimal/datetime 先转成可序列化形式。"""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif isinstance(value, (bytes, bytearray)):
            out[key] = value.decode("utf-8", errors="replace")
        else:
            try:
                json.dumps(value, default=str)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = str(value)
    return out
