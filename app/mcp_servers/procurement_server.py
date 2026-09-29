"""采购与合同 MCP Server (FastMCP, streamable-http transport)。

暴露三块能力给 Contract_Agent:
1. 采购申请单的创建/查询/合规初审(比价、供应商准入、预算余额三条硬规则);
2. 合同台账的送审与初审(规则引擎 + LLM 条款抽取分工, 见 app/procurement/rules.py);
3. 供应商主数据在册校验。

与 finance_server 的边界: 报销是"费用已发生后核销", 采购是"付款前的事前把关",
两者共用 fin_department_budgets 预算池, 但单据与状态机各自独立, 不互相冒充。

写操作纪律(与既有 server 一致): create_*/submit_*/save_* 一律不被 Tool Cache
缓存(前缀白名单不含它们), 初审结论每次实算, 防止把一次"提交成功"复用到下一次。

Run:
    python -m app.mcp_servers.procurement_server  # serves http://0.0.0.0:8006/mcp
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import sync as dbsync
from app.db.models import ContractReview, DepartmentBudget, PurchaseRequest, Supplier
from app.db.sql_guard import SQLGuardError
from app.procurement import rules

mcp = FastMCP("enterprise-procurement-contract", host="0.0.0.0", port=8006)

_CST = timezone(timedelta(hours=8))

ALLOWED_CATEGORIES = {"IT设备", "办公用品", "咨询服务", "市场推广", "培训服务", "其他"}
# Text2SQL 白名单: 采购两张业务表 + 供应商 + 预算池 + 员工主数据(部门/姓名关联)
ALLOWED_TABLES = {
    "proc_orders",
    "proc_contracts",
    "proc_suppliers",
    "fin_department_budgets",
    "hr_employees",
}


def _next_no(session: Session, table: str, prefix: str, column: str, start: int) -> str:
    """按前缀取下一单号: 只依赖 SUBSTRING 尾段数字, 与 fin/hr 的写法保持一致。"""
    max_no = session.execute(
        text(
            f"SELECT COALESCE(MAX(CAST(SUBSTRING({column}, {len(prefix) + 1}) AS INTEGER)), "
            f"{start - 1}) FROM {table}"
        )
    ).scalar_one()
    return f"{prefix}{int(max_no) + 1}"


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _dec(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def _order_dict(o: PurchaseRequest) -> dict[str, Any]:
    return {
        "order_no": o.order_no,
        "emp_id": o.emp_id,
        "department": o.department,
        "title": o.title,
        "category": o.category,
        "amount": _dec(o.amount),
        "currency": o.currency,
        "supplier_name": o.supplier_name,
        "quotes_count": o.quotes_count,
        "budget_year": o.budget_year,
        "status": o.status,
        "current_node": o.current_node,
        "precheck_result": o.precheck_result,
        "flags": o.flags or [],
        "created_at": _iso(o.created_at),
    }


def _contract_dict(c: ContractReview, *, with_content: bool = False) -> dict[str, Any]:
    payload = {
        "contract_no": c.contract_no,
        "title": c.title,
        "party_a": c.party_a,
        "party_b": c.party_b,
        "category": c.category,
        "amount": _dec(c.amount),
        "currency": c.currency,
        "sign_date": _iso(c.sign_date),
        "effective_date": _iso(c.effective_date),
        "expiry_date": _iso(c.expiry_date),
        "doc_key": c.doc_key,
        "status": c.status,
        "risk_level": c.risk_level,
        "findings": c.findings or [],
        "review": c.review_json or {},
        "reviewer": c.reviewer,
        "opinion": c.opinion,
        "created_at": _iso(c.created_at),
    }
    if with_content:
        payload["content"] = c.content
    return payload


def _find_supplier(session: Session, name: str) -> dict[str, Any] | None:
    if not name:
        return None
    row = session.scalars(select(Supplier).where(Supplier.name == name)).first()
    if row is None:  # 名称模糊匹配一次, 减少"公司全称/简称"造成的未准入误判
        row = session.scalars(select(Supplier).where(Supplier.name.contains(name))).first()
    if row is None:
        return None
    return {
        "supplier_code": row.supplier_code,
        "name": row.name,
        "category": row.category,
        "bank_account": row.bank_account,
        "qualification": row.qualification,
        "risk_status": row.risk_status,
    }


def _find_budget(session: Session, department: str, year: int) -> dict[str, Any] | None:
    if not department:
        return None
    row = session.scalars(
        select(DepartmentBudget)
        .where(DepartmentBudget.department == department, DepartmentBudget.year == year)
        .order_by(DepartmentBudget.year.desc())
    ).first()
    if row is None:
        return None
    return {
        "department": row.department,
        "year": row.year,
        "annual_budget": _dec(row.annual_budget),
        "used_amount": _dec(row.used_amount),
    }


def _emp_department(session: Session, emp_id: str) -> str:
    if not emp_id:
        return ""
    return session.execute(
        text("SELECT department FROM hr_employees WHERE emp_id = :eid"), {"eid": emp_id}
    ).scalar() or ""


@mcp.tool()
def create_purchase_order(
    user_id: str,
    title: str,
    amount: float,
    category: str,
    supplier_name: str = "",
    quotes_count: int = 1,
    reason: str = "",
    department: str = "",
) -> dict[str, Any]:
    """创建采购申请单(状态进入 PRECHECK, 等待合规初审)。

    Args:
        user_id: 申请人工号。
        title: 采购事项, 如 研发部测试机采购。
        amount: 金额(元), 必须大于 0。
        category: IT设备/办公用品/咨询服务/市场推广/培训服务/其他。
        supplier_name: 拟定供应商名称(需在供应商名录内, 否则初审会判为未准入)。
        quotes_count: 比价份数; 金额>5000 元时制度要求 >=3 份。
        reason: 申请事由。
        department: 申请部门; 留空时按工号从员工主数据带出。

    Returns:
        新建单据(含 order_no 与初审门禁状态) 或 {error}。
    """
    if category not in ALLOWED_CATEGORIES:
        return {"error": f"非法采购类别: {category}; 可选: {sorted(ALLOWED_CATEGORIES)}"}
    if amount <= 0:
        return {"error": "采购金额必须大于 0"}
    if quotes_count < 1:
        return {"error": "比价份数至少为 1(单一来源也需说明理由)"}
    if not user_id:
        return {"error": "缺少申请人工号, 请先提供姓名由系统解析"}

    with Session(dbsync.get_sync_engine()) as session:
        dept = department or _emp_department(session, user_id)
        order = PurchaseRequest(
            order_no=_next_no(session, "proc_orders", "PO", "order_no", 3000),
            emp_id=user_id,
            department=dept,
            title=title,
            category=category,
            amount=round(float(amount), 2),
            supplier_name=supplier_name,
            quotes_count=int(quotes_count),
            budget_year=datetime.now(_CST).year,
            reason=reason,
            status="PRECHECK",
            current_node="合规初审",
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        session.commit()
        result = _order_dict(order)
    result["next_action"] = "调用 precheck_purchase_order 出具初审结论后再提交审批"
    return result


@mcp.tool()
def precheck_purchase_order(order_no: str) -> dict[str, Any]:
    """对一张采购申请单执行合规初审(比价份数/供应商准入/部门预算余额), 结论落回单据。

    初审只出具结论, 不代替审批: 高风险单据被置为 RETURNED(退回补充), 低/中风险
    进入 PENDING 等待人工审批节点。

    Args:
        order_no: 采购单号, 如 PO3000。

    Returns:
        {order_no, risk_level, findings, conclusion, status} 或 {error}。
    """
    with Session(dbsync.get_sync_engine()) as session:
        order = session.get(PurchaseRequest, order_no)
        if order is None:
            return {"error": f"采购单 {order_no} 不存在"}
        supplier = _find_supplier(session, order.supplier_name)
        budget = _find_budget(session, order.department, order.budget_year or datetime.now(_CST).year)
        outcome = rules.precheck_purchase_order(
            amount=order.amount,
            department=order.department,
            supplier_name=order.supplier_name,
            quotes_count=order.quotes_count,
            category=order.category,
            budget=budget,
            supplier=supplier,
        )
        order.status = "RETURNED" if outcome.risk_level == "高" else "PENDING"
        order.current_node = "退回补充" if order.status == "RETURNED" else "采购审批"
        order.precheck_result = outcome.conclusion
        order.flags = [f.to_dict() for f in outcome.findings]
        session.commit()
        return {
            "order_no": order.order_no,
            "department": order.department,
            "amount": _dec(order.amount),
            "supplier": supplier or {"name": order.supplier_name, "in_register": False},
            "budget": budget or {"available": False},
            **outcome.to_dict(),
            "status": order.status,
            "current_node": order.current_node,
        }


@mcp.tool()
def query_purchase_order(order_no: str) -> dict[str, Any]:
    """按单号查询采购申请单与其初审结论。

    Args:
        order_no: 采购单号, 如 PO3000。

    Returns:
        单据详情或 {error}。
    """
    with Session(dbsync.get_sync_engine()) as session:
        order = session.get(PurchaseRequest, order_no)
        if order is None:
            return {"error": f"采购单 {order_no} 不存在"}
        return _order_dict(order)


@mcp.tool()
def list_purchase_orders(user_id: str = "", department: str = "", status: str = "") -> list[dict[str, Any]]:
    """列出采购申请单(可按申请人工号/部门/状态过滤; 都不传则返回最近 20 单)。

    Args:
        user_id: 申请人工号。
        department: 部门名称。
        status: DRAFT/PRECHECK/PENDING/APPROVED/RETURNED/REJECTED/PAID。

    Returns:
        单据列表(可能为空)。
    """
    with Session(dbsync.get_sync_engine()) as session:
        stmt = select(PurchaseRequest).order_by(PurchaseRequest.created_at.desc()).limit(20)
        if user_id:
            stmt = stmt.where(PurchaseRequest.emp_id == user_id)
        if department:
            stmt = stmt.where(PurchaseRequest.department == department)
        if status:
            stmt = stmt.where(PurchaseRequest.status == status.upper())
        return [_order_dict(o) for o in session.scalars(stmt).all()]


@mcp.tool()
def check_purchase_compliance(
    amount: float,
    department: str = "",
    supplier_name: str = "",
    quotes_count: int = 1,
) -> dict[str, Any]:
    """不落单据的合规预演: 用户问"这样买行不行/需要几家比价"时先用它判定。

    Args:
        amount: 预计金额(元)。
        department: 部门(提供则可校验预算余额)。
        supplier_name: 拟定供应商(提供则可校验准入与风险状态)。
        quotes_count: 当前已收集的比价份数。

    Returns:
        {risk_level, findings, conclusion} —— 规则口径与真实初审完全一致。
    """
    with Session(dbsync.get_sync_engine()) as session:
        supplier = _find_supplier(session, supplier_name)
        budget = _find_budget(session, department, datetime.now(_CST).year) if department else None
    return rules.precheck_purchase_order(
        amount=amount,
        department=department,
        supplier_name=supplier_name,
        quotes_count=quotes_count,
        budget=budget,
        supplier=supplier,
    ).to_dict()


@mcp.tool()
def list_suppliers(category: str = "", keyword: str = "") -> list[dict[str, Any]]:
    """查询在册供应商(可按类别或名称关键词过滤)。

    Args:
        category: 供应商类别, 如 IT设备。
        keyword: 名称关键词(子串匹配)。

    Returns:
        供应商列表; 为空说明该名称未准入。
    """
    with Session(dbsync.get_sync_engine()) as session:
        stmt = select(Supplier).order_by(Supplier.supplier_code).limit(50)
        if category:
            stmt = stmt.where(Supplier.category == category)
        if keyword:
            stmt = stmt.where(Supplier.name.contains(keyword))
        return [
            {
                "supplier_code": s.supplier_code,
                "name": s.name,
                "category": s.category,
                "qualification": s.qualification,
                "risk_status": s.risk_status,
            }
            for s in session.scalars(stmt).all()
        ]


@mcp.tool()
def query_supplier(name: str) -> dict[str, Any]:
    """按名称查单个供应商的资质与风险状态(合同初审前的主体核验)。

    Args:
        name: 供应商名称(支持子串匹配)。

    Returns:
        供应商记录(含 bank_account 供付款条款核对) 或 {error}。
    """
    with Session(dbsync.get_sync_engine()) as session:
        found = _find_supplier(session, name)
    return found or {"error": f"未在册供应商: {name}(需先完成准入)"}


@mcp.tool()
def check_contract_clauses(content: str, amount: float = 0, party_b: str = "") -> dict[str, Any]:
    """合同条款规则初审(纯判定, 不落台账): 必备条款缺失 + 高风险表述 + 金额分级。

    判定由进程内规则引擎完成(见 app/procurement/rules.py), 不消耗 token 也不受
    模型状态影响 —— 合同初审的底线必须是可判定的事实, 不能是模型的一次猜测。

    Args:
        content: 合同全文(或主要条款摘录; 越完整判定越准)。
        amount: 合同金额(元), 用于超权限判定。
        party_b: 乙方名称(提供则交叉核验供应商在册状态与收款账号一致性)。

    Returns:
        {risk_level, findings, conclusion, checked_rules} 或 {error}。
    """
    if not (content or "").strip():
        return {"error": "合同正文为空: 请粘贴全文或先在知识库入库后按 doc_key 送审"}
    with Session(dbsync.get_sync_engine()) as session:
        supplier = _find_supplier(session, party_b) if party_b else None
    outcome = rules.precheck_contract(
        content=content, party_b=party_b, amount=amount, supplier=supplier
    )
    return {
        **outcome.to_dict(),
        "checked_rules": {
            "required_clauses": len(rules.REQUIRED_CLAUSES),
            "risky_terms": len(rules.RISKY_TERMS),
            "quote_required_amount": float(rules.QUOTE_REQUIRED_AMOUNT),
            "single_sign_limit": float(rules.SINGLE_SIGN_LIMIT),
        },
    }


@mcp.tool()
def submit_contract_review(
    title: str,
    content: str,
    party_b: str,
    amount: float = 0,
    party_a: str = "",
    category: str = "采购",
    sign_date: str = "",
    effective_date: str = "",
    expiry_date: str = "",
    doc_key: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """把一份合同登记进台账并出具初审结论(规则判定 + 风险清单 + 初审意见)。

    LLM 的语义补充由 Contract_Agent 负责(它会在本工具结果之上再调
    analyze_contract_terms), 台账里的 review_json 存那份补充结论。

    Args:
        title: 合同名称。
        content: 合同正文(送审全文)。
        party_b: 乙方/对手方名称。
        amount: 合同金额(元)。
        party_a: 甲方全称, 留空用默认签约主体。
        category: 采购/服务/框架协议/劳动/保密。
        sign_date/effective_date/expiry_date: YYYY-MM-DD, 可留空。
        doc_key: 已入库文档的 doc_key(有则一并记录, 便于溯源到知识库)。
        user_id: 送审人工号。

    Returns:
        台账记录 + 初审结论; 失败返回 {error}。
    """
    if not (content or "").strip():
        return {"error": "合同正文为空, 无法出具初审结论"}

    def _d(value: str) -> date | None:
        text_value = (value or "").strip()
        if not text_value:
            return None
        try:
            return date.fromisoformat(text_value[:10])
        except ValueError:
            return None

    with Session(dbsync.get_sync_engine()) as session:
        supplier = _find_supplier(session, party_b)
        outcome = rules.precheck_contract(
            content=content,
            title=title,
            party_a=party_a or "马小 i 科技有限公司",
            party_b=party_b,
            amount=amount,
            sign_date=_d(sign_date),
            effective_date=_d(effective_date),
            expiry_date=_d(expiry_date),
            supplier=supplier,
        )
        contract = ContractReview(
            contract_no=_next_no(session, "proc_contracts", "CT", "contract_no", 8000),
            title=title,
            party_a=party_a or "马小 i 科技有限公司",
            party_b=party_b,
            category=category,
            amount=round(float(amount or 0), 2),
            sign_date=_d(sign_date),
            effective_date=_d(effective_date),
            expiry_date=_d(expiry_date),
            doc_key=doc_key,
            content=content,
            status="RISK" if outcome.risk_level == "高" else "PRECHECKED",
            risk_level=outcome.risk_level,
            findings=[f.to_dict() for f in outcome.findings],
            reviewer=user_id,
            opinion=outcome.conclusion,
            created_at=datetime.now(timezone.utc),
        )
        session.add(contract)
        session.commit()
        return {
            **_contract_dict(contract, with_content=False),
            "conclusion": outcome.conclusion,
            "supplier_in_register": supplier is not None,
        }


@mcp.tool()
def query_contract(contract_no: str) -> dict[str, Any]:
    """按合同号查询台账记录与初审结论(不含全文, 全文用 get_contract_text)。

    Args:
        contract_no: 合同号, 如 CT8000。

    Returns:
        台账记录或 {error}。
    """
    with Session(dbsync.get_sync_engine()) as session:
        row = session.get(ContractReview, contract_no)
        if row is None:
            return {"error": f"合同 {contract_no} 不存在"}
        return _contract_dict(row)


@mcp.tool()
def list_contracts(user_id: str = "", status: str = "", risk_level: str = "") -> list[dict[str, Any]]:
    """列出合同台账(可按送审人/状态/风险等级过滤)。

    Args:
        user_id: 送审人工号(对应 reviewer 字段)。
        status: DRAFT/PRECHECKED/APPROVED/RISK/REJECTED。
        risk_level: 低/中/高。

    Returns:
        台账列表(按创建时间倒序, 最多 20 条)。
    """
    with Session(dbsync.get_sync_engine()) as session:
        stmt = select(ContractReview).order_by(ContractReview.created_at.desc()).limit(20)
        if user_id:
            stmt = stmt.where(ContractReview.reviewer == user_id)
        if status:
            stmt = stmt.where(ContractReview.status == status.upper())
        if risk_level:
            stmt = stmt.where(ContractReview.risk_level == risk_level)
        return [_contract_dict(c) for c in session.scalars(stmt).all()]


@mcp.tool()
def get_contract_text(contract_no: str) -> dict[str, Any]:
    """取合同送审全文(条款分析需要原文时调用; 只返回文本不做判定)。

    Args:
        contract_no: 合同号。

    Returns:
        {contract_no, title, content} 或 {error}。
    """
    with Session(dbsync.get_sync_engine()) as session:
        row = session.get(ContractReview, contract_no)
        if row is None:
            return {"error": f"合同 {contract_no} 不存在"}
        return {"contract_no": row.contract_no, "title": row.title, "content": row.content}


@mcp.tool()
def save_contract_opinion(
    contract_no: str,
    risk_level: str,
    clauses: list[dict[str, Any]] | None = None,
    missing: list[str] | None = None,
    semantics: list[dict[str, Any]] | None = None,
    opinion: str = "",
) -> dict[str, Any]:
    """把模型的条款抽取与语义风险补写进台账(在规则结论之上叠加, 不覆盖规则 findings)。

    Args:
        contract_no: 合同号。
        risk_level: 模型给出的综合风险(低/中/高); 若比规则结论更低会被拒绝——
            模型可以升级风险, 不能降级规则已判定的红线。
        clauses: 关键条款摘要, 每项 {type, summary}。
        missing: 模型额外发现的缺失项(规则未覆盖的)。
        semantics: 语义风险, 每项 {clause, risk, level, suggestion}。
        opinion: 合并后的初审意见(展示用)。

    Returns:
        更新后台账 或 {error}。
    """
    if risk_level not in rules.RISK_ORDER:
        return {"error": f"非法 risk_level: {risk_level}; 可选 低/中/高"}
    with Session(dbsync.get_sync_engine()) as session:
        row = session.get(ContractReview, contract_no)
        if row is None:
            return {"error": f"合同 {contract_no} 不存在"}
        base = rules.RISK_ORDER.get(row.risk_level or "低", 0)
        proposed = rules.RISK_ORDER[risk_level]
        merged = max(base, proposed)  # 只升不降
        row.review_json = {
            "clauses": clauses or [],
            "missing": missing or [],
            "semantic_risks": semantics or [],
        }
        row.risk_level = next(k for k, v in rules.RISK_ORDER.items() if v == merged)
        if opinion:
            row.opinion = opinion
        row.status = "RISK" if merged >= 2 else (row.status or "PRECHECKED")
        session.commit()
        return _contract_dict(row)


@mcp.tool()
def execute_sql(sql: str) -> list[dict[str, Any]]:
    """Text2SQL: 在采购/合同业务库上执行一条只读 SELECT 并返回结果。

    仅可查询: proc_orders(采购单), proc_contracts(合同台账), proc_suppliers(供应商),
    fin_department_budgets(部门预算), hr_employees(员工主数据)。仅允许单条 SELECT;
    结果最多 50 行。统计/明细类问题用本工具, 业务动作走专用工具。

    Args:
        sql: 一条针对上述白名单表的只读 PostgreSQL SELECT 语句。

    Returns:
        [{columns, rows, rowcount}] 或 [{error, sql}]。
    """
    try:
        return dbsync.execute_readonly_sql(sql, ALLOWED_TABLES)
    except SQLGuardError as exc:
        return [{"error": f"SQL 校验失败: {exc}", "sql": sql}]
    except Exception as exc:  # noqa: BLE001 - 交回错误由 Agent 修正 SQL
        return [{"error": f"SQL 执行失败: {exc.__class__.__name__}", "sql": sql}]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
