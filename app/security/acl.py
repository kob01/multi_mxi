"""Document-level access control (ACL) for the RAG knowledge base.

Design (matches the platform's layered-authorization model):
- 文档权限以元数据形式冗余存储在每条向量记录上 (visibility / owner_id /
  dept_id / allowed_roles), 由统一身份系统下发的用户主体 (Principal:
  user_id / department / role) 在检索时做前置裁剪。
- 前置裁剪 (Metadata Filter): 在向量检索(pgvector)阶段用 SQL 谓词
  ``app.rag.vectorstore.build_sql_filter``, 让无权文档根本不进入候选集
  (TopK 之前), 兼顾正确性与召回效率。
- 稀疏通道 (Elasticsearch BM25) 无法用 SQL, 由 ``app.rag.bm25._acl_filter``
  构造语义等价的 bool filter。
- 最终授权 (Final Authorization): 重排/父块组装后、进入 Context Builder
  前, 再对最终资料集逐条复核一次, 作为纵深防御 (防止索引脏数据/父块
  组装引入越权块)。

可见性策略 (DocVisibility):
- public  : 全员可见 (制度/公告类知识)
- dept    : 仅 doc.dept_id 指定部门可见
- role    : 仅 allowed_roles 列表内角色可见 (逗号包裹存储, 如 ",hr,admin,")
- private : 仅所有者 owner_id 可见
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import DocVisibility, KnowledgeChunk, Role


@dataclass(frozen=True)
class Principal:
    """Caller identity resolved from the unified identity system."""

    user_id: str = ""
    department: str = ""
    role: Role = Role.EMPLOYEE

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


def format_allowed_roles(roles: list[str] | tuple[str, ...] | str) -> str:
    """Normalise a role list into the comma-wrapped storage form `,hr,admin,`.

    Wrapping with leading/trailing commas makes substring matching unambiguous
    (`,hr,` never falsely matches `shr`).
    """
    if isinstance(roles, str):
        parts = [r.strip() for r in roles.split(",") if r.strip()]
    else:
        parts = [str(r).strip() for r in roles if str(r).strip()]
    if not parts:
        return ""
    return "," + ",".join(dict.fromkeys(parts)) + ","


def parse_allowed_roles(stored: str) -> list[str]:
    """Inverse of :func:`format_allowed_roles`."""
    if not stored:
        return []
    return [r for r in stored.strip(",").split(",") if r]


def is_allowed(chunk: KnowledgeChunk, principal: Principal) -> bool:
    """Per-chunk ACL predicate — the single source of truth reused by the
    BM25 in-memory filter and the final authorization pass.

    Mirrors :func:`app.rag.vectorstore.build_sql_filter` and the ES bool filter
    exactly, so the two channels and the post-rerank re-check never disagree on
    what a principal may read.
    """
    if principal.is_admin:
        return True
    vis = chunk.visibility
    if vis == DocVisibility.PUBLIC.value or not vis:
        return True
    if vis == DocVisibility.PRIVATE.value:
        return bool(principal.user_id) and chunk.owner_id == principal.user_id
    if vis == DocVisibility.DEPT.value:
        return bool(principal.department) and chunk.dept_id == principal.department
    if vis == DocVisibility.ROLE.value:
        return principal.role.value in parse_allowed_roles(chunk.allowed_roles)
    # Unknown visibility -> default-deny.
    return False
