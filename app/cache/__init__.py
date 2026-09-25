"""记忆层 + 缓存层。

按目标架构图分两组能力:
- ``app.cache``: Prompt Cache / Retrieval Cache / Tool Cache, 统一落 Redis。
- ``app.memory``: 长期记忆的 Vector(pgvector) 与 Graph(Neo4j) 通道。

Session Memory(Redis) / Working State Checkpoint(Redis) 不属于本包, 分别仍在
``app.assistant.memory`` / ``app.assistant.graph`` 内(它们是 Assistant 编排的
一部分, 不是通用的记忆/缓存基础设施)。
"""
