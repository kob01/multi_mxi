# 配置红线记录（每次改动前必读）

## 1. `docker/.env` — `OLLAMA_BASE_URL`（禁止修改）

```ini
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

- **规则**：此行保持原样，任何改动（代码重构、compose 调整、环境变量整理）都不得把它改回 `localhost` 或删除。
<!-- - **原因**：assistant 及各 MCP/A2A 服务运行在 Docker 容器内，容器里的 `localhost` 指向**容器自身**而不是宿主机；访问宿主机 Ollama 必须走 `host.docker.internal`。
- **事故记录（2026-09-17）**：该值一度配成 `http://localhost:11434`，容器内无法连到 Ollama，上传图片立即 502，报错 `All connection attempts failed`（0.1s 内失败，非超时）。改回 `host.docker.internal` 并重建容器后恢复。 -->
- **检查时机**：凡涉及 `docker/.env`、`app/config.py`、`docker/docker-compose.yml` environment 段的改动，或排查任何 Ollama 连接类报错时，先核对本条。

## 2. `docker/.env` — `PG_HOST`（容器内必须为服务名）

```ini
PG_HOST=postgres
PG_SSLMODE=disable
```

- **规则**：容器内 `PG_HOST` 必须指向 compose 服务名 `postgres`（不能是 `localhost`，也不能指回已下线的 MySQL 云主机 `47.116.208.170:3306`）；宿主机本地开发才用 `localhost`。
- **原因**：存储层已统一为 PostgreSQL（元数据 + pgvector），容器里的 `localhost` 指向容器自身，而 `postgres` 服务未映射到宿主机时走 `localhost` 必定连接失败。

## 3. PostgreSQL 密码与 pgvector 扩展

- 密码只来自 `PG_PASSWORD` 环境变量 / `.env` / `docker/secrets/pg_password.txt`（挂载为 `/run/secrets/pg_password`），**绝不写进代码、compose environment 或 git**。
- 启用知识库必须满两个条件：`documents/knowledge_chunks` 表存在、`vector` 扩展已装。扩展在 `docker/init/01_vector.sql` 由镜像超管首启安装；应用侧 `init_schema()` 仅兜底，云实例上应用账号无权限时会直接报错提示（不要改成静默忽略）。
- **检查时机**：改动 `app/db/session.py`、`app/db/models.py`（`embedding_dim`）、`docker/docker-compose.yml` 的 postgres 段，或排查 `type "vector" does not exist` / 启动时报缺 PG_PASSWORD 时，先核对本条。

