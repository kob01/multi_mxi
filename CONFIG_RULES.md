# 配置红线记录（每次改动前必读）

## 1. `docker/.env` — `OLLAMA_BASE_URL`（禁止修改）

```ini
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

- **规则**：此行保持原样，任何改动（代码重构、compose 调整、环境变量整理）都不得把它改回 `localhost` 或删除。

- **检查时机**：凡涉及 `docker/.env`、`app/config.py`、`docker/docker-compose.yml` environment 段的改动，或排查任何 Ollama 连接类报错时，先核对本条。

## 2. `docker/.env` — `PG_HOST`（容器内必须为服务名）

```ini
PG_HOST=postgres
PG_SSLMODE=disable
```

- **规则**：容器内 `PG_HOST` 必须指向 compose 服务名 `postgres`（不能是 `localhost`，也不能指回已下线的 MySQL 云主机 `47.116.208.170:3306`）；宿主机本地开发才用 `localhost`。
- **原因**：存储层已统一为 PostgreSQL（元数据 + pgvector），容器里的 `localhost` 指向容器自身，而 `postgres` 服务未映射到宿主机时走 `localhost` 必定连接失败。

