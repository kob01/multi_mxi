"""个人级记忆: User Memory(profile/preference/habit) + Episodic + Personal Knowledge + Personal Graph。

与"短期"的 Session Memory(仍在 ``app.assistant.memory``, 按会话滚动窗口 +
摘要)分开: 本包存的是跨会话、按用户隔离的长期记忆, 以及实体关系图。

模块分工:
- ``taxonomy``     桶语义的唯一事实源(kind / 注入方式 / 中文标签 / prompt 小节标题)
- ``vector_store`` 记忆条目桶(preference/habit/episode/knowledge)的 pgvector 读写
- ``profile_store`` 画像桶(user_profiles 表, 一人一条, 确定性合并)
- ``graph_store``  个人图谱通道(Neo4j, 以 :MemoryUser 为锚点)
- ``extraction``   一次 LLM 调用产出全部桶的提取器
- ``personal``     编排层: 读路径拼 Business Context, 写路径分桶落盘 + 情节蒸馏
"""
