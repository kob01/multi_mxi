# Docker 对接与一键打包

## 目标拓扑（已确认）

宿主机只跑两个前台进程：`uvicorn app.main:app --reload`(:8000) 与 `pnpm dev`(:5173)。其余 compose 里有的服务全部由 docker 提供，宿主经已发布端口对接：

| 依赖 | 宿主地址(dev) | compose 服务 |
|---|---|---|
| PG | localhost:5432 | postgres |
| ES | localhost:9200 | elasticsearch |
| Redis | localhost:6379/0 | redis |
| Neo4j | bolt://localhost:7687 | neo4j |
| Mongo | mongodb://localhost:27017 | mongo |
| TEI rerank | http://localhost:8080 | tei-rerank |
| MinerU | http://localhost:8888 | mineru(profile) |
| HR/财务 MCP | http://localhost:8001|8002/mcp | hr-mcp / finance-mcp |
| HR/财务 Agent | http://localhost:9001|9002 | hr-agent / finance-agent |

唯一非 docker 依赖：Ollama(localhost:11434，`CONFIG_RULES` 第 1 条红线不动)。

## 一、配置单一来源（消除宿主/容器串味）

1. `app/config.py`：`env_file` 由 `(".env", "docker/.env")` 改为 `(".env", ".env.local")`（`.env.local` 已被 gitignore，供机器私有覆盖）。容器侧不再依赖 dotenv 文件，全部由 compose 注入真实环境变量。
2. `docker/docker-compose.yml`：给 5 个应用服务（assistant / hr-mcp / finance-mcp / hr-agent / finance-agent）加 `env_file: [.env]`（compose 项目目录 = `docker/`，即 `docker/.env`）。`environment:` 只保留必须锁死的红线键（服务名地址、`/data` 挂载路径、`${VAR:-default}` 插值项）——compose 中 `environment:` 优先级高于 `env_file:`，语义不变。
3. `docker/.env` 容器侧配置校正：`KNOWLEDGE_DIR=/data/knowledge`、`UPLOAD_DIR=/data/uploads`、`AUDIT_LOG_PATH=/data/logs/audit.jsonl`（当前 `./logs/...` 会落到 `/srv/logs` 不落卷；mcp 服务也一并补齐）、`LANGSMITH_TRACING=false`；`TEI_RERANK_URL=http://tei-rerank:8080` 可在此文件启用（串味根因已消除）。
4. `app/config.py`：`langsmith_tracing` 默认值 `"true"` 改为 `"false"`（现容器侧无该 env + secret 文件有 key → 实际在上云传 trace）；开发机上由根 `.env` 显式置 true。
5. 根 `.env`：MCP/A2A/MinerU 段保留 `localhost` 已发布端口（当前值已正确，仅补注释说明这些进程由 docker 提供、不再宿主起），并在文件头加一段「本文件=宿主视角，容器视角见 docker/.env」。
6. `app/assistant/a2a_client.py`：`_get_client()` 取到卡片后以配置地址为准 —— `card = card.model_copy(update={"url": base_url})`（SDK 的 `JsonRpcTransport` 用 `agent_card.url` 作 RPC 端点；覆盖后宿主→docker 与容器→容器两种拓扑都成立，且避免卡片 URL 被指向外部地址）。
7. 文档同步：`CONFIG_RULES.md` 第 5 条改写为「双轨 dotenv 已取消；宿主只读 `.env`，容器只读 compose env_file+environment」，并新增两条红线（A2A 卡片 URL 以配置为准；`LANGSMITH_TRACING` 容器必须 false）；`README.md`「本地开发」段重写为 3 步（起 docker 依赖 → 自检 → 宿主网关 + vite dev）。

## 二、密钥安全

1. 真实密钥只允许存在于 `docker/secrets/*.txt`（gitignore）与宿主环境变量；根 `.env` 的 `DEEPSEEK_API_KEY=` / `LANGSMITH_API_KEY=` 保留空值并把注释改成「禁止在此填值，写 `docker/secrets/<name>.txt`」。
2. 新增入库模板：`.env.example`、`docker/.env.example`（密钥字段一律空占位）、`docker/secrets/README.md`（给出 PowerShell 生成随机 PG 密码的命令，示例用占位符不含真实值）。
3. `app/config.py`：`pg_password` / `deepseek_api_key` / `langsmith_api_key` / `mongo_password` / `neo4j_password` 用 `Field(default=..., repr=False)`，防止 `print(settings)`、异常栈、trace 里带出密钥。
4. `.dockerignore` 补齐：`docker/.env`、`docker/secrets/`、`.env.local`、`logs/`、`reports/`、`data/uploads/`、`data/tei_models/`、`data/msmarco/`、`data/pgdata/`、`web-ui/node_modules/`、`.langgraph_api/`、`web/dist/`（镜像内前端由打包脚本显式带入时再放开该行的说明写进注释）。
5. 自检脚本内置「密钥泄漏检查」：`.env`/`docker/.env` 中 `DEEPSEEK_API_KEY`/`LANGSMITH_API_KEY`/`PG_PASSWORD` 出现非空值即报错，提示改用 secret 文件；日志与输出永不打印值或长度之外的内容。

## 三、依赖服务一键起 + 对接自检

新增 `scripts/dev_services.py`（`uv run python -m scripts.dev_services <action>`，Windows/Linux 通用，只读探测不改数据）：

- `up [--build]`：`docker compose -f docker/docker-compose.yml --profile mineru up -d postgres elasticsearch redis neo4j mongo tei-rerank mineru hr-mcp finance-mcp hr-agent finance-agent`；先校验 `docker/secrets/{pg_password,deepseek_api_key}.txt` 存在，缺失则打印创建命令并退出（不自动生成，避免与已有 `pg_data` 卷密码不一致）。
- `check [--strict]`：逐项探测并输出表格（服务 / 取自 Settings 的目标地址 / 状态 / 修复命令），探测点：PG `SELECT 1`、ES `/_cluster/health`、Redis `PING` + `JSON.SET`/`FT._LIST` 模块可用、Neo4j `verify_authentication`、Mongo `ping`、TEI `/health`、MinerU `/openapi.json`、`hr-mcp`/`finance-mcp` `/mcp` 端口探活、`hr-agent`/`finance-agent` `/.well-known/agent-card.json`、网关 `/health`。有必需项不可达退出码 1。
- `down`：`docker compose ... stop`（只停不删卷）。

新增 `scripts/dev.ps1`：`./scripts/dev.ps1` = `dev_services up` + `check` + `Start-Process` 拉起 uvicorn(:8000) 与 `pnpm dev`(:5173)（PID 写 `logs/dev.pid`，日志分别落 `logs/dev-gateway.log` / `logs/dev-web.log`）；`./scripts/dev.ps1 -Stop` 结束两个前台进程；`-SkipDocker` 跳过依赖启动。

## 四、前端 dev 模式

1. `web-ui/vite.config.js`：proxy target 改为 `process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'`；代理前缀除 `/api` 外补 `/health`；保留 `/api` 流式转发（vite http-proxy 默认不缓冲，禁用 dev server gzip 由后端 `StreamingResponse` 保证）。FE 全部用相对路径 `/api/...`（`ChatView/UploadView/MemoryView/GraphView` 已确认），无需改业务代码。
2. `app/main.py`：SPA 静态托管条件化 —— 现在只要 `web/dist/assets` 存在就挂 catch-all，`index.html` 缺失时 `FileResponse` 会 500。改为仅当 `web/dist/index.html` 存在才注册静态 + 回退；不存在时 `GET /` 返回一段指向 `http://localhost:5173` 的开发提示（`application/json`），`/api` 与 `/health` 路由不受影响。dev 模式不依赖 `web/dist`。
3. `README.md`：dev 入口写 `http://localhost:5173`（聊天）、`:5173/upload`、`:5173/graph`、`:5173/memory`；生产/打包后入口 `:8000`。

## 五、一键打包（产出部署目录，不含镜像）

新增 `scripts/package.py`（`uv run python -m scripts.package`），一条命令完成前后端一体交付物：

1. 前置校验：`uv`/`python`、`node`/`pnpm` 可用；`--check-only` 只跑校验。
2. 前端构建：`pnpm install --frozen-lockfile` + `pnpm build` → 产物落 `web/dist`（`--skip-frontend` 复用现有产物）。
3. 组装目录 `build/mxi-deploy-<pyproject版本>-<git短sha>-<UTC+8时间戳>/`：
   - 后端：`app/`、`scripts/`、`pyproject.toml`、`uv.lock`、`requirements.txt`、`langgraph.json`、`README.md`、`CONFIG_RULES.md`、`.dockerignore`
   - 前端产物：`web/dist/`（配合 `docker/Dockerfile` 已有的 `COPY web ./web`，目标机 `--build` 即得到前后端一体镜像）
   - 部署：`docker/`（`Dockerfile`、`docker-compose.yml`、`init/`、`mineru/Dockerfile`、`secrets/README.md`）
   - 语料：`data/knowledge/`（`--skip-knowledge` 可省）
   - 配置模板：`.env.example`、`docker/.env.example`（真实 `.env`、`docker/.env` 一律不打包）
   - `DEPLOY.md`（目标机步骤：建 secrets 文件 → 复制两个 `.example` → `docker compose -f docker/docker-compose.yml --profile mineru up -d --build` → `exec assistant python -m scripts.init_db` → `ingest_knowledge` → 访问 `:8000`）
   - `manifest.json`（版本、git sha、生成时间、文件相对路径 + sha256）
4. 复制黑名单（硬编码，优先级高于 include）：`.env`、`.env.local`、`docker/.env`、`docker/secrets/*.txt`、`.venv`、`node_modules`、`__pycache__`、`logs`、`reports`、`data/uploads`、`data/tei_models`、`data/msmarco`、`data/pgdata`、`.langgraph_api`、`.qoder`、`.trae`、`build`。
5. 密钥闸门：对输出目录全量文本文件跑正则扫描 —— `sk-[A-Za-z0-9]{16,}`、`-----BEGIN .*PRIVATE KEY-----`、`postgresql\+asyncpg://[^:/\s]+:[^@\s]+@`、`(api_key|password|secret|token)\s*[:=]\s*['\"]?[^\s'"]{12,}`；除 `.example`/`secrets/README.md` 中的空值与占位符白名单外命中即删除该文件并以非 0 退出报错（打印命中文件名+规则名，不打印命中内容）。
6. 产出压缩包 `build/<目录名>.zip`（`--tar` 出 `tar.gz`），打印路径、大小、sha256；`--strict-config` 时额外在包内跑 `docker compose -f docker/docker-compose.yml config` 验证目标机可渲染。

## 测试计划

1. 配置双轨：宿主 `uv run python -c "from app.config import get_settings as g;s=g();print(s.es_url,s.mongo_url,s.redis_url,s.neo4j_uri,s.tei_rerank_url,s.knowledge_dir)"` 全 localhost/宿主相对路径；`docker compose -f docker/docker-compose.yml exec assistant python -c "..."` 同一行输出全服务名 + `/data/...`。
2. 服务自检：`uv run python -m scripts.dev_services check` 全绿；`docker compose ... stop neo4j` 后重跑应报红并给出 `up -d neo4j`，退出码 1（覆盖当前 neo4j Exited、assistant/hr-mcp/finance-mcp Created 的空档）。
3. dev 全链路（浏览器 `http://localhost:5173`）：RAG 提问（走 docker PG 向量 + ES BM25 + TEI 重排）、文档上传（docker Mongo + ES）、记忆面板（docker Redis + Neo4j）、报销委派（docker hr-agent/finance-agent → docker mcp，验证 A2A 卡片 URL 覆盖生效）、图片上传走 docker mineru。
4. SSE 经 vite 代理：`MXI_BASE=http://127.0.0.1:5173 uv run python scripts/test_sse_resume.py` 与 `scripts/demo_reimburse.py`（指向宿主 :8000）均通过。
5. 密钥安全：临时往 `docker/.env` 写一个假 `DEEPSEEK_API_KEY=sk-<32位>` → `check` 与 `package` 都必须拦截；打包产物内 `Get-ChildItem -Recurse` 无 `.env`/`secrets/*.txt`；manifest sha256 抽查 3 个文件一致。
6. 目标机语义验证（本机模拟）：在 `build/mxi-deploy-*/` 里复制两个 `.example`、建 secrets 文件后 `docker compose ... config --quiet` 通过，`docker compose ... build assistant` 能成功（前端 dist 已随包）。

## 假设与取舍

- 不引入新工具链：脚本一律 Python + 一个 PowerShell 包装；不加 Makefile/nox。
- docker 里的 mcp/agent 代码是镜像快照，改这部分需 `dev_services up --build`（脚本已支持）。
- 按选择「只产出部署目录，不含镜像」，目标机自行 `compose build`；离线镜像 tar 导出留作后续可选项。
- 不改动应用鉴权模型（`/api` 仍按 `user_id`/`operator` 传参）：本次「密钥安全」只覆盖凭据存放、注入、泄漏面。
- 评测隔离库（`mxi_msmarco_eval`）逻辑不变，只是宿主地址解析恢复正确后自动对接 docker PG。