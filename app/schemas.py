"""Shared Pydantic schemas for API layer and internal routing."""

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Role(str, Enum):
    """Enterprise roles used for permission whitelists."""

    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR = "hr"
    FINANCE = "finance"
    ADMIN = "admin"


class DocVisibility(str, Enum):
    """文档可见性策略 (存储在文档元数据中, 检索时做前置权限裁剪)."""

    PUBLIC = "public"    # 全员可见 (制度/公告类知识)
    DEPT = "dept"        # 仅指定部门可见
    ROLE = "role"        # 仅指定角色可见
    PRIVATE = "private"  # 仅文档所有者(上传者)可见


class IntentType(str, Enum):
    """Top-level intent categories produced by the intent recognizer."""

    KNOWLEDGE_QA = "knowledge_qa"      # simple query -> RAG answer
    TOOL_CALL = "tool_call"            # complex operation -> MCP tool / 能力域进程内工具
    AGENT_DELEGATE = "agent_delegate"  # professional task -> A2A agent
    CHITCHAT = "chitchat"              # small talk -> direct LLM answer


class IntentResult(BaseModel):
    """Structured output of the intent recognizer."""

    intent: IntentType
    target: Optional[str] = Field(
        default=None,
        description="Sub-target, e.g. 'finance' / 'hr' for agents, or a tool domain.",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", description="Why the classifier picked this intent.")
    layer: Optional[str] = Field(
        default=None,
        description="Which funnel layer produced this result: rule/embedding/llm/fallback.",
    )


class ChatRequest(BaseModel):
    """Inbound chat request from the Web UI / API clients."""

    session_id: str = Field(description="Conversation session identifier.")
    user_id: str = Field(description="End-user identifier.")
    role: Role = Field(default=Role.EMPLOYEE, description="Caller role for permission checks.")
    department: str = Field(default="", description="Caller department for document-level ACL checks.")
    message: str = Field(description="User utterance.")
    thinking: Optional[bool] = Field(
        default=None,
        description="本轮是否开启深度思考; None 时取全局默认 LLM_THINKING_ENABLED。",
    )


class ChatResponse(BaseModel):
    """Outbound chat response."""

    session_id: str
    answer: str
    intent: IntentType
    route: Literal["assistant_kb", "mcp_tool", "a2a_agent", "direct"]
    target: Optional[str] = None
    trace_id: str
    # 会话记录落库后的助手消息 id(供前端历史对齐; DB 降级时为 None)
    message_id: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 结构化交付物(名称/URL/标题); office 文档下载目前走 answer 里的链接, 此字段预留给前端快捷按钮
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeChunk(BaseModel):
    """A single retrievable knowledge chunk.

    Parent-child chunking: a document is parsed into section-level parent
    blocks; oversized parents are window-split into child chunks. Retrieval
    hits children (`is_parent=False`); parents are assembled afterwards to
    provide complete section context.
    """

    chunk_id: str
    doc_id: str
    title: str
    content: str = ""          # 窄行构造点不再必须传正文(子块文本靠主键回表补)
    source: str
    modality: Literal["text", "video_transcript", "image"] = "text"
    parent_id: str = ""          # child -> parent chunk_id; parents keep ""
    is_parent: bool = False      # 值由"来自哪张表"推导, 不再是 SQL 过滤条件
    page_no: int = -1            # pdf page / pptx slide / xlsx sheet; -1 unknown
    section: str = ""            # section heading path, slide title, sheet name
    score: float = 0.0
    # --- 父子双表新增定位/展示字段 ---
    parent_type: str = "section"
    chunk_index: int = 0
    ord: int = 0
    start_offset: int = -1
    end_offset: int = -1
    content_hash: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)  # 仅展示, 权限字段禁止入内
    # --- 文档级 ACL (冗余存储在每个 chunk 上, 供向量库 Metadata Filter 前置裁剪) ---
    visibility: str = "public"   # DocVisibility 值: public/dept/role/private
    owner_id: str = ""           # 文档所有者工号 (private 判定)
    dept_id: str = ""            # 授权部门 (dept 判定)
    allowed_roles: str = ""      # 授权角色, 逗号分隔 (role 判定)


class ParentBlock(BaseModel):
    """父块轻量 DTO: 供 assemble_parents 与 Mongo 回填使用。

    与 ``KnowledgeChunk`` 区分两种粒度: 父块走 Mongo 文本, 子块走 PG ``chunk_text``。
    ``content`` 默认空(从 Mongo parent_texts 回填后才非空)。
    """

    parent_id: str
    doc_id: str
    title: str = ""
    section: str = ""
    page_no: int = -1
    parent_type: str = "section"
    ord: int = 0
    start_offset: int = -1
    end_offset: int = -1
    content_hash: str = ""
    content: str = ""
    visibility: str = "public"
    owner_id: str = ""
    dept_id: str = ""
    allowed_roles: str = ""
