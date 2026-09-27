# 记忆 + 缓存能力落地方案

## 目标
按用户给出的两张架构图补齐：
- 记忆层：Session Memory(Redis) / Working State(Checkpoint) / Long Memory(Vector + Graph) → 汇入 Business Context
- 缓存层：Prompt Cache / Retrieval Cache / Tool Cache → 统一落 Redis

已确认选型：Checkpoint 用 **Redis**（`langgraph-checkpoint-redis`），长期记忆 Graph 通道用 **Neo4j**（新增 docker 服务），Vector 通道复用现有 pgvector。

## 新增依赖（pyproject.toml + requirements.txt）
- `redis>=5.0`（asyncio 客户端）
- `langgraph-checkpoint-redis>=0.0.x`（AsyncRedisSaver）
- `neo4j>=5.x`（官方 AsyncGraphDatabase 驱动）

## 新增配置（app/config.py）
在 `Settings` 中加：
- `redis_url: str = "redis://localhost:6379/0"`，`redis_enabled: bool = True`
- `checkpoint_enabled: bool = True`（关闭时退回进程内 `InMemorySaver`，行为同现状）
- `session_memory_ttl: int = 3600`
- `neo4j_uri: str = "bolt://localhost:7687"`，`graph_memory_enabled: bool = True`（Neo4j 单节点内网，`NEO4J_AUTH=none`，与 ES `xpack.security.enabled=false` 同一先例，无需 secret 文件）
- `long_term_memory_enabled: bool = True`（Vector 通道总开关）
- `cache_enabled: bool = True`，`prompt_cache_ttl/retrieval_cache_ttl: int = 300`，`tool_cache_ttl: int = 30`（Tool Cache TTL 最短，因余额/审批状态是实时数据）
- 全部走现有 `env_file=(".env", "docker/.env")` 机制，无需额外加载顺序改动

## 新增模块

### app/cache/（Redis 基础设施 + 三类缓存）
- `redis_client.py`：`get_redis() -> redis.asyncio.Redis | None` 进程级单例；连接失败/`redis_enabled=false` 时返回 `None`（只 WARNING，不抛异常），所有调用方必须容忍 `None` 走降级分支——与 `HybridRetriever`/`reranker` 现有降级模式一致。
- `prompt_cache.py`：`async cached_llm(prompt_key: dict) -> str | None` / `store`，key = sha256(model + temperature + 渲染后的完整 prompt)。
- `retrieval_cache.py`：`async cached_retrieve(query, top_k, top_n, principal) -> (chunks, score_mode) | None`，key 必须包含 ACL 签名（`role + department + user_id`），避免不同权限用户共享同一份命中结果（对应 `graph.py` 里 `Principal` 的语义）；提供 `invalidate_all()`。
- `tool_cache.py`：`async cached_call(server_or_domain, tool_name, args, role) -> str | None` / `store`，key 同样带 `role`（权限敏感）。

### app/memory/（长期记忆，与短期 `app/assistant/memory.py` 区分开）
- `vector_store.py`：新表 `long_term_memories`（SQLAlchemy 模型加到 `app/db/models.py`，参照 `KnowledgeChunkRow` 的 HNSW 索引写法）：`id, user_id, kind(fact/preference/summary), content, source_session_id, embedding Vector(dim), created_at, last_accessed_at`；封装 `upsert_memory`（先做同用户下 cosine 相似度 > 0.92 查重，命中则只更新 `last_accessed_at`/`content`，不重复插入）与 `search_memories(user_id, query_vec, top_k)`。
- `graph_store.py`：`neo4j.AsyncGraphDatabase` 单例封装，`ensure_schema()`（建唯一约束/索引，幂等），`upsert_entities(user_id, entities, relations)`（`MERGE` 语义），`related_facts(user_id, entity_names, hops=2)`。
- `extraction.py`：新增 prompt（`app/assistant/prompts.py` 里加 `MEMORY_EXTRACTION_PROMPT`），一次 LLM 调用同时产出 `{facts: [...], entities: [...], relations: [...]}`，供 Vector 通道（facts）和 Graph 通道（entities/relations）共用，避免两次调用。

## 改造点

### app/assistant/memory.py（Session Memory -> Redis）
- `history_text` / `append` / `get` 改为 `async`，Redis 可用时用 `LPUSH`+`LTRIM`（滚动窗口）+ `SET summary`（`EXPIRE session_memory_ttl`）替代进程内 dict；Redis 不可用时保留当前 dict 行为作为降级路径。
- 同步更新调用方 `graph.py`：`load_context`、`persist_memory`、`handle()` 里审计日志读取 `history_text` 的三处，全部改为 `await`。

### app/assistant/graph.py
- `AssistantOrchestrator.__init__` 不再直接 `_build_graph()`；新增 `async setup()`：按 `checkpoint_enabled`/`redis_enabled` 选择 `AsyncRedisSaver`（`from_conn_string` + `await setup()`）或 `InMemorySaver`，再 `self._build_graph(checkpointer)`。`handle()` 首次调用前幂等 `await self.setup()`。
- `handle()` 调 `ainvoke` 时传 `config={"configurable": {"thread_id": session_id}}`（`AssistantState` 全部字段在每次调用时都被显式重置，不会与历史 checkpoint 状态串台）。
- 新增/改造节点 `build_context`（替换现有 `load_context`）：并行拉 Redis Session Memory + `long_term_memories` 向量检索 + Neo4j `related_facts`，拼成统一 `history` 文本（新增字段 `memory_ctx`，与 `history` 合并传给下游 prompt），三路均单独 try/except 降级，任一子通道不可用只影响拼接内容，不阻断对话。
- `persist_memory` 节点：在现有会话摘要归档之外，触发 `extraction.py` 做长期记忆提取（跳过 `route == "direct"` 的闲聊轮次，省 LLM 调用），写 Vector（`long_term_memories`）+ 写 Graph（`graph_store.upsert_entities`），均在 `try/except` 内，失败只记审计不抛出。
- `kb_retrieve`：先查 `retrieval_cache`，未命中再走现有混合检索链路，最后写回缓存；`refresh_knowledge()`（现已被 `app/docs/service.py` 的 `ingest_confirmed` 等 3 处调用）里追加 `await invalidate_all()`。
- `tool_execute` / `agent_delegate` / `call_mcp_tool*` / `A2AClientPool.send`：调用前查 `tool_cache`（key 含 `role`），未命中再真实调用并写回。
- `rewrite_query` / `chitchat` / `intent.py` 的 LLM 兜底层：查/写 `prompt_cache`。

### app/db/session.py
- 无 SQL 改动（`Base.metadata.create_all` 已覆盖新表 `long_term_memories`），只需保证 `app/db/models.py` 导入新模型。

### app/main.py
- `lifespan` 里在 `init_schema()` 之后，`await get_orchestrator().setup()` 提前完成 Redis Checkpointer / Neo4j schema 初始化，失败仅日志告警不中断启动（沿用现有 PG 初始化失败降级策略）。

### docker/docker-compose.yml
- 新增 `redis` 服务（`redis:7-alpine`，`ports: ["6379:6379"]`，volume `redis_data`）。
- 新增 `neo4j` 服务（`neo4j:5-community`，`NEO4J_AUTH: none`，`ports: ["7474:7474", "7687:7687"]`，volume `neo4j_data`）。
- `assistant` 服务环境变量补充 `REDIS_URL: redis://redis:6379/0`、`NEO4J_URI: bolt://neo4j:7687`，`depends_on` 增加 `redis`、`neo4j`。

## 不做的事
- 不引入新 secret 文件（Neo4j/Redis 均按内网单节点、无认证模式，对齐 Elasticsearch 现有决策）。
- 不改 `HybridRetriever`/`PgVectorStore` 内部实现，缓存只在 `graph.py` 调用点包装，检索通道本身保持纯净。

## 验证
- 本地：`redis-server`/`neo4j` 未启动时，全部新链路应静默降级为现状行为（进程内 dict memory + 无 checkpointer + 无长期记忆 + 无缓存），对话不报错。
- 启动依赖后：同一问题二次提问应命中 Retrieval/Prompt Cache（审计日志标记 `cache_hit`）；换 `session_id` 但同 `user_id` 提问，`build_context` 应带出跨会话长期记忆（Vector/Graph 通道）。