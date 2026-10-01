"""单据号生成: 用 PostgreSQL 序列取代 ``SELECT MAX(...)+1``。

原先三个业务 server 都这么取号(``MAX(尾号)+1``), 有两个后果:

1. **并发重号**: 两个 ``create_*`` 同时读到同一个 max, 第二个 INSERT 撞主键;
   而写工具原先连 ``SQLAlchemyError`` 都没接, 异常经 ToolNode 变成一句噪声文本,
   用户侧表现为"提交失败但不知道为什么"。
2. **号段可枚举**: 单号是自增的, 配合"按单号取数不校验归属"就是横向越权的取号器
   (归属校验见 app/security/caller.py, 这一层只负责把号段本身的规律性降下来 ——
   即使校验被绕过, 也不能靠猜号成批命中)。

序列按 ``CREATE SEQUENCE IF NOT EXISTS`` 惰性建, 不依赖迁移脚本(容器里表由
``init_schema`` 的 ``create_all`` 建, 序列同理); 首次创建时把当前值对齐到表内已有
最大尾号, 避免把存量单据的号再发出去一遍。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 序列名/表名/列名都来自代码里的常量, 但仍按标识符白名单校验一次: 防将来把用户输入
# 拼进 DDL(那是比重号更糟的注入面)。
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# 本进程已确认存在的序列(避免每次取号都跑一条 DDL)。
_ensured: set[str] = set()


def next_numbered(
    session: Session,
    *,
    sequence: str,
    prefix: str,
    table: str,
    column: str,
    start: int,
) -> str:
    """取下一个单据号 ``{prefix}{序列当前值}``。

    Args:
        sequence: 序列名(小写标识符)。
        prefix: 单号前缀, 如 ``HR`` / ``FIN`` / ``PO`` / ``CT``。
        table: 单据表名(只为对齐存量最大尾号)。
        column: 单号列名。
        start: 号段起点(与既有 ``MAX`` 兜底值同口径, 如 HR 用 1000)。
    """
    for name in (sequence, table, column):
        if not _IDENT_RE.match(name):
            raise ValueError(f"非法标识符, 拒绝拼进 DDL: {name!r}")

    if sequence not in _ensured:
        existed = session.execute(
            text("SELECT to_regclass(:name)"), {"name": sequence}
        ).scalar()
        session.execute(text(f"CREATE SEQUENCE IF NOT EXISTS {sequence} START {start}"))
        if existed is None:
            max_no = session.execute(
                text(
                    f"SELECT COALESCE(MAX(CAST(SUBSTRING({column} FROM '[0-9]+$') AS BIGINT)), 0) "
                    f"FROM {table}"
                )
            ).scalar_one()
            if int(max_no or 0) >= start:
                session.execute(
                    text("SELECT setval(CAST(:seq AS regclass), CAST(:val AS bigint), true)"),
                    {"seq": sequence, "val": int(max_no)},
                )
                logger.info("sequence %s aligned to existing max %s", sequence, max_no)
        _ensured.add(sequence)

    value = session.execute(text(f"SELECT nextval('{sequence}')")).scalar_one()
    return f"{prefix}{int(value)}"
