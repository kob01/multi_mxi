"""SQLAlchemy ORM models (PostgreSQL): 文档元数据 + HR/Finance/Procurement 业务表 + pgvector 知识块 + 个人记忆/用户画像 + 会话记录 + 报表产物."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings


def _utcnow() -> datetime:
    """Timezone-aware timestamp (PG 侧列为 timestamptz, naive 值会被错读为本地时区)."""
    return datetime.now(timezone.utc)


def _default_tenant_id() -> str:
    """新行的租户归属默认取配置的单租户默认值(层 1)。

    只给"服务端插行"这一条路径用; 旧行的回填走 app/db/scope.py 的显式 UPDATE。
    回填不出的行留在 ``''``: 空租户在 RLS 策略下任何会话都匹配不上, 即"未归属 = 谁都
    看不到"的默认拒, 而不是"未归属 = 全员可见"。
    """
    return get_settings().default_tenant_id


class Base(DeclarativeBase):
    """Declarative base for all metadata tables."""


class Document(Base):
    """One uploaded document (identity = normalized file name + extension)."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    ext: Mapped[str] = mapped_column(String(16))
    modality: Mapped[str] = mapped_column(String(32), default="text")
    file_path: Mapped[str] = mapped_column(String(512), default="")
    parsed_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # MySQL MEDIUMTEXT -> PG TEXT(无长度上限)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    # --- 发布态门禁 / 正文外置标记 (老库升级走 _BACKFILL_COLUMNS 补列) ---
    # status: ready / ingesting / failed。ingesting 期间该文档的块一律不可被检索命中
    # (防"正在入库的半篇文档"泄漏), 见 docs/service.py 的发布态与 security/acl 门禁。
    status: Mapped[str] = mapped_column(String(16), default="ready")
    # body_stored: 正文已落 MongoDB 的标记, 迁移脚本与运维排查用。
    body_stored: Mapped[bool] = mapped_column(Boolean, default=False)
    # --- 文档级 ACL (权限存储在元数据中; 检索时经 pgvector Metadata Filter 前置裁剪) ---
    visibility: Mapped[str] = mapped_column(String(16), default="public")  # public/dept/role/private
    owner_id: Mapped[str] = mapped_column(String(64), default="")         # private: 所有者工号
    dept_id: Mapped[str] = mapped_column(String(64), default="")           # dept: 授权部门
    allowed_roles: Mapped[str] = mapped_column(String(128), default="")    # role: 逗号包裹 ",hr,admin,"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class Tag(Base):
    """A document category tag (LLM-suggested or user-defined)."""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(16), default="llm")  # llm / custom
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class DocumentTag(Base):
    """Many-to-many link between documents and tags."""

    __tablename__ = "document_tags"
    __table_args__ = (UniqueConstraint("doc_key", "tag_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_key: Mapped[str] = mapped_column(String(32), index=True)
    tag_id: Mapped[int] = mapped_column(BigInteger, index=True)


# ---------------------------------------------------------------------------
# HR 业务表 (hr_ 前缀)
# ---------------------------------------------------------------------------
class Employee(Base):
    """员工主数据 (含年假额度, 供工单/年假查询与 Text2SQL 使用)."""

    __tablename__ = "hr_employees"

    emp_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    department: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[str] = mapped_column(String(64), default="")
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    annual_leave_total: Mapped[int] = mapped_column(Integer, default=10)
    annual_leave_used: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="在职")  # 在职/离职
    # ---- 层 1 作用域列(RLS 谓词键): tenant_id + dept_id 由会话作用域比对 ----
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class HRTicket(Base):
    """HR 服务工单."""

    __tablename__ = "hr_tickets"

    ticket_no: Mapped[str] = mapped_column(String(32), primary_key=True)  # HR1000+
    emp_id: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32))  # 入职/离职/考勤/薪酬/证明开具/其他
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN/PROCESSING/DONE/CANCELLED
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    # 软删除三件套(层 4-3): 写通道里的 DELETE 一律被改写成 SET is_deleted=true。
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class LeaveRecord(Base):
    """请假记录."""

    __tablename__ = "hr_leave_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    emp_id: Mapped[str] = mapped_column(String(32), index=True)
    leave_type: Mapped[str] = mapped_column(String(16))  # 年假/事假/病假/调休/婚假
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[Decimal] = mapped_column(Numeric(5, 1))
    status: Mapped[str] = mapped_column(String(16), default="审批中")  # 审批中/已批准/已驳回
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Finance 业务表 (fin_ 前缀)
# ---------------------------------------------------------------------------
class Reimbursement(Base):
    """报销单."""

    __tablename__ = "fin_reimbursements"

    order_no: Mapped[str] = mapped_column(String(32), primary_key=True)  # FIN5000+
    emp_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    category: Mapped[str] = mapped_column(String(32), index=True)  # 差旅费/交通费/餐饮费/办公用品/培训费
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="SUBMITTED")  # SUBMITTED/APPROVED/REJECTED/PAID
    current_node: Mapped[str] = mapped_column(String(64), default="部门主管审批")
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, server_default=func.now()
    )


class DepartmentBudget(Base):
    """部门年度预算."""

    __tablename__ = "fin_department_budgets"
    __table_args__ = (UniqueConstraint("department", "year"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    department: Mapped[str] = mapped_column(String(64), index=True)
    year: Mapped[int] = mapped_column(Integer)
    annual_budget: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    used_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    # 预算表是高危表(层 4-6 黑名单): 有作用域列供读隔离, 但写通道永远不命中它。
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)


# ---------------------------------------------------------------------------
# Procurement 业务表 (proc_ 前缀): 供应商 / 采购申请单 / 合同初审
# ---------------------------------------------------------------------------
class Supplier(Base):
    """供应商主数据 (供采购单关联与合同初审的资质/黑名单校验)."""

    __tablename__ = "proc_suppliers"

    supplier_code: Mapped[str] = mapped_column(String(32), primary_key=True)  # SUP001+
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64), default="")      # IT设备/办公用品/咨询服务/市场推广...
    bank_account: Mapped[str] = mapped_column(String(64), default="")   # 收款账号 (合同付款条款一致性核对)
    qualification: Mapped[str] = mapped_column(String(32), default="")  # 一般纳税人/小规模/个体
    risk_status: Mapped[str] = mapped_column(String(16), default="正常")  # 正常/关注/黑名单
    # 供应商是跨部门共享的主数据: dept_id 保空串(全员作用域), 只按 tenant_id 隔离。
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class PurchaseRequest(Base):
    """采购申请单: 由 Contract_Agent 创建, 初审结论落 precheck_result + flags.

    与报销单的差别在于"事前": 报销是费用已发生后核销, 采购是付款前的申请与
    合规初审(比价/供应商资质/预算余额), 因此初审结论要留在单据上供人工复核。
    """

    __tablename__ = "proc_orders"

    order_no: Mapped[str] = mapped_column(String(32), primary_key=True)  # PO3000+
    emp_id: Mapped[str] = mapped_column(String(32), index=True)          # 申请人
    department: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64), default="")        # 同报销类别口径, 便于跨域统计
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    supplier_name: Mapped[str] = mapped_column(String(128), default="")
    quotes_count: Mapped[int] = mapped_column(Integer, default=1)        # 比价份数(合规门槛 >=3)
    budget_year: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    # DRAFT/PRECHECK/PENDING/APPROVED/REJECTED/PAID
    status: Mapped[str] = mapped_column(String(16), default="PRECHECK", index=True)
    current_node: Mapped[str] = mapped_column(String(64), default="合规初审")
    # 初审结论摘要(人类可读); 结构化风险项落 flags JSON, 仅承载展示与复核信息
    precheck_result: Mapped[str] = mapped_column(Text, default="")
    flags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class ContractReview(Base):
    """合同台账 + 初审结论: 规则清单(确定性) + LLM 条款抽取共同构成结论.

    初审是"初审"不是"审批": 本表只产出风险清单与建议, 最终放行仍由法务/财务
    人工决定, 因此结论必须整份留痕(review_json)以便复盘与追责。
    """

    __tablename__ = "proc_contracts"

    contract_no: Mapped[str] = mapped_column(String(32), primary_key=True)  # CT8000+
    title: Mapped[str] = mapped_column(String(255), default="")
    party_a: Mapped[str] = mapped_column(String(128), default="")   # 本企业主体
    party_b: Mapped[str] = mapped_column(String(128), index=True)   # 对手方(供应商名)
    category: Mapped[str] = mapped_column(String(64), default="")   # 采购/服务/框架协议/劳动/保密
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    sign_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    doc_key: Mapped[str] = mapped_column(String(32), default="")    # 关联已入库文档(可空)
    content: Mapped[str] = mapped_column(Text, default="")          # 送审全文(初审的输入)
    # DRAFT/PRECHECKED/APPROVED/RISK/REJECTED
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="")  # 低/中/高
    findings: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 命中的规则项
    review_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # LLM 抽取的条款/缺失项
    reviewer: Mapped[str] = mapped_column(String(64), default="")
    opinion: Mapped[str] = mapped_column(Text, default="")          # 初审意见(可直接回给用户)
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    dept_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 层 1: 部门注册表(业务表 dept_id 的单一事实来源)
# ---------------------------------------------------------------------------
class Department(Base):
    """部门注册表: ``dept_id`` <-> 部门名的唯一对应关系。

    为什么新开一表而不是直接拿 ``hr_employees.department`` 当键: 字符串当隔离键会让
    "市场部"与"市场一部"这类改名/重名变成隔在隔离层里看不见的洞; 业务表里留着
    department 列供展示与旧 SQL 兼容, 而隔离判定只看 dept_id。
    """

    __tablename__ = "sys_departments"

    dept_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # D001+
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=_default_tenant_id, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# RAG 知识块
# ---------------------------------------------------------------------------
class KnowledgeChunkRow(Base):
    """[遗留旧表] 父子混装的单表 ``knowledge_chunks``(迁移前的形态)。

    现行检索/入库路径已全部改走父子双表(``doc_chunks`` + ``doc_parents``, 见
    ``app/rag/vectorstore.py``), 本 ORM 仅由 ``scripts/migrate_doc_stores.py``
    读写(存量搬迁与校验); 旧表按下线流程是改名而非 DROP, 因此仍留在 metadata 里。

    ``chunk_id`` 主键, ``is_parent`` 区分父块/子块
    (检索只命中子块), 四个 ACL 标量冗余在每行上供检索前置裁剪。父块也写向量
    (与旧行为一致), 列可空以便后续只向量化子块。
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        # 稠密 TopK 检索: cosine HNSW (pgvector)。create_all 以非 CONCURRENT 方式
        # 建索引(需独占事务), 语料上量后应先建表、再手工 CREATE INDEX CONCURRENTLY。
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        # 单文档重入库 / ACL 刷新的主路径
        Index("ix_knowledge_chunks_doc_parent", "doc_id", "is_parent"),
    )

    chunk_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(512), default="")
    modality: Mapped[str] = mapped_column(String(32), default="text")
    parent_id: Mapped[str] = mapped_column(String(80), default="")
    is_parent: Mapped[bool] = mapped_column(Boolean, default=False)  # 父块不参与检索
    page_no: Mapped[int] = mapped_column(Integer, default=-1)  # -1 = unknown
    section: Mapped[str] = mapped_column(String(256), default="")
    visibility: Mapped[str] = mapped_column(String(16), default="public")
    owner_id: Mapped[str] = mapped_column(String(64), default="")
    dept_id: Mapped[str] = mapped_column(String(64), default="")
    allowed_roles: Mapped[str] = mapped_column(String(128), default="")  # ",hr,admin,"
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )


# ---------------------------------------------------------------------------
# 文档正文外置: PG 父子双表 (去版本化, chunk_text 留 PG, 父块全文在 Mongo)
# ---------------------------------------------------------------------------
class DocParentRow(Base):
    """父块: 结构定位与上下文单位。**不存正文**, 正文在 Mongo parent_texts。

    ``parent_id`` 全局唯一(``{doc_id}-p{seq:04d}``), 既是本表主键、
    ``doc_chunks.parent_id`` 的外键(父子引用只在 PG 内成立), 又直接当 Mongo
    ``parent_texts`` 的 ``_id``。offset 基准是 normalized_text, 由 structure 单向派生,
    不回写 Mongo(不变量 4)。``start_offset/end_offset`` 为 -1 表示未知(迁移回算失败)。
    """

    __tablename__ = "doc_parents"
    __table_args__ = (
        Index("ix_doc_parents_doc_ord", "doc_id", "ord"),
    )

    parent_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    ord: Mapped[int] = mapped_column(Integer, default=0)  # 全篇序号(相邻块扩展用)
    parent_type: Mapped[str] = mapped_column(String(16), default="section")  # section/clause/table/faq
    title: Mapped[str] = mapped_column(String(512), default="")
    section: Mapped[str] = mapped_column(String(256), default="")  # 派生自 structure, 不回写
    page_no: Mapped[int] = mapped_column(Integer, default=-1)
    start_offset: Mapped[int] = mapped_column(Integer, default=-1)  # normalized_text 字符区间
    end_offset: Mapped[int] = mapped_column(Integer, default=-1)
    content_hash: Mapped[str] = mapped_column(String(32), default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    child_count: Mapped[int] = mapped_column(Integer, default=0)
    normalizer_version: Mapped[str] = mapped_column(String(8), default="n1")
    # --- 4 个 ACL 标量列(安全边界, 与 doc_chunks 逐条对应; 禁止入 extra) ---
    visibility: Mapped[str] = mapped_column(String(16), default="public")
    owner_id: Mapped[str] = mapped_column(String(64), default="")
    dept_id: Mapped[str] = mapped_column(String(64), default="")
    allowed_roles: Mapped[str] = mapped_column(String(128), default="")
    # 仅承载展示字段, 权限字段禁止入 JSONB。
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class DocChunkRow(Base):
    """子块: 向量与它的 embedding 输入同表(一致性优先), 但 chunk_text 不进扫描列。

    与旧 ``knowledge_chunks`` 的关键区别: 没有父块行(父块占旧表约一半且均带无用
    向量), HNSW 图只装可检索子块; ``is_parent`` 列消失(表本身就分开了)。
    ``chunk_text`` 存于此但**不列入** vectorstore.NARROW_COLUMNS —— TopK 只取窄列,
    命中后按 chunk_id 主键点查批量取文本。
    """

    __tablename__ = "doc_chunks"
    __table_args__ = (
        Index(
            "ix_doc_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        Index("ix_doc_chunks_doc_ord", "doc_id", "ord"),
        Index("ix_doc_chunks_parent_ord", "parent_id", "chunk_index"),
    )

    chunk_id: Mapped[str] = mapped_column(String(80), primary_key=True)  # {doc_id}-p{seq:04d}-c{idx:02d}
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_id: Mapped[str] = mapped_column(String(80), index=True)  # PG 内引用 DocParentRow
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)  # parent 内序号
    ord: Mapped[int] = mapped_column(Integer, default=0)  # 全篇序号
    chunk_text: Mapped[str] = mapped_column(Text, default="")  # 不列入 NARROW_COLUMNS
    content_hash: Mapped[str] = mapped_column(String(32), default="")  # 决定要不要重 embed
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str] = mapped_column(String(64), default="")  # 向量仅在同模型内可比
    title: Mapped[str] = mapped_column(String(512), default="")
    source: Mapped[str] = mapped_column(String(512), default="")
    modality: Mapped[str] = mapped_column(String(32), default="text")
    section: Mapped[str] = mapped_column(String(256), default="")
    page_no: Mapped[int] = mapped_column(Integer, default=-1)
    # --- ACL 前置裁剪(安全边界) ---
    visibility: Mapped[str] = mapped_column(String(16), default="public")
    owner_id: Mapped[str] = mapped_column(String(64), default="")
    dept_id: Mapped[str] = mapped_column(String(64), default="")
    allowed_roles: Mapped[str] = mapped_column(String(128), default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # 仅展示
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 个人记忆 (Vector 通道: User Memory / Episodic / Personal Knowledge)
# ---------------------------------------------------------------------------
class LongTermMemoryRow(Base):
    """一条跨会话个人记忆(偏好/习惯/情节/知识) + 稠密向量, 按 ``user_id`` 隔离。

    与 ``knowledge_chunks`` 是两张不同的表: 知识库是全企业共享的文档, 长期记忆
    是"这个用户"自己的对话沉淀, 权限语义完全不同(必须按 user_id 严格隔离,
    不能像文档那样走 public/dept/role 可见性模型), 因此不复用同一张表。

    个人级记忆的分桶(桶语义见 ``app/memory/taxonomy.py``)复用 ``kind`` 列而非
    新开表: 各桶都是"一段文本 + 向量"的同一形状, 共用同一套 HNSW 索引与语义
    查重路径, 多开表只会让召回变成 N 次 UNION。``fact`` 是引入分桶前的历史
    取值, 仍可被召回, 但新写入不再产生。
    """

    __tablename__ = "long_term_memories"
    __table_args__ = (
        # 与 knowledge_chunks 同样的 cosine HNSW 检索方式。
        Index(
            "ix_long_term_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        # 语义查重 / 召回都是"限定 user_id + 按向量排序", 复合索引前置 user_id。
        Index("ix_long_term_memories_user_created", "user_id", "created_at"),
        # 偏好/习惯是"每轮都直读"的稳定桶: 限定 user_id + kind 按最近使用取 Top-N。
        Index(
            "ix_long_term_memories_user_kind_access",
            "user_id",
            "kind",
            "last_accessed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    # fact(历史遗留)/preference/habit/episode/knowledge
    kind: Mapped[str] = mapped_column(String(16), default="fact")
    title: Mapped[str] = mapped_column(String(128), default="")  # 情节/知识的短标题
    content: Mapped[str] = mapped_column(Text, default="")
    # 产出来源: turn(对话轮提取) / session_summary(会话摘要折叠) / reflection(情节蒸馏)
    source: Mapped[str] = mapped_column(String(32), default="turn")
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # 事件发生时间(仅情节; "上周报的销"这类时间锚点)
    source_session_id: Mapped[str] = mapped_column(String(64), default="")
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 用户画像 (User Memory 的 profile 桶)
# ---------------------------------------------------------------------------
class UserProfileRow(Base):
    """一个用户一条聚合画像: 结构化当前值 + 观测序列 + 渲染好的 prompt 摘要。

    不入 ``long_term_memories`` 的原因有二: 一是画像"一人一条"、同一个属性只留
    一个当前值, 与逐条事实的语义不同; 二是画像每轮都要全量注入, 不需要也不应该做
    向量相似度检索(没有 embedding 列)。摘要由属性模板渲染, 不额外调 LLM。

    当前值按**生效时间派生**而不是按写入顺序覆盖, 因此波动类属性(体重/身高/部门/
    职位等)的完整观测序列另存一份在 ``attribute_history``: 旧值不会被一句历史陈述
    抹掉, 但也不会挤进 prompt(只给"我的记忆"页看)。每键条数有界, 画像表仍是一人一行。
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 当前值: {"姓名": ["..."], "体重": ["70kg"], "技能": ["...", "..."]}(多值槽可以并列)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    # 观测序列(只给波动类属性): {"体重": [{value, valid_at, recorded_at, explicit}, ...]},
    # 按"当前在前"排序; 可空 —— 列上线之前的老行没有历史, 首次合并时从 attributes 自愈。
    attribute_history: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    summary: Mapped[str] = mapped_column(Text, default="")
    # 情节蒸馏门槛的判定基准: 上次蒸馏时间点之后新增的情节才计入触发条件
    last_reflected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 页面会话记录 (聊天历史持久化: 刷新/重进页面后可回看)
# ---------------------------------------------------------------------------
class ChatSession(Base):
    """一个前端会话窗口 (identity = 客户端生成的 session_id)。

    与 Session Memory(Redis, 有 TTL) 是两回事: 这里只负责"页面历史记录"
    的永久回看, 不参与 prompt 上下文拼装。
    """

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # client session_id
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16), default="employee")
    department: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(120), default="新会话")  # 首条用户消息前缀
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class ChatMessage(Base):
    """一条会话消息 (user / assistant); 助手消息附带思考内容与路由元信息。"""

    __tablename__ = "chat_messages"
    __table_args__ = (
        # 历史回看主路径: 限定会话按时间序取全部消息
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16))  # user/assistant
    content: Mapped[str] = mapped_column(Text, default="")
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)  # 思考过程(仅助手)
    route: Mapped[str] = mapped_column(String(32), default="")  # 仅助手: 路由标签
    target: Mapped[str] = mapped_column(String(64), default="")
    intent: Mapped[str] = mapped_column(String(32), default="")
    docs_meta: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 参考来源
    # docgen 路由的创作产物 [{name, url, title}]; 与 docs_meta 分开是因为两者前端用途不同
    # (一个是引用条, 一个是"查看文档/去加工"按钮), 合在一起会让渲染逻辑靠 type 猜测。
    artifacts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 报表产物台账 (Analyst_Agent 生成的图表/周报/月报)
# ---------------------------------------------------------------------------
class ReportArtifact(Base):
    """一份分析产物的台账: 文件落盘 + 数据库行两侧并存.

    为什么不只存文件: 图表/报告是需要被"再找到"的资产(上周那份报告在哪),
    只靠目录命名无法按人/按主题回查; 为什么不只存库: SVG/Markdown 需要直接
    用 URL 打开。行只记定位与归属(谁在什么参数下生成的), 正文以磁盘为准。
    """

    __tablename__ = "report_artifacts"
    __table_args__ = (
        Index("ix_report_artifacts_user_created", "created_by", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(128), primary_key=True)  # 文件名(svg/md)
    kind: Mapped[str] = mapped_column(String(16), default="chart")     # chart/report
    title: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")    # 生成者工号
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)     # 生成参数(SQL/维度/周期)
    bytes_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 层 2/4/6: 写通道(模型只交结构化 DSL, 服务端模板化生成 SQL)的落库面
# ---------------------------------------------------------------------------
class PendingDataOp(Base):
    """一个待确认/待审批的写计划。

    为什么必须落库而不是在对话里一步执行: Analyst_Agent 的 A2A 卡片是 ``streaming=False``,
    它的 ReAct 循环无法中途把"是否确认?"抛给用户并等回答。所以写操作分两段:
    ``plan_data_op`` 只生成计划并回一个 op_id, 真正的执行发生在下一轮干净对话
    (发起人确认) 或审批台(人工批准) —— 这同时把"读过业务数据的那轮推理"与
    "执行写"隔成两个上下文(层 5-C 的信息流控制)。
    """

    __tablename__ = "dataops_pending"
    __table_args__ = (
        # 审批台列表的主路径: 按状态取待办 + 按发起人回查自己的计划。
        Index("ix_dataops_pending_status_created", "status", "created_at"),
        Index("ix_dataops_pending_actor_created", "actor_user_id", "created_at"),
    )

    op_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid hex
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    tenant_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    dept_scope: Mapped[str] = mapped_column(String(255), default="")  # 编译时注入的部门集(逗号分隔)
    actor_user_id: Mapped[str] = mapped_column(String(64), default="")
    actor_role: Mapped[str] = mapped_column(String(16), default="")
    action: Mapped[str] = mapped_column(String(16), default="")        # update/delete/insert
    entity: Mapped[str] = mapped_column(String(64), default="")
    dsl_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 模型原样输出
    final_sql: Mapped[str] = mapped_column(Text, default="")            # 服务端模板生成的 SQL
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 绑定的值(永不进 SQL 文本)
    preview_text: Mapped[str] = mapped_column(Text, default="")         # 给人看的回显
    nl_question: Mapped[str] = mapped_column(Text, default="")          # 本轮用户原句
    reason: Mapped[str] = mapped_column(Text, default="")               # 模型填的变更理由
    est_rows: Mapped[int] = mapped_column(Integer, default=0)           # dry-run 影响行数
    cost_estimate: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # PENDING_CONFIRM / PENDING_APPROVAL / NEED_REVIEW / DENIED / EXECUTED / EXPIRED / FAILED
    status: Mapped[str] = mapped_column(String(24), default="PENDING_CONFIRM", index=True)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    approver_id: Mapped[str] = mapped_column(String(64), default="")
    approve_note: Mapped[str] = mapped_column(Text, default="")
    before_image_ref: Mapped[str] = mapped_column(String(64), default="")
    rows_affected: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SqlAuditRecord(Base):
    """一次 SQL/写计划执行的不可篡改审计行(层 6)。

    与 ``logs/audit.jsonl`` 双写: JSONL 是给 SIEM 采集的流, 本表是给"回滚/追责/异常
    检测"做结构化查询的。注意 PG 层只是"逻辑不可变"(角色无 UPDATE/DELETE 权 +
    RULE 拦写), 真 WORM 需要外部存储。
    """

    __tablename__ = "sql_audit_records"
    __table_args__ = (
        # 异常检测的窗口查询都按"谁 + 何时"走, 不带这个索引会全表扫审计本身。
        Index("ix_sql_audit_user_ts", "user_id", "ts"),
        Index("ix_sql_audit_decision_ts", "policy_decision", "ts"),
    )

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid hex
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    tenant_id: Mapped[str] = mapped_column(String(32), default="")
    dept_id: Mapped[str] = mapped_column(String(255), default="")
    user_id: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="")
    nl_question: Mapped[str] = mapped_column(Text, default="")
    generated_sql: Mapped[str] = mapped_column(Text, default="")  # 模型产出原文(SQL 或 DSL)
    final_sql: Mapped[str] = mapped_column(Text, default="")      # 注入域谓词后的 SQL
    policy_decision: Mapped[str] = mapped_column(String(16), default="allow")  # allow/deny/approval
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    approver_id: Mapped[str] = mapped_column(String(64), default="")
    rows_affected: Mapped[int] = mapped_column(Integer, default=0)
    before_image_ref: Mapped[str] = mapped_column(String(64), default="")
    cost_estimate: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    engine: Mapped[str] = mapped_column(String(32), default="postgresql")
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class DataOpBeforeImage(Base):
    """变更前镜像: UPDATE/软删命中的整行快照(层 4-4)。

    有了它, 回滚就是"照着镜像写回去", 而不是灾难。存 JSONB 而不是重建同构表:
    要能容纳未来新加的列, 且不把归档面变成"每表一张影子表"的 DDL 膨胀。
    """

    __tablename__ = "dataop_before_image"
    __table_args__ = (
        Index("ix_dataop_image_op_captured", "op_id", "captured_at"),
    )

    image_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    op_id: Mapped[str] = mapped_column(String(64), index=True)
    table_name: Mapped[str] = mapped_column(String(64), default="")
    pk_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 主键列->值
    row_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 整行
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class DataOpArchive(Base):
    """真删归档: 软删行到期真删前的整行备份(层 4-3 的"必须物理删的场景")。

    与 before_image 的差别: image 服务于"回滚这一次变更", archive 服务于"数据已经
    不在了但合规要求还能查到"。两个留存窗口各自独立, 不能合并到一个表里混口径。
    """

    __tablename__ = "dataop_archive"
    __table_args__ = (
        Index("ix_dataop_archive_table_purged", "table_name", "purge_after"),
    )

    archive_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    op_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    table_name: Mapped[str] = mapped_column(String(64), default="")
    pk_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    row_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(64), default="")
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    # 到期才允许真删(与软删保留窗口同一口径)。
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
