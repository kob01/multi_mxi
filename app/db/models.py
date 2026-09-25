"""SQLAlchemy ORM models (PostgreSQL): 文档元数据 + HR/Finance 业务表 + pgvector 知识块 + 会话记录."""

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


# ---------------------------------------------------------------------------
# RAG 知识块
# ---------------------------------------------------------------------------
class KnowledgeChunkRow(Base):
    """One retrievable chunk (parent or child) with its dense vector.

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
# 长期记忆 (Vector 通道)
# ---------------------------------------------------------------------------
class LongTermMemoryRow(Base):
    """一条跨会话长期记忆(事实/偏好) + 稠密向量, 按 ``user_id`` 隔离。

    与 ``knowledge_chunks`` 是两张不同的表: 知识库是全企业共享的文档, 长期记忆
    是"这个用户"自己的对话沉淀, 权限语义完全不同(必须按 user_id 严格隔离,
    不能像文档那样走 public/dept/role 可见性模型), 因此不复用同一张表。
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
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="fact")  # fact/preference
    content: Mapped[str] = mapped_column(Text, default="")
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
