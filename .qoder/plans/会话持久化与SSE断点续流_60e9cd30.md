# 会话持久化 + 思考模式 + SSE 断点续流

## 总体设计

- 新增 SSE 流式链路：`POST /api/chat/stream` 启动一次运行（run），图在后台任务中执行，事件写入进程内 **StreamHub 缓冲区**（按 `run_id` 索引、事件带自增 id）；客户端通过 `GET /api/chat/stream/{run_id}` + `Last-Event-ID` 头随时重连，从断点重放并续流。页面刷新后凭 `localStorage` 中的 `run_id` 恢复。
- 会话记录落 PostgreSQL：新表 `chat_sessions` / `chat_messages`，`persist_memory` 节点同时写库；thinking 内容一并存 JSON 列。
- 思考模式：deepseek-flash 默认开启思考，通过 `extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": ...}` 显式控制；`reasoning_content` 以独立 SSE 事件下发，前端可折叠展示。Ollama 模型走 `think` 参数，非流式模型自动降级为无思考。

## 后端改动

### 1. 配置（app/config.py、.env、docker/.env）
- `Settings` 新增：`llm_thinking_enabled: bool = True`（全局默认）、`llm_reasoning_effort: str = "high"`（high/max，deepseek 取值）、`stream_buffer_ttl: int = 600`（run 结束后缓冲区保留秒数）。
- `.env` / `docker/.env` 补对应注释项。

### 2. LLM 工厂（app/llm.py）
- `get_chat_model(..., thinking: bool | None = None)`：DeepSeek 分支在非 json_mode 时注入 `extra_body = {"thinking": {"type": "enabled"/"disabled"}, "reasoning_effort": settings.llm_reasoning_effort}`；json_mode 保持现有 disabled 逻辑不动。
- 新增辅助函数从消息对象提取思考内容：优先 `msg.additional_kwargs["reasoning_content"]`（ChatOpenAI 透传），Ollama 取 `thinking` 字段；取不到返回空串（优雅降级）。

### 3. 会话持久化（app/db/models.py、新增 app/chat_store.py）
- ORM 新增 `ChatSession`（`chat_sessions`：id=client session_id、user_id、role、department、title=首条用户消息前 30 字、created_at、updated_at）与 `ChatMessage`（`chat_messages`：id 自增、session_id 索引、trace_id、role(user/assistant)、content TEXT、thinking TEXT nullable、route、target、intent、docs_meta JSON、created_at）。无需手写迁移，`init_schema` 的 `create_all` 自动建表。
- 新增 `app/chat_store.py`：异步 DAO —— `save_turn(...)`（upsert session + 插两条消息，DB 不可用时记 warning 不抛）、`list_sessions(user_id, limit)`、`get_messages(session_id)`、`delete_session(session_id)`。
- [memory.py](file:///d:/ai/mxi/app/assistant/memory.py) 继续负责 prompt 上下文窗口，进程重启后由 chat_store 记录按需重建（在 `MemoryStore.get` 首次未命中且 DB 有该会话记录时回填 turns，保持简单）。

### 4. StreamHub（新增 app/assistant/stream.py）
- `RunBuffer`：`events: list[tuple[int, dict]]`、`done: bool`、`subscriber` 条件通知；`append(event)` 返回自增 id；`iterate(from_id)` 异步生成器 = 先重放历史、再等待新事件、done 后结束。
- `StreamHub`（进程内单例）：`create(run_id)` / `get(run_id)` / `append(run_id, event)`；run 完成后按 `stream_buffer_ttl` 定时清理。事件即 SSE `data`（JSON），每条带 `id`。
- 事件协议：`status`（节点进度：rewriting / retrieving / generating…）、`think`（思考增量 {"delta": ...}）、`token`（回答增量）、`result`（最终 route/intent/docs_meta/trace_id/message_id）、`done`、`error`。

### 5. Graph 流式与思考透出（app/assistant/graph.py、prompts.py 按需）
- `AssistantState` 与 `ChatRequest` 透传 `run_id`、`thinking`（request 字段见第 6 点）。
- 新增共用协程 `_astream_answer(llm, prompt, sink, run_id)`：`astream` 迭代，逐 chunk 判定 `reasoning_content` 与正文，分别经 StreamHub 发 `think` / `token` 事件，返回完整回答文本。
- 改造 `kb_generate`、`chitchat`：thinking 开启时走 `_astream_answer`（流式 + 思考透出），关闭或模型不支持时回退现有一次性 `ainvoke` 路径（此时不发 think/token，仅靠 status + result 事件），行为兼容 `/api/chat` 旧端点。
- `tool_execute`、`agent_delegate`、`kb_retrieve`、`kb_requery` 不逐 token 流式，只在关键节点 `await hub.append(...)` 发 `status` 事件（前端展示"正在调用工具…"等）。
- `persist_memory`：调用 `chat_store.save_turn`（含 thinking 文本、masked 后内容、docs_meta），并把 DB `message_id` 通过 `result` 事件带回。
- 新增 `handle_stream(req) -> run_id`：生成 run_id（uuid）、创建缓冲区、`asyncio.create_task` 跑 `_graph.ainvoke`，任务异常时发 `error` + `done`；返回 run_id 供路由先下发给客户端。

### 6. API（app/schemas.py、app/assistant/router.py）
- `ChatRequest` 新增 `thinking: bool | None = None`（None 时取全局默认）。
- 新增端点：
  - `POST /api/chat/stream`：body 为 ChatRequest；SSE 响应头先建 run，然后直接在本连接上 `iterate(0)`（合并为一次请求）。
  - `GET /api/chat/stream/{run_id}`：读取请求头 `Last-Event-ID`（缺省 0）重放续流；run 不存在（已过期/服务重启）返回 404，前端降级为拉取历史。
  - `GET /api/sessions?user_id=...`、`GET /api/sessions/{session_id}/messages`、`DELETE /api/sessions/{session_id}`。
- SSE 用 `StreamingResponse(media_type="text/event-stream")`，每帧 `id: N\nevent: message\ndata: {...}\n\n`，附 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 保留 `POST /api/chat` 非流式端点不变（评测脚本等仍可用）。

## 前端改动（web-ui/src/views/ChatView.vue）

- sessionId 持久化：`localStorage` 存 `mxi_session_id`（复用现有 `web-xxxx` 格式），刷新/重进页面不再丢失会话；onMounted 时 `GET /api/sessions/{id}/messages` 回填历史消息（含 thinking 折叠块、cites、route 标签）。
- 发送改为流式：`fetch('/api/chat/stream', {method:'POST', ...})` + ReadableStream 手工解析 SSE 帧（EventSource 不支持 POST）。每收到一帧记录 `id` 到 `localStorage: mxi_last_event_id + mxi_active_run_id`；`token` 事件增量渲染、`think` 事件写入当前气泡的思考区。
- 断点续传：onMounted 检测存在未完成 run（有 `mxi_active_run_id` 且最后一条助手消息未落库/未收到 done）时，`fetch('/api/chat/stream/{run_id}', {headers:{'Last-Event-ID': lastId}})` 重连续流；404 则清除 run_id 并改拉历史消息。done/done+result 收到后清除 `mxi_active_run_id`。
- 思考模式开关：输入区加 `el-switch`（"深度思考"），默认取后端配置接口下发或初始为开；请求 body 带 `thinking` 字段。气泡内用 `<el-collapse>`/折叠 div 展示"思考过程"（流式中默认展开、结束后收起），样式与现有 cites footer 一致。
- 完成后执行 `pnpm build`（输出 web/dist，遵循现有构建方式）。

## 测试计划

- 后端手动验证：`uvicorn app.main:app` 起服务；curl `/api/chat/stream` 观察 think/token/result/done 事件与 `id:` 字段；中途 Ctrl+C 断开后 `curl -H "Last-Event-ID: N" /api/chat/stream/{run_id}` 验证重放+续流；run 完成刷新后查 `/api/sessions/{id}/messages` 验证落库（含 thinking）。
- 前端验证：`pnpm dev` 下发消息看流式与思考折叠；生成中刷新页面验证历史回填 + 续流；开关思考模式对比行为；DB 不可用时聊天仍可用（持久化静默降级）。
- 回归：`POST /api/chat` 旧端点、LangGraph Studio（get_graph）不受影响。

## 假设与边界

- StreamHub 为进程内缓冲（与现有 MemoryStore 同定位），单 uvicorn worker 部署有效；服务重启后进行中/已过期的 run 不可续流，前端降级为读已持久化历史（该轮答案因未跑完不存在，属可接受损失）。
- 思考内容仅 deepseek-flash/reasoner 真实产生；Ollama 模型无 reasoning_content 时 think 事件自然缺省，前端不显示思考区。
- 会话接口暂未做鉴权，与现有 `/api/docs` 风格一致（user_id 由前端传入）。