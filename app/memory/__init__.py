"""长期记忆: Vector(pgvector) + Graph(Neo4j) 双通道。

与"短期"的 Session Memory(仍在 ``app.assistant.memory``, 按会话滚动窗口 +
摘要)分开: 本包存的是跨会话、按用户隔离的长期事实/偏好, 以及实体关系图。
"""
