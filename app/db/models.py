"""SQLAlchemy ORM models (MySQL): 文档元数据 + HR/Finance 业务表."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    parsed_text: Mapped[str | None] = mapped_column(MEDIUMTEXT, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    # --- 文档级 ACL (权限存储在元数据中; 检索时经 Milvus Metadata Filter 前置裁剪) ---
    visibility: Mapped[str] = mapped_column(String(16), default="public")  # public/dept/role/private
    owner_id: Mapped[str] = mapped_column(String(64), default="")         # private: 所有者工号
    dept_id: Mapped[str] = mapped_column(String(64), default="")           # dept: 授权部门
    allowed_roles: Mapped[str] = mapped_column(String(128), default="")    # role: 逗号包裹 ",hr,admin,"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Tag(Base):
    """A document category tag (LLM-suggested or user-defined)."""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(16), default="llm")  # llm / custom
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class HRTicket(Base):
    """HR 服务工单."""

    __tablename__ = "hr_tickets"

    ticket_no: Mapped[str] = mapped_column(String(32), primary_key=True)  # HR1000+
    emp_id: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32))  # 入职/离职/考勤/薪酬/证明开具/其他
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN/PROCESSING/DONE/CANCELLED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class DepartmentBudget(Base):
    """部门年度预算."""

    __tablename__ = "fin_department_budgets"
    __table_args__ = (UniqueConstraint("department", "year"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    department: Mapped[str] = mapped_column(String(64), index=True)
    year: Mapped[int] = mapped_column(Integer)
    annual_budget: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    used_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
