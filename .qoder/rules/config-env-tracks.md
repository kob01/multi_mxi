---
trigger: glob
glob:
  - app/config.py
  - docker/.env
  - docker/docker-compose.yml
  - /.env
  - docker/.env.example
  - .dockerignore
  - scripts/package.py
---

# 配置双轨与容器地址红线

本项目配置分宿主轨与容器轨，两轨值视角不同，禁止互串。改动上述文件前先核对本文件。

## 1. env_file 只读宿主轨，禁止加回 docker/.env

`app/config.py` 的 `model_config` 必须保持 `env_file=(".env", ".env.local")`。

- **禁止**把 `docker/.env` 加进这个元组。pydantic-settings 的多 dotenv 是**后者覆盖前者**，加回去会让宿主机进程（网关、评测脚本）拿到容器服务名。
- 后果不是报错而是**静默降级**：`getaddrinfo` 失败后 rerank 退 RRF、会话记忆退内存、图记忆被关闭、BM25 无候选，症状与"功能坏了"完全一致但没有异常栈。
- 容器侧配置只能走 compose 的两条注入通道：`env_file: [.env]`（即 `docker/.env`，整份以真实环境变量注入，放可调参数）+ `environment:`（锁死红线键，优先级高于前者）。
- 新增需要进容器的配置键，必须同时落到 `docker/.env` 与 `docker/.env.example`，否则容器里该键退到代码默认值（例：`LONG_TERM_MEMORY_ENABLED` 代码默认 `false`，只写宿主 `.env` 就会导致容器行为与宿主不一致）。
- 反向同样禁止：`docker/.env` 里不写 `localhost:18xxx` 这类宿主地址，宿主 `.env` 里不写 `tei-rerank`/`elasticsearch` 这类服务名。
- **双轨的地基是 `.dockerignore`**：它排除了 `.env`、`.env.local`、`.env.*.local`、`docker/.env`，所以容器文件系统里根本没有 dotenv，`app/config.py` 在容器内也就无从加载、无从覆盖。**从 `.dockerignore` 删掉这些行会立即让双轨保护失效**，症状就是上面那条静默降级。

## 2. 容器内地址必须用 compose 服务名

`docker/.env` 中以下为红线键，不得改成 `localhost`、不得删除：

| 键                  | 必须的值                                                         |
| ------------------- | ---------------------------------------------------------------- |
| `OLLAMA_BASE_URL`   | `http://host.docker.internal:11434`                              |
| `PG_HOST`           | `postgres`（配合 `PG_SSLMODE=disable`）                          |
| `MONGO_URL`         | `mongodb://mongo:27017`                                          |
| `ES_URL`            | `http://elasticsearch:9200`                                      |
| `TEI_RERANK_URL`    | `http://tei-rerank:8080`                                         |
| `REDIS_URL`         | `redis://redis:6379/0`                                           |
| `NEO4J_URI`         | `bolt://neo4j:7687`                                              |
| `LANGFUSE_BASE_URL` | `http://langfuse-web:3000`（compose 锁死，**不写 docker/.env**） |

- 容器里的 `localhost` 指向容器自身，未映射端口时必定连接失败；Mongo 侧表现为写正文时 `ServerSelectionTimeoutError`。
- `LANGFUSE_BASE_URL` 指向本 compose 内 `profile=langfuse` 的自建可观测栈（默认不启动）；该栈不起时 trace 上报会失败但**不影响对话**（`app/tracing.py` 逐层 try/except）。它与 `LANGFUSE_ENABLED` 不同：地址是红线，开关可调。
- `OLLAMA_BASE_URL` 是唯一例外：Ollama 不在 compose 内、只跑在宿主机，所以走 `host.docker.internal` 而不是服务名。
- 宿主机本地开发才用 `localhost`，且写在宿主 `.env` 里，不是这份。
- 元数据/向量层已是 PostgreSQL，不要指回任何 MySQL 地址（旧的 `47.116.208.170:3306` 云主机已下线）。

## 3. 存储分工：动之前先确认是哪一层

- **PostgreSQL + pgvector**：文档/业务元数据、窄标量、ACL、向量、子块 `chunk_text`（取代原 MySQL + Milvus）。
- **MongoDB**：正文外置存储——整篇 `raw_text`/`normalized_text`/`structure` 与父块全文。`mongo_enabled` 默认 `true`，compose 里 `mongo` 服务与 `mongo_data` 卷都在跑，**这是现行架构，不是遗留项**。
- **Elasticsearch**：BM25 稀疏检索的派生倒排（`content_tokens`）。

关掉 Mongo（`MONGO_ENABLED=false`）只把父块上下文降级为子块文本、不阻断对话，但入库路径会直接报错。

## 4. 宿主端口只用 `*_HOST_PORT` 插值

compose 的 `ports:` 里，assistant / hr-mcp / finance-mcp / neo4j 的**宿主侧**端口必须是插值形式，默认值不得改回 8000/8001/8002/7474/7687：

```yaml
assistant:     ports: ["${ASSISTANT_HOST_PORT:-18000}:8000"]
hr-mcp:        ports: ["${HR_MCP_HOST_PORT:-18001}:8001"]
finance-mcp:   ports: ["${FINANCE_MCP_HOST_PORT:-18002}:8002"]
# neo4j 两个协议端口各一份: HTTP=Browser(人工看), Bolt=driver RPC(图记忆/知识图谱必需)
neo4j:         ports: ["${NEO4J_HTTP_HOST_PORT:-17474}:7474", "${NEO4J_BOLT_HOST_PORT:-7687}:7687"]
# 其余服务保持 1:1 原值: postgres 5432 / ES 9200 / Redis 6379 / Mongo 27017 /
# mineru 8888 / tei-rerank 8080(${TEI_PORT}) / hr-agent 9001 / finance-agent 9002
```

- 原因：Windows `winnat`/Hyper-V 开机把 7254-7353、7354-7453、7454-7553、7554-7653、7956-8055 写进 TCP 端口排除段，段内端口即使无人监听也无法 bind；Docker 报 `ports are not available: ... bind: An attempt was made to access a socket in a way forbidden by its access permissions`，容器卡 `Created`（neo4j 为 `Exited(255)`）且**没有任何应用日志**——极易被误判为代码或镜像故障。
- 排除段内容**每次开机都会变**，绝不能假设某端口“一直能用”，也不要因此去改 compose 里的默认值。本机后续实测新增 `7630-7729`，已把 `7687` 括进去，neo4j 直接 `Exited(255)`；现用 `docker/.env` 的 `NEO4J_BOLT_HOST_PORT=17687` 抬起（默认值仍为 7687 不动）。判断口径：起不来先跑 `netsh int ipv4 show excludedportrange protocol=tcp`。
- 容器内监听端口与 compose 网络内的服务名地址（如 `bolt://neo4j:7687`）**永远不变**，`NEO4J_URI` 在 `docker/.env` 里是红线键不得改成宿主端口。
- 改这些默认值时必须同步：`/.env`、`docker/.env` 的 `HR_MCP_URL`/`FINANCE_MCP_URL`、`app/config.py` 对应默认值、`web-ui/vite.config.js` 代理目标、`scripts/dev.ps1`、`scripts/test_sse_resume.py`、`scripts/demo_reimburse.py` 的 `MXI_BASE`；改 `NEO4J_BOLT_HOST_PORT` 则同步 `/.env` 的 `NEO4J_URI` 与 `app/config.py` 的 `neo4j_uri` 默认值（两者都是宿主轨）。
- 排查“compose 里某几个服务起不来、其余正常”：先跑 `netsh int ipv4 show excludedportrange protocol=tcp`。
- 换机后若 `18xxx`/`17xxx` 也落进新机排除段：**用同名环境变量再挪一次**（`$env:ASSISTANT_HOST_PORT=20000; docker compose -f docker/docker-compose.yml up -d assistant`），**不要改 compose 里的默认值**（改了会连坐第 4 条的同步清单）。

## 5. 密钥只住 `docker/secrets/*.txt`

- `DEEPSEEK_API_KEY`/`LANGSMITH_API_KEY`/`PG_PASSWORD`/`MONGO_PASSWORD` 在 `/.env`、`docker/.env`、compose `environment:` 字面量中一律留空（**只写键名、值留空才是正确写法**，如 `DEEPSEEK_API_KEY=`；带值即算泄漏）；真值只放 `docker/secrets/<name>.txt`（已 gitignore）或以真实环境变量注入。原因：dotenv 会被 compose 当插值源并注入容器环境变量，配置一备份/截图/分享就连带泄密。
- `Settings` 里的口令/API Key 字段必须带 `Field(repr=False)`，否则 `repr(settings)` 会把密码写进启动日志与异常栈。
- 宿主机直跑依赖 `config.py` 的 `_SECRET_HOST_FALLBACK` 回退读 `docker/secrets/*.txt`；新增密钥字段要同步补 `_SECRET_FILES` 与 `_SECRET_HOST_FALLBACK` 两张表，不要改成硬编码或写进 dotenv。
- 容器侧 `LANGSMITH_TRACING` 恒为 `false`（代码默认值也是 `false`），避免对话内容上传云端。
- Langfuse 不同：它是 `profile=langfuse` 的**自建**栈，trace 不出容器网络，所以容器侧
  允许 `LANGFUSE_ENABLED=true`（仍默认关）。但项目密钥 `LANGFUSE_API_KEY`（单文件两行
  sk/pk）依旧只住 `docker/secrets/langfuse_api_key.txt`，dotenv 里留空；不用时该 secret
  文件可以是**空文件**（compose 要求它存在，空值等同未配置 → 自动降级为不上报）。

### 三道拦截链（改任一环都要回归）

1. **开发侧**：`uv run python -m scripts.dev_services check` 扫 dotenv 明文密钥（`SECRET_KEYS` 名单）与宿主轨串味。
2. **打包侧**：`uv run python -m scripts.package`（只跑前置校验用 `--check-only`）先做**源侧审计**——本机 `.env`/`docker/.env` 出现明文密钥即中止打包；再做**产物扫描**——正则命中且不在白名单则**删除该文件并中止**。复制阶段黑名单含 `.env`、`.env.local`、`docker/.env`、`data/.env`、`docker/secrets/*.txt`（该目录只有 `SECRETS_KEEP` 列出的 `README.md` 能进包）。
3. **镜像侧**：`.dockerignore` 保证密钥连 build context 都不进——`docker build` 会把整个 context 传给 daemon，命中文件即使没被 `COPY` 进镜像也已离开本机权限边界；生产镜像里的密钥只能来自运行时 `/run/secrets`。

## 6. 开发拓扑（容器 = 唯一验证环境，2026-09 起）

```powershell
./scripts/dev.ps1                              # 起 docker 全栈(含 assistant 容器网关) + 自检 + vite dev(:5173)
./scripts/dev.ps1 -Build                       # 改过 app/ 任何后端代码后必须重建镜像
uv run python -m scripts.dev_services check --gateway   # 只看对接结果(含探容器网关 /api/health)
uv run python -m scripts.dev_services env-check         # 容器轨防覆盖专项(无需 docker 在跑)
```

compose 里的全部服务（**assistant** / postgres / elasticsearch / redis / neo4j / mongo / tei-rerank / mineru / hr-mcp / finance-mcp / analytics-mcp / procurement-mcp / hr-agent / finance-agent / analyst-agent / contract-agent）都只在容器内跑；宿主机只跑 vite dev + Ollama。**宿主直跑网关做验证已禁止**（展开正文见 `container-first-verification.md`），旧约定“宿主网关 bind ASSISTANT_PORT、assistant 不进 dev 主轨”已作废：`DEV_SERVICES` 默认含 assistant，不得为让端口而 `compose stop assistant`。改过 `app/` 后 docker 侧跑的是镜像快照，不重建则请求被旧镜像接走，表现为“改了代码不生效”。

## 7. 容器轨防覆盖：红线键在 compose 里是字面量

历史上 `environment:` 写成 `ES_URL: ${ES_URL:-http://elasticsearch:9200}` 时，docker/.env 这个插值源、部署 shell 里残留的同名 export、直接 `docker compose up` 前的环境变量，都能悄悄改掉容器地址，后果仍是静默降级。现已收敛为：

- **红线键（地址/卷路径/tracing/容器内监听端口）在 compose `environment:` 里只允许字面量**：`ES_URL`、`TEI_RERANK_URL`、`MONGO_URL`、`OLLAMA_BASE_URL`、`DEEPSEEK_BASE_URL`、`UPLOAD_DIR`、`KNOWLEDGE_DIR`、`REPORT_DIR`、`AUDIT_LOG_PATH`、`ASSISTANT_PORT`、`PG_HOST`、`PG_SSLMODE`、各 `*_MCP_URL`/`*_AGENT_URL`、`LANGSMITH_TRACING`、`LANGFUSE_BASE_URL` 等（完整名单以
  `dev_services.py::COMPOSE_REDLINE_KEYS` 为准）。改这些键 = 改 compose 本身，
  dotenv 覆盖不动它们。
- **跨部署真可调的键仍允许 `${VAR:-服务名默认}` 插值**：`PG_PORT/PG_USER/PG_DATABASE`、`REDIS_URL`、`NEO4J_URI`、`ES_INDEX`、`DOC_KG_ENABLED`、`MINERU_*`、`EMBEDDING_DIM`、`LLM_MODEL/INTENT_MODEL`、各 `*_IMAGE`/`*_HOST_PORT`（这些默认值都是容器视角，被覆盖不会引入宿主串味）。
- **三道守护**：① `dev_services env-check` 扫 compose 源，红线键行含 `${` 或键从 compose 消失即 FAIL；② docker/.env 地址键出现 `localhost` 也 FAIL；③ 双轨同名业务参数漂移与 docker/.env ↔ `.env.example` 缺键各出一条 WARN（新增只落单侧的调参键 = 容器退代码默认值，两侧结论不可互相复现）。
- 真实环境变量仍优先于代码默认值（对不在 env_file 里的宿主脚本依然生效），所以**宿主跑脚本前不要 export 同名地址变量**，结论存疑时先跑 env-check + check。

## 自检

```
uv run python -m scripts.dev_services check
```

头两条即“宿主轨未混入容器地址”与“dotenv 里无明文密钥”。排查任何“某层能力莫名失效”先跑它，再回看本文件第 1 条。

按症状定向：

- **Ollama 连接类报错 / embedding 超时无日志** → 先核对 §2 的 `OLLAMA_BASE_URL` 是否被改成 `localhost` 或被删。
- **某层静默降级（rerank/记忆/BM25 无候选且无异常）** → §1；若容器内地址被人从 dotenv 改飘了 → §7 跑 `env-check`。
- **写正文报 `ServerSelectionTimeoutError`** → §2 的 `MONGO_URL`。
- **部分服务起不来、无应用日志** → §4 跑 `netsh` 查排除段。
- **改了不生效** → §6 重建镜像（含 assistant 自身，不再靠宿主热重载）。

`NORMALIZER_VERSION` 的升降规则见 `.qoder/rules/doc-normalizer-version.md`；A2A 地址覆盖见 `.qoder/rules/a2a-endpoint.md`。
