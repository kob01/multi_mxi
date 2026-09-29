# 配置红线（索引）

红线正文已全部迁至 `.qoder/rules/`，由 Qoder 按 glob / 描述自动加载。**本文件不写规则内容，只做条号路由**——同一条规则在两处并存必然漂移，改内容请只改 `.qoder/rules/` 那一份。

## 条号 → 权威正文

历史引用（`app/config.py` 文档串、`docker/docker-compose.yml` 头注释里的「见 CONFIG_RULES.md 第 5 条」）按本表解析，条号不得变更。

| 条号 | 主题                                                      | 权威正文                          |
| ---- | --------------------------------------------------------- | --------------------------------- |
| 1    | `OLLAMA_BASE_URL` 禁改（`host.docker.internal`）          | `config-env-tracks.md` §2         |
| 2    | 容器内 `PG_HOST=postgres`                                 | `config-env-tracks.md` §2         |
| 3    | 容器内 `MONGO_URL=mongodb://mongo:27017`                  | `config-env-tracks.md` §2、§3     |
| 4    | `NORMALIZER_VERSION` 与 `normalize_text()` 同升降         | `doc-normalizer-version.md`       |
| 5    | 配置双轨：`env_file` 只读宿主轨，禁止加回 `docker/.env`   | `config-env-tracks.md` §1         |
| 6    | 宿主端口走 `*_HOST_PORT`（18000/18001/18002/17474/17687） | `config-env-tracks.md` §4         |
| 7    | A2A 卡片通告地址不作路由依据                              | `a2a-endpoint.md`                 |
| 8    | 密钥只住 `docker/secrets/*.txt`，dotenv 留空              | `config-env-tracks.md` §5         |
| 9    | 开发拓扑：容器 = 唯一验证环境，宿主只跑 vite dev + Ollama | `config-env-tracks.md` §6         |
| 10   | 容器轨防覆盖：compose 红线键字面量锁死 + env-check        | `config-env-tracks.md` §7         |
| 11   | 禁止宿主直跑网关验证，改代码必重建镜像                    | `container-first-verification.md` |
| —    | 存储三层分工（PG / ES / Mongo）                           | `config-env-tracks.md` §3         |
| —    | 症状 → 红线定位速查（动手前看）                           | `config-redlines-checklist.md`    |

规则文件均在 `d:\ai\mxi\.qoder\rules\`。

## 新增红线怎么放

优先落到已有 glob 文件对应小节，**不要新开 always-on 规则**（每轮常驻很贵）。只有当某条约束必须在"还没读任何文件之前"就可见时，才加 `trigger: model_decision` + 一句高关键词密度的 `description`（这种方式每轮只注入路径与描述，零正文成本）。

## 已作废的判断（防止再被误改回来）

- **「Mongo 已下线」是错的。** 「统一存储 PostgreSQL」只覆盖元数据与向量层（取代原 MySQL + Milvus），正文外置仍在 Mongo。现行是三层分工，见 `config-env-tracks.md` §3；不要因为看到 `MONGO_URL` 就当作遗留配置清掉。
- **「`env_file` 顺序要调对」已过时。** 历史写法 `(".env", "docker/.env")` 已删除，约束现在是「不得再加回去」。
- 2026-09 之前本文件是唯一事实源；此后任何只写进本文件的"规则"都不会被模型看到。

## 自检

```
uv run python -m scripts.dev_services env-check
uv run python -m scripts.dev_services check
uv run python -m scripts.package --check-only
```
