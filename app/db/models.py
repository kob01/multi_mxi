"""SQLAlchemy ORM models (PostgreSQL): 文档元数据 + HR/Finance/Procurement 业务表 + pgvector 知识块 + 个人记忆/用户画像 + 会话记录 + 报表产物 + 语义层(指标中台) + 智能问数留痕/评测集."""

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
    text,
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
    # 异步入库下的写入时刻: 新文档在提交入库那一事务里置 ingesting(本来就没有块,
    # 挡掉零成本); 已在架文档重入库全程保持 ready 不转 ingesting —— 发布是单事务原子
    # 操作, 把它打成 ingesting 只会让一篇正在服务的文档在队列+embed 期间整篇检不到。
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
# 入库流水线: 上传记录 / 任务状态机 / 事件表 (PG = 事实源, 外部存储靠投影收敛)
# ---------------------------------------------------------------------------
class DocUpload(Base):
    """一次上传 = 一行 ``doc_uploads`` + 一份解析产物(artifact)。

    为什么要单独成表: 上传与入库是两个时刻的动作, 中间产物必须可命名、可过期、
    可幂等复用 —— 否则 ``/upload`` 预览解析一次、``/ingest`` 再解析一次, 两次产物
    可能不一致(且 OCR 成本翻倍)。

    ``stored_rel_path`` 存**相对 upload_dir 的路径**而不是绝对路径: 绝对路径是
    容器内 ``/data/uploads/...``, 宿主脚本读到它就指向了不存在的位置。
    ``content_sha256`` 是幂等键之一: 同 doc_key 同正文的重复上传直接复用已解析产物。
    """

    __tablename__ = "doc_uploads"
    __table_args__ = (
        Index("ix_docuploads_dedup", "doc_key", "content_sha256", "status"),
        Index("ix_docuploads_status_created", "status", "created_at"),
        Index("ix_docuploads_expires", "expires_at"),
    )

    upload_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_key: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    ext: Mapped[str] = mapped_column(String(16), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    content_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    stored_rel_path: Mapped[str] = mapped_column(String(512), default="")
    modality: Mapped[str] = mapped_column(String(32), default="text")
    # pending / parsing / parsed / failed / expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    artifact_id: Mapped[str] = mapped_column(String(64), default="")
    parser_version: Mapped[str] = mapped_column(String(16), default="")
    uploaded_by: Mapped[str] = mapped_column(String(64), default="")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class IngestTask(Base):
    """一次入库 = 一行可重放的状态机任务(替代散在 ``documents.status`` 里的隐式状态)。

    三个设计点都不是可选的:
    - **task_id 确定性**(``sha1(doc_key|upload_id|content_sha256)``): 用户连点两次
      "确认入库"落进同一行, 幂等免费得到;
    - **部分唯一索引** ``ux_ingest_tasks_active_doc``: 同一文档第二个进行中任务插不
      进来(旧写法靠"请求内顺序执行"天然串行, 异步化后必须显式互斥);
    - **lease_owner/lease_expires_at + attempt**:  worker 崩溃后任务不会永远停在
      ``ingesting``, 由回收器按租约重新认领并从**当前阶段**续跑。
    """

    __tablename__ = "ingest_tasks"
    __table_args__ = (
        Index(
            "ux_ingest_tasks_active_doc", "doc_key", unique=True,
            postgresql_where=text(
                "stage IN ('pending','parsing','embedding','publishing','indexing')"
            ),
        ),
        Index("ix_ingest_tasks_claim", "stage", "lease_expires_at"),
        Index("ix_ingest_tasks_doc_created", "doc_key", "created_at"),
    )

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_key: Mapped[str] = mapped_column(String(32), index=True)
    upload_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    content_sha256: Mapped[str] = mapped_column(String(64), default="")
    # pending / parsing / embedding / publishing / indexing / ready / failed
    stage: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 提交时已校验过的入库参数: {"tags": [...], "acl": {...}, "uploader": "..."}
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str] = mapped_column(Text, default="")
    # 阶段产物快照({"written": N, "marked": [chunk_id...], "warnings": [...]})
    # 存在这里才让 indexing 阶段可重放: 重建 ES 时要删的正是发布阶段标掉的块。
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OutboxEvent(Base):
    """事务性发件箱: 与事实源写同一事务提交, 再由 relay 投到 Redis Streams。

    为什么必须有: 直接"写 PG 再发 Redis"是第二个双写点 —— 发了事件但事务回滚,
    或提交成功但 Redis 抖动没发出去, 都会留下悬挂状态。事件行与业务行同事务提交后,
    至多"晚投", 不会"错投/漏投"; Redis 在这里只当低延迟的 handoff 通道, 真正的
    认领依据是 ``ingest_tasks`` 本身(db_poll 自愈循环)。
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_claim", "status", "event_id"),
        Index("ix_outbox_retry", "status", "next_retry_at"),
    )

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # parse.requested / ingest.requested / doc.acl_changed / doc.deleted
    topic: Mapped[str] = mapped_column(String(32), index=True)
    doc_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # new / sent / failed
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
# 文档正文外置: PG 父子双表 (单版本覆盖语义 + 陈旧标记, chunk_text 留 PG, 父块全文在 Mongo)
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
        Index(
            "ix_doc_parents_deprecated", "deprecated_at",
            postgresql_where=text("deprecated_at IS NOT NULL"),
        ),
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
    # 陈旧标记: 发布事务只把本次不再引用的块标上时间戳, 物理删除交后台 GC。
    # 为什么不用真版本列: 历史版本向量会挤占 HNSW TopK 拉低召回(已定的去版本化
    # 决策); 但在主链路 DELETE 大块文本行会长时间持锁并给并发查询留空窗。标记列
    # 同时消掉这两个问题: 检索侧一律 ``deprecated_at IS NULL``, 旧行在新行落地的
    # 同一瞬间不可见, 再由 GC 分批真删。
    deprecated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
        # GC 只需要扫"已标陈旧且过了宽限期"的那一小撮, 部分索引不进入热路径成本。
        Index(
            "ix_doc_chunks_deprecated", "deprecated_at",
            postgresql_where=text("deprecated_at IS NOT NULL"),
        ),
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
    # 陈旧标记(与 doc_parents.deprecated_at 同义): upsert 新行时必须重置为 NULL,
    # 否则同 chunk_id 重入库后会被 GC 当成旧行删掉。
    deprecated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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


# ---------------------------------------------------------------------------
# 语义层(指标中台元数据): AskData_Agent 的"组织级共享长期记忆"
#
# 为什么是表而不是配置文件: 语义层要被人修订、要被 badcase 回流补词、要被问数链路
# 按向量召回 —— 它是一个需要审计与并发读的数据面, 不是一次性启动参数。而
# `docker/init/*.sql` 只在空卷首建时执行(存量库永不重放), 所以 DDL 必须走 create_all。
# 三张 sem_ 表都**不带 dept_id**: 它们是"定义"而不是"业务数据", 隔离粒度是角色级
# (见 sem_metrics.allowed_roles), 真正的行隔离仍在被引用表的 RLS 上。
# ---------------------------------------------------------------------------
class MetricDef(Base):
    """一个受治理的指标定义(问数只能命中这里登记过的口径)。

    ``expression`` 是**列级片段**而不是整条 SQL: 编译器只把它插进 SELECT 子句, 列名
    必须落在该表真实列集合内(见 app/db/policy.table_columns 的对账口径), 于是
    "模型说我要 SUM(pg_read_file(...))"这类面根本不存在。
    """

    __tablename__ = "sem_metrics"
    __table_args__ = (
        Index(
            "ix_sem_metrics_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        Index("ix_sem_metrics_status_domain", "status", "domain"),
    )

    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), default="")       # 中文名
    domain: Mapped[str] = mapped_column(String(32), default="")       # expense/budget/hr/leave/purchase/contract
    unit: Mapped[str] = mapped_column(String(16), default="")         # 元/单/天/人次/%
    agg: Mapped[str] = mapped_column(String(16), default="sum")       # sum/count/avg/max/min/ratio/count_distinct
    source_table: Mapped[str] = mapped_column(String(64), default="")
    expression: Mapped[str] = mapped_column(Text, default="")         # 受控聚合片段(列名须在白名单内)
    caliber_where: Mapped[str] = mapped_column(Text, default="")      # 口径谓词(服务端文本, 不含值)
    join_sql: Mapped[str] = mapped_column(Text, default="")           # 需要的 JOIN(服务端模板)
    time_column: Mapped[str] = mapped_column(String(64), default="created_at")
    compatible_dims: Mapped[list | None] = mapped_column(JSON, nullable=True)
    calendar: Mapped[str] = mapped_column(String(16), default="month")  # 默认时间粒度
    # 逗号包裹的角色串 ",finance,admin,": 空串 = 继承 analytics 域级白名单。
    allowed_roles: Mapped[str] = mapped_column(String(128), default="")
    # 口径出处: "reports:expense_total" 表示与周报共用同一份常量(防两版数字对不上)。
    caliber_ref: Mapped[str] = mapped_column(String(64), default="")
    # 口径负责人: 语义层修订是治理动作, 必须有一个可以追的人(工号或邮箱前缀)。
    owner: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/draft
    # 单对象版本: 每次同步真值(YAML)进库时内容变了才 +1; 聚合指纹见 sem_change_log.semver。
    version: Mapped[int] = mapped_column(Integer, default=1)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class DimensionDef(Base):
    """一个可下钻/可过滤的维度(枚举值反查与维度兼容性判定的依据)。"""

    __tablename__ = "sem_dimensions"
    __table_args__ = (
        Index(
            "ix_sem_dimensions_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    dim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    source_table: Mapped[str] = mapped_column(String(64), default="")
    column: Mapped[str] = mapped_column(String(64), default="")
    join_sql: Mapped[str] = mapped_column(Text, default="")
    column_prefix: Mapped[str] = mapped_column(String(8), default="")  # 多表 JOIN 时的列前缀别名
    # static = 枚举落 enum_values; column:table.col = 从库里反查(不写死, 主数据会变)。
    enum_source: Mapped[str] = mapped_column(String(64), default="static")
    enum_values: Mapped[list | None] = mapped_column(JSON, nullable=True)
    time_like: Mapped[bool] = mapped_column(Boolean, default=False)
    # 维度树(如 ["地区","省","市"]): 下钻按层级逐级展开用, 平铺维度留空。
    hierarchy: Mapped[list | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class TermAlias(Base):
    """说法 -> 语义对象 的映射(同义词/业务黑话/表头简称), 向量召回的载体。"""

    __tablename__ = "sem_terms"
    __table_args__ = (
        UniqueConstraint("term", "term_type", "target_id", name="uq_sem_terms_term_target"),
        Index(
            "ix_sem_terms_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        Index("ix_sem_terms_target", "term_type", "target_id"),
    )

    term_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(128), index=True)
    term_type: Mapped[str] = mapped_column(String(16), default="metric")  # metric/dim/enum
    target_id: Mapped[str] = mapped_column(String(64), default="")
    weight: Mapped[float] = mapped_column(Numeric(4, 3), default=1.0)  # 同分时的优先级
    # manual=人工登记; seed=随语义层灌入; badcase=归因路由产出的补词草案
    source: Mapped[str] = mapped_column(String(16), default="seed")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/draft
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 语义层细分面: 枚举值 / 指标-维度兼容性 / 时间语义 / 行列策略
# (把“字段”拆成“表”的理由不是洁瘾: 枚举值要带热度参与打分、兼容性要能挂
# default_filter、时间语义是全局配置而不是逐指标字段 —— 三者都无法塞进原表的一列。)
# ---------------------------------------------------------------------------
class DimValueDef(Base):
    """一个维度的取值(含黑话别名与热度): 枚举反查从“ SELECT DISTINCT 现查”升级成可治理的表。"""

    __tablename__ = "sem_dim_values"
    __table_args__ = (
        UniqueConstraint("dim_id", "value", name="uq_sem_dim_values_dim_value"),
        Index(
            "ix_sem_dim_values_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    value_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dim_id: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[str] = mapped_column(String(128))
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 热度: 从 askdata_query_log 的过滤条件里统计出来(见 scripts/refresh_dim_popularity),
    # 不是人工拍的数字 —— 它是“同一个维度里用户更常提到哪个值”的观测量。
    popularity: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class MetricDimCompatDef(Base):
    """指标×维度兼容性(兼容不是布尔: 不兼容时可以带一个默认过滤把问法救回来)。"""

    __tablename__ = "sem_metric_dim_compat"
    __table_args__ = (
        UniqueConstraint("metric_id", "dim_id", name="uq_sem_metric_dim_compat"),
        Index("ix_sem_metric_dim_compat_dim", "dim_id"),
    )

    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    compat: Mapped[str] = mapped_column(String(16), default="ok")  # ok/no/default_filter
    # {"dim": "<dim_id>", "op": "eq", "value": ...}: compat=default_filter 时编译器自动补上。
    default_filter: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class TimeSemanticDef(Base):
    """时间语义(自然/财年): 周起点与同比基期规则是全局口径, 不能写在每个指标行里。"""

    __tablename__ = "sem_time_semantics"

    cal_type: Mapped[str] = mapped_column(String(16), primary_key=True)  # natural/fiscal
    week_start: Mapped[int] = mapped_column(Integer, default=1)  # 1=周一(PG date_trunc 已是 ISO)
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1)
    # 同比基期规则: same_calendar_day(同日历日) / same_weekday(同星期) / prior_period_end。
    compare_base_rule: Mapped[str] = mapped_column(String(32), default="same_calendar_day")
    timezone: Mapped[str] = mapped_column(String(32), default="Asia/Shanghai")
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class RowPolicyDef(Base):
    """行级策略模板(在 RLS 之上再叠一层“按问数语义改写 WHERE”)。

    为什么不只靠 RLS: 有些口径限制是**指标维度**的(如“部门角色只能看自己部门且已归档的
    合同”), RLS 只能按登录身份判, 拿不到“这次问的是哪个指标”。两层取交集:
    本表改写的是额外谓词, RLS 仍是底线(绕过本表也读不到越权行)。
    """

    __tablename__ = "sem_row_policies"
    __table_args__ = (
        Index("ix_sem_row_policies_role_table", "role", "table_name"),
    )

    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), index=True)  # employee/manager/finance/admin/*
    table_name: Mapped[str] = mapped_column(String(64), default="")
    # 谓词模板: 列位用事实表别名, 变量位只允许 {dept_id}/{tenant_id}/{user_id}/{today}
    # (由服务端从已验证作用域代入, 不接受模型输入)。
    predicate: Mapped[str] = mapped_column(Text, default="")
    metric_id: Mapped[str] = mapped_column(String(64), default="")  # 空 = 不限指标
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class ColPolicyDef(Base):
    """列级策略(按角色剔除敏感列): 在出口 DLP 之前先不让它进 SELECT。"""

    __tablename__ = "sem_col_policies"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    table_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    denied_cols: Mapped[list | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class SemChangeLogDef(Base):
    """语义层版本日志: Trace 里的 semver 要能反查到“当时到底是哪一份定义”。

    为什么必需: 没有它, 一个月后的 badcase 只能看到“口径不对”而无法回答
    “口径是什么时候被谁改坏的”; 有了它, 回放时按 semver 取那一版定义就能重现。"""

    __tablename__ = "sem_change_log"
    __table_args__ = (
        Index("ix_sem_change_log_semver", "semver"),
        Index("ix_sem_change_log_created", "created_at"),
    )

    log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 定义集内容指纹(与 MetricCatalog.semver 同一算法): 一份快照一个号。
    # 不再写 ``index=True``: 它按命名约定生成的索引名就是 ``ix_sem_change_log_semver``,
    # 与 __table_args__ 里那条同名 —— create_all 会连着跑两条 CREATE INDEX,
    # 第二条报 relation already exists 并把整个 init_schema 事务回滚掉
    # (表现为"网关启动失败 + 文档管理/知识库整块不可用")。索引入 __table_args__ 单点声明。
    semver: Mapped[str] = mapped_column(String(32))
    object_kind: Mapped[str] = mapped_column(String(32), default="catalog")  # catalog/表名
    # 对象标识可以是复合主键(如 `m_expense_total|d_expense_employee_name`), 宽度必须容纳它:
    # 以前登记 16 时整个同步事务会在写版本日志时报 value too long(定义行跟着回滚)。
    object_id: Mapped[str] = mapped_column(String(96), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    actor: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(16), default="yaml_sync")  # yaml_sync/manual/seed/badcase
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 智能问数: 全链路留痕与评测集
# ---------------------------------------------------------------------------
class AskDataQueryLog(Base):
    """一次问数的完整链路记录: 看板指标与 badcase 归因的唯一原料。

    为什么不只写 audit.jsonl: JSONL 是给 SIEM 采集的流, 回答不了"上周澄清触发率是多少"
    这种聚合问题; 为什么不只靠 LLM judge: 归因要能定位到具体槽位与候选, 必须结构化存。
    """

    __tablename__ = "askdata_query_log"
    __table_args__ = (
        Index("ix_askdata_log_verdict_created", "verdict", "created_at"),
        Index("ix_askdata_log_user_created", "user_id", "created_at"),
    )

    query_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid hex
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    tenant_id: Mapped[str] = mapped_column(String(32), default="")
    user_id: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    slots_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)       # 槽位 + confidence
    candidates_json: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 三路召回候选
    dsl_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)         # LLM 产出的 DSL
    # metric_api / compiler / sandbox / clarify / repair / degrade
    route: Mapped[str] = mapped_column(String(24), default="")
    final_sql: Mapped[str] = mapped_column(Text, default="")
    rowcount: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    token_cost: Mapped[int] = mapped_column(Integer, default=0)
    # 本轮 LLM 调用次数与重试数(预算断与自修复烧了多少 token 要看这两个数)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # 本轮用的语义层快照指纹: badcase 回溯到当时口径的唯一依据
    semver: Mapped[str] = mapped_column(String(32), default="", index=True)
    catalog_origin: Mapped[str] = mapped_column(String(16), default="")
    # 结果缓存命中(B1): 命中率是要看的数, 但不能靠"把 route 写成 cache"来记
    # (那会把"一次通过率"的分母搞坏)。
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 本轮主指标的量级({"shape": "d_x,d_y", "metrics": {"m_expense_total": 1234.5}}):
    # 结果校验拿它算历史基线 z-score(G6)。为什么只存每个指标一个代表值而不是整行结果:
    # 留痕表是 admin 可读的聚合面, 把整张结果表写进来等于把行级权限的边界搬到应用层重做一遍。
    metric_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # ok / clarify / denied / fail / degraded
    verdict: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    error_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # up / down / rewritten (None = 未反馈)
    feedback: Mapped[str] = mapped_column(String(16), default="")
    # intent / link / dsl / exec / render
    attribution: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, server_default=func.now()
    )


class AskDataGolden(Base):
    """自建 golden 评测集: 真实业务问法 -> 期望 DSL/SQL/结果集。

    公开基准(BIRD/Spider)标注噪声大, 只作参考不进回归; 本表是"改了 prompt/语义层会不会
    把准确率搞坏"的唯一判据, 因此期望结果存**行数据**而不是存 SQL 文本(文本等价但结果
    不同也算错, 反过来也一样 —— EX 以结果集为准)。

    ``embedding`` 不只服务检索评测: 它同时是 **语义缓存**的基底(方案 §3 V3) ——
    新问法与某条已标注问法相似度≥阈值且 ``semver`` 与当前一致时, 直接复用它的 DSL。
    复用的是"意图"而不是"数据": 校验、编译、RLS、审计一步不少。
    """

    __tablename__ = "askdata_golden"
    __table_args__ = (
        Index("ix_askdata_golden_enabled_tag", "enabled", "tags"),
        Index(
            "ix_askdata_golden_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question: Mapped[str] = mapped_column(Text, default="")
    dsl_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 期望槽位(与 DSL 分开存): slot-F1 需要“应该抽出什么”而不是“应该怎么算”;
    # 没标注的用例只进 EX/DSL-EM, 不计入 slot-F1 分母。
    slots_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expected_sql: Mapped[str] = mapped_column(Text, default="")
    expected_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[str] = mapped_column(String(128), default="")  # 逗号分隔: 环比/下钻/澄清
    # 难度分层(easy/medium/hard): 验收线按层看, 只看总分会把“简单题满分 + 难题全错”抹平
    difficulty: Mapped[str] = mapped_column(String(16), default="easy", index=True)
    intent: Mapped[str] = mapped_column(String(16), default="", index=True)  # fetch/compare/topn/trend/composition
    note: Mapped[str] = mapped_column(Text, default="")
    # 这条用例的标准答案是按哪一版语义层算出来的(语义缓存只在同版时复用)
    semver: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 这条问法是否允许拿去做语义缓存。默认 **false**，两道理由:
    # 1. 评测有效性: agent 模式的题目就来自 golden, 全部可缓存等于让模型拄标准答案,
    #    EX 会刷成 100% 而 NL→DSL 能力一点没变(本轮实踩: 165/277 轮带"相似度 1.000 复用"注记);
    # 2. 生产口径: 批量生成的合成问法没经过人逐条核对, 不该拿去服务真实用户。
    # 只有人工转正的高频真实问法才应该置 true(治理面 POST /golden/{case_id}/cacheable)。
    cacheable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AskDataEvalRun(Base):
    """一次离线评测的结果概要(只存结论, 逐条明细留在报告文件里)。

    为什么不只存文件: 看板要回答"EX 现在多少、比上一跑好了还是坏了", 而
    ``reports/*.md`` 在容器里不是共享卷、也不是能聚合的面。一行一个结论比
    去解构 markdown 便宜得多, 也与 ``sql_audit_records`` 的"结构化回查面"同构。
    """

    __tablename__ = "askdata_eval_run"
    __table_args__ = (
        Index("ix_askdata_eval_run_started", "started_at"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    exec_accuracy: Mapped[float] = mapped_column(Numeric(5, 2), default=0)  # EX %
    # DSL-EM(结构完全匹配) 与 slot-F1: 两个都是“没执行就能算”的便宜指标,
    # 它们把“口径选错”与“结果碰对了”分开看, 只看 EX 会把前者埋掉。
    dsl_exact_match: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    slot_f1: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    first_pass_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    clarify_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    # 误澄清率: 澄清了但用户选的正是最高分候选(本来不必问) —— 这才是澄清代价
    mis_clarify_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    degrade_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    fallback_rate: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    p95_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # CI 门禁结论: 与上一同模式 run 比 EX 降幅超阈值就标 red(脚本退码非 0)
    gate_status: Mapped[str] = mapped_column(String(16), default="")
    gate_note: Mapped[str] = mapped_column(String(256), default="")
    baseline_run_id: Mapped[str] = mapped_column(String(64), default="")
    semver: Mapped[str] = mapped_column(String(32), default="")
    # 降级标记: judge 不可用时只跑 EX, 结论不能与全量跑同口径比较。
    mode: Mapped[str] = mapped_column(String(32), default="full")
    report_path: Mapped[str] = mapped_column(String(512), default="")
    summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")


class AskDataFeedbackEvent(Base):
    """结构化反馈事件(like/dislike/edit_query/edit_dsl/copy/drill/ticket)。

    为什么不只在 query_log 上记一个 feedback 字段: 归因决策树要拿“用户改了什么”当
    证据(edit_dsl 的 diff 直接映射到 DSL 生成的哪个字段错了), 一个枚举值里没有这个信息;
    而一行一个事件才能同时接住“同一轮既点踩又改了 DSL”。
    """

    __tablename__ = "askdata_feedback_event"
    __table_args__ = (
        Index("ix_askdata_event_query_type", "query_id", "event_type"),
        Index("ix_askdata_event_created", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    query_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # like / dislike / edit_query / edit_dsl / copy / drill / ticket / noanswer
    event_type: Mapped[str] = mapped_column(String(24), default="", index=True)
    # 事件载荷: edit_dsl 存修正后的 DSL, drill 存下钻维度, dislike 存可选理由码。
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 归因不只看事件类型, 还看“当时链路走到哪一步挂了”: 这里回写当时阶段供看板与回放
    stage: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AskDataSample(Base):
    """SFT/DPO 样本队列(方案 §4⑥ 的"样本回流"在本期的形态)。

    为什么不把 badcase 直接当训练集: 错的样本比不训更危险(它会教模型去犯同一个错)。
    所以这里只收**同时满足两个条件**的轮次: ① DSL 过了校验器且编译执行成功(它是可验证的口径),
    ② 用户给过肯定反馈或亲自改写过(它是人认可的答案), 其余只进草案不进这里。
    状态机仍是 `draft`(待人工复核) -> `approved` -> `exported`：导出脚本只读 approved。

    `rejected_json` 只属于 dpo: 它是"模型当时给错的那一版", 与 chosen 成对才有意义;
    拿两个不同问法的样本凑成对会把偏序方向教反。
    """

    __tablename__ = "askdata_sample"
    __table_args__ = (
        Index("ix_askdata_sample_status_kind", "status", "kind"),
        Index("ix_askdata_sample_created", "created_at"),
    )

    sample_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # sft = 一条"人话 -> 正确 DSL"; dpo = 一个偏好对(chosen/rejected)
    kind: Mapped[str] = mapped_column(String(16), default="sft", index=True)
    query_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    # 样本主体: 当时真正该用的 DSL(已过校验与执行)
    chosen_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rejected_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    slots_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    semver: Mapped[str] = mapped_column(String(32), default="")
    # 归因类别(intent/link/dsl/exec/render): 只有语言到 DSL 的两类才是训练材料
    category: Mapped[str] = mapped_column(String(16), default="")
    # 两道门禁的结论(写成列而不是藏在 note 里: 导出条件要能被 SQL 查)
    gate_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    gate_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # draft / approved / rejected / exported
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )


class AskDataTicket(Base):
    """人工工单(FALLBACK 链最后一级的出口)。

    为什么必须有: 问数的降级链只能给“建议问法”, 但有些问法是**真的该由人看**
    (口径不存在 / 数据本身缺失 / 用户不信任结果)。没有工单就是“系统默默把球掉在地上”。
    """

    __tablename__ = "askdata_ticket"
    __table_args__ = (
        Index("ix_askdata_ticket_status_created", "status", "created_at"),
    )

    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    query_id: Mapped[str] = mapped_column(String(64), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    # 开单原因码: no_metric / no_data / budget_exceeded / engine_error / user_request
    reason: Mapped[str] = mapped_column(String(32), default="")
    dsl_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    semver: Mapped[str] = mapped_column(String(32), default="")
    requested_by: Mapped[str] = mapped_column(String(64), default="", index=True)
    assigned_to: Mapped[str] = mapped_column(String(64), default="")
    # open / processing / resolved / rejected
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )
