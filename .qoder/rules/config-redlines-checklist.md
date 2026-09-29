---
trigger: model_decision
description: 动手改配置前的红线预检清单：app/config.py 的 env_file 双轨、docker/.env 容器服务名地址、compose 红线键字面量锁死、OLLAMA_BASE_URL、compose 宿主端口 18xxx、容器唯一验证环境、密钥只住 secrets 文件、.dockerignore 与打包拦截；含"静默降级/连不上/服务起不来/改了不生效"等症状到条目的定位映射。
---

# 配置红线速查（动手前逐条对照）

适用文件：`app/config.py`、`/.env`、`docker/.env`、`docker/docker-compose.yml`、`.dockerignore`、`scripts/package.py`。

本文件只是**预检清单**；展开正文与原因在 `.qoder/rules/config-env-tracks.md`（改上述文件时 glob 自动加载）。同一条规则不要在两处同时改。

## 八条不许做

1. 不许把 `docker/.env` 加进 `app/config.py` 的 `env_file`（现必须是 `(".env", ".env.local")`）。
2. 不许在 `docker/.env` 里把容器地址写成 `localhost`：`PG_HOST=postgres`、`MONGO_URL=mongodb://mongo:27017`，`ES_URL`/`TEI_RERANK_URL`/`REDIS_URL`/`NEO4J_URI` 同理。
3. 不许改或删 `OLLAMA_BASE_URL=http://host.docker.internal:11434`（唯一非服务名例外：Ollama 只跑在宿主机，不在 compose 内）。
4. 不许把 compose `ports:` 的宿主侧**默认值**改回 8000/8001/8002/7474/7687（Windows winnat 排除段会占用它们，且**段内容每次开机都变**，不要假设某端口永久可用）。neo4j 两个协议端口都要发：HTTP `17474→7474`（Browser，人工看图谱用）与 Bolt `17687→7687`（driver RPC，功能必需）；`7687` 已被本机新排除段 `7630-7729` 吞掉，靠 `docker/.env` 的 `NEO4J_BOLT_HOST_PORT=17687` 抬生效值，**不动 compose 默认值**。
5. 不许把密钥值写进任何 dotenv 或 compose `environment:` 字面量；真值只在 `docker/secrets/<name>.txt`。
6. 不许从 `.dockerignore` 删掉 dotenv 与 `docker/secrets/` 的排除行——这是双轨隔离与镜像不泄密共同的地基。
7. 不许把 compose `environment:` 里的红线键（地址/卷路径/tracing/ASSISTANT_PORT，名单见 `dev_services.py::COMPOSE_REDLINE_KEYS`）改回 `${VAR:-...}` 插值——那会让 docker/.env 与 shell 残留 export 能悄悄改容器地址（静默降级）；也不许在宿主机 export 同名地址变量后跑脚本。
8. 不许在宿主机直跑网关/后端服务做代码验证（容器是唯一验证环境，assistant 在 `DEV_SERVICES` 主轨里；见 `container-first-verification.md`）。

## 三条必须做

- 新增要进容器的配置键：必须同时落 `docker/.env` **和** `docker/.env.example`，否则容器内退到代码默认值；双轨同名调参键必须同值（只改一侧 = 容器与宿主脚本结论不可互复现）。
- 改 `app/docs/normalize.py::normalize_text()` 行为：必须同步递增 `NORMALIZER_VERSION`（见 `doc-normalizer-version.md`）。
- 动完配置跑 `uv run python -m scripts.dev_services env-check`（容器轨防覆盖专项）+ `uv run python -m scripts.dev_services check`；打包前跑 `uv run python -m scripts.package --check-only`。

## 症状 → 定位

| 症状                                                                    | 先看哪条                                                                                            |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| rerank 退 RRF / 会话记忆退内存 / BM25 无候选，且**无异常栈**            | 不许做 1（宿主轨串味，静默降级）                                                                    |
| 容器起来了但连不上；写正文报 `ServerSelectionTimeoutError`              | 不许做 2                                                                                            |
| embedding 超时、Ollama 连接类报错且无日志                               | 不许做 3                                                                                            |
| 部分服务卡 `Created` / neo4j `Exited(255)`，无应用日志                  | 不许做 4，跑 `netsh int ipv4 show excludedportrange protocol=tcp`                                   |
| **其他容器**报 `Failed to DNS resolve address <svc>:<port>`（Errno -2） | 同上：那服务因端口起不来，Docker 内置 DNS 就不再解析其服务名——根因不在报错那一层                    |
| 服务起来但鉴权失败，或日志/异常栈里出现口令                             | 不许做 5                                                                                            |
| 容器内地址与 `docker/.env` 写的不一样 / 有人改了 dotenv 地址"没反应"    | 不许做 7：红线键已字面量锁死，改 dotenv 不再生效应改 compose；跑 `env-check` 定位                   |
| 改了 `app/` 任何后端代码却不生效                                        | 容器跑的是镜像快照，`./scripts/dev.ps1 -Build` 重建（含 assistant，禁止改回宿主直跑验证，不许做 8） |
| 某层能力莫名失效，原因不明                                              | 跑 `dev_services check`，再读 `config-env-tracks.md` §1                                             |
