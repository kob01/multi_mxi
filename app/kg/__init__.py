"""文档知识图谱 (GraphRAG): 实体级关联 + 关联文档可视化。

与对话长期记忆的 Graph 通道(``app.memory.graph_store``, 按 user_id 隔离的
``:MemoryEntity``)刻意分开: 本包构建的是**全局知识库级别**的文档图谱, 使用全新
标签命名空间(``:KgDoc`` / ``:KgEntity`` / ``:MENTIONS`` / ``:KG_REL``), 复用同一个
Neo4j 驱动单例。文档之间通过"共同提及的实体"间接关联, 页面据此可视化"有关联的文档"。

权限只在查询出口做过滤: 先由 PostgreSQL ``documents`` (ACL 事实来源)算出 principal
可访问的 ``doc_key`` 集合, 再据此裁剪 Neo4j 子图, 无权文档节点及其独占边一律不返回。

所有对 Neo4j 的读写在驱动不可用/开关关闭时静默降级(对齐 graph_store 的策略), 不阻断
入库与对话。
"""
