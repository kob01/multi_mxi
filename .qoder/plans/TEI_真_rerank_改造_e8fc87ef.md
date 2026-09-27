# TEI 真 rerank 改造

## 摘要

现状 `app/rag/reranker.py::OllamaReranker` 并非真重排：它调 Ollama `/api/embed` 取 `dengcao/bge-reranker-v2-m3` 的向量再算 cosine，而该 GGUF 在 Windows llama.cpp 上调用即崩（`exit 0xc0000409`），于是每次查询都撞超时后降级 RRF（约 28s/查询）。

改造为 **docker-compose 内的 TEI 服务**（HuggingFace Text Embeddings Inference）托管官方 `BAAI/bge-reranker-v2-m3`，走真 cross-encoder 的 `/rerank` 端点。按用户决策：**只留一个实现**（删除 Ollama 伪实现，不做 provider 抽象）、**先只实现不跑 MS MARCO 评测**。

零新增 Python 依赖（`httpx` 已在 `pyproject.toml`/`requirements.txt`）。

### 本次已实机核验的前提（方案据此定型）

| 事实 | 证据 | 对方案的影响 |
|---|---|---|
| Ollama 0.32.5 无 rerank API | `ollama.exe` 二进制含 `/api/embed`、`/api/generate`，不含 `/api/rerank`；HTTP 实测 `POST /api/rerank` → 404 | 不能指望 Ollama 侧修复 |
| GPU 为 RTX 4060 Laptop 8GB（Ada, sm_89） | `nvidia-smi` | TEI 镜像必须选 `89-1.9`（非 `:1.9`＝A100、非 `:86-1.9`＝A10） |
| Docker GPU 直通可用 | `docker info` 有 `nvidia` runtime；`docker run --gpus all ubuntu:22.04 nvidia-smi` 输出显卡成功 | compose 可用 GPU 设备预留 |
| `89-1.9` 镜像可达 | `docker manifest inspect` 对 `ghcr.io/...:89-1.9` 与 `ghcr.m.daocloud.io/...:89-1.9` 均返回 amd64 manifest | 默认走 daocloud 镜像前缀（与仓库其余镜像一致） |
| **huggingface.co 本机不可达**，`hf-mirror.com` 可达(200) | `Invoke-WebRequest` 超时 vs 200 | TEI 启动自下载权重必失败 → **必须宿主机预下载 + 本地目录加载** |
| TEI `/rerank` 契约 | 官方 `docs/openapi.json`：请求 `{query, texts[], raw_scores=false, return_text=false, truncate}`；响应为**裸数组** `[{index:int, score:float, text?}]`；424＝模型非单分类 | 解析不能按 Cohere 的 `{results:[{relevance_score}]}` 写 |
| TEI 支持 air-gapped 本地目录 | 官方 README `--model-id /data/Qwen3-Embedding-0.6B` | 权重挂载 + 路径式 model-id |
| rerank 前正文已就位 | `app/rag/retriever.py:118-121` 关键不变量 2（`attach_texts` 在 rerank 之前） | 真 cross-encoder 不会拿到 ES 通道空正文而被裁光 |

---

## 1. 权重预下载（新增，一次性）

TEI 不会从可达网络自下载，故权重由宿主机预落到仓库数据目录（不进 git）。

- 目标目录：`data/tei_models/BAAI/bge-reranker-v2-m3/`（须含 `config.json`、`model.safetensors`、`tokenizer.json`、`tokenizer_config.json`、`special_tokens_map.json`、`sentencepiece.bpe.model`）。
- 命令（PowerShell，用 `uvx` 免装到项目环境）：
  ```powershell
  $env:HF_ENDPOINT='https://hf-mirror.com'
  uvx --from "huggingface_hub[cli]" hf download BAAI/bge-reranker-v2-m3 `
    --local-dir data/tei_models/BAAI/bge-reranker-v2-m3 `
    --exclude "onnx/*" "assets/*" "long_context/*" "*.h5" "*.pt" "*.msgpack" "images/*"
  ```
  `--exclude` 必需：该仓库带 `onnx/`、`long_context/`（8k 变体）等副本，不排除会多下载数 GB；fp16 权重本身约 1.1GB。
- 兜底：若 `hf` CLI 不可用，`git lfs install; git clone https://hf-mirror.com/BAAI/bge-reranker-v2-m3 data/tei_models/BAAI/bge-reranker-v2-m3`。
- `.gitignore` 追加 `data/tei_models/`（紧跟现有 `data/pgdata/` 条目风格，带一行中文注释说明是 TEI 权重缓存）。

## 2. compose 新增 tei-rerank 服务

文件 `docker/docker-compose.yml`，插在 `neo4j` 与 `mineru` 之间（与其余基础设施服务同段），并在 `assistant.depends_on` 加 `tei-rerank: condition: service_started`。

```yaml
  # TEI: 真 cross-encoder 重排 (BAAI/bge-reranker-v2-m3 序列分类头), 取代原先借道
  # Ollama /api/embed 的伪 rerank —— 该 GGUF 在 Windows llama.cpp 上调用即崩。
  # 镜像按 GPU 架构选: sm_89(Ada/RTX40 系)=89-1.9; 无 GPU 退 cpu-1.9 (仅改 TEI_IMAGE)。
  # 权重由宿主机预下载挂载(内网 huggingface.co 不可达, 不能让容器自下载)。
  tei-rerank:
    image: ${TEI_IMAGE:-ghcr.m.daocloud.io/huggingface/text-embeddings-inference:89-1.9}
    command: >-
      --model-id /data/models/BAAI/bge-reranker-v2-m3
      --dtype float16 --max-batch-tokens 8192 --json-output
    environment:
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
    ports: ["${TEI_PORT:-8080}:80"]
    volumes:
      - ../data/tei_models:/data/models:ro
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    networks: [mxi]
```

- 不加 compose `healthcheck`：TEI 镜像未保证有 `curl`/`wget`，就绪判定统一放到应用侧 `GET /health`（见 §4）。
- 若本机 compose 版本不支持 `deploy.resources.devices`，改为服务级 `runtime: nvidia`（`docker info` 已注册该 runtime），二者只保留一个。
- `assistant` 的 environment 增加 `TEI_RERANK_URL: ${TEI_RERANK_URL:-http://tei-rerank:8080}`，并**删除** `RERANK_MODEL: ${RERANK_MODEL:-dengcao/bge-reranker-v2-m3}` 一行。

## 3. 配置项改造 `app/config.py`

- Ollama 段注释 `# Ollama (仅 embedding / rerank 仍走本地 Ollama)` → 改为"仅 embedding 走本地 Ollama；rerank 已迁 TEI"。
- **删除** `rerank_model`（仅 `config.py` 与 env 引用，代码无其他消费方）。
- RAG 段新增/改写（放在 `rerank_top_n` 之后）：
  ```python
  # Rerank 走 TEI 容器的真 cross-encoder (/rerank), 不再借道 Ollama /api/embed。
  # 容器内必须用服务名 tei-rerank; 宿主机直跑用 http://localhost:8080 (见 CONFIG_RULES)。
  tei_rerank_url: str = "http://localhost:8080"
  # 总超时 3s / 建连 0.5s: TEI 半死不能拖垮整条对话链路(超时报错即降级 RRF)。
  rerank_timeout: float = 3.0
  rerank_connect_timeout: float = 0.5
  # 单条候选送打分前的字符截断, 控 batch token 规模(bge-reranker-v2-m3 上限 8k token)。
  rerank_max_chars: int = 1024
  ```
- `rerank_enabled` 注释重写：删掉"Windows Ollama llama.cpp crashes on bge-reranker GGUF"这句过时描述，改为"TEI 不可用/超时 → 优雅降级 RRF 融合序"，保留该开关（作为 kill switch 与降级验证入口）。
- `retrieval_score_threshold` 注释补一句分数标度语义：真 rerank 分数是 TEI sigmoid 后的 **0~1 相关性**（相关块常 >0.9、噪声常 <0.05），不再是以前的 cosine 标度；默认值 **保持 0.4**，由 §6 冒烟核对后再决定是否微调。
- 不动 `retrieval_max_retries`、`rag_top_k`、`rerank_top_n`。

## 4. 重写 `app/rag/reranker.py`（单一实现）

整文件替换为 `TeiReranker`；**删除** `OllamaReranker`、`_cosine`、`math` 导入。必须遵守仓库既有规范（高并发 HTTP 复用 `AsyncClient` 连接池 + 批量单请求，禁止每次 new client）：

```python
"""Reranker backed by TEI (Text Embeddings Inference) /rerank.

真 cross-encoder: BAAI/bge-reranker-v2-m3 的序列分类头直接给 (query, text) 打分,
输出 sigmoid 后的 0~1 相关性 —— 取代原先借道 Ollama /api/embed 的伪 rerank
(该 GGUF 在 Windows llama.cpp 上调用即崩, 每查询都退化成 RRF)。
"""

_MAX_CANDIDATES = 64   # 单请求上限, 与 rag_top_k 同量级
```

要点（逐条落实）：

1. **共享 client**：模块级 `_client: httpx.AsyncClient | None`，`_get_client()` 惰性构造，`timeout=httpx.Timeout(settings.rerank_timeout, connect=settings.rerank_connect_timeout)`、`limits=httpx.Limits(max_connections=16, max_keepalive_connections=8)`；`base_url` 取 `settings.tei_rerank_url.rstrip('/')`。新增 `async def close_reranker_client()`（幂等，client 置 None），供 lifespan shutdown 调用 —— 与 `close_redis` / `close_mongo` 同构。
2. **类签名兼容既有调用点**：`async def rerank(self, query: str, chunks: Sequence[KnowledgeChunk], top_n: int) -> list[KnowledgeChunk]`（`retriever.py:126` 调用形式不变）。空 `chunks` 直接返回 `[]`，不发请求。
3. **一次批量请求**：`POST /rerank`，body `{"query": query, "texts": [...], "raw_scores": False, "return_text": False, "truncate": True}`；`truncate=True` 必传，否则超长文本 TEI 直接报错。候选按 `[:_MAX_CANDIDATES]` 截断，多余部分沿用原 RRF 序附在结果尾部（不静默丢弃排序信息）。
4. **文本构造**：`f"{c.title}\n{c.content}"[: settings.rerank_max_chars]`（保持旧行为，改为读配置）。
5. **响应解析要容错三种形态**：TEI v1.x 裸数组 `[{index,score,text}]`、老 TEI `[{id,score}]`、Cohere 风格 `{results:[{index,relevance_score}]}`。统一 `idx = item.get("index", item.get("id", i))`、`score = float(item.get("score", item.get("relevance_score", 0.0)))`；按 `index` 回填到对应 chunk 的 `model_copy(update={"score": score})`，再按分数降序取 `top_n`。**返回顺序必须由我们重排**，不依赖服务端是否已排序。
6. **错误暴露**：`resp.raise_for_status()` 前，非 2xx 时抛带响应体片段的异常，例如 `RuntimeError(f"TEI /rerank {resp.status_code}: {resp.text[:200]}")`；424/429/5xx 都能从日志直接看出原因（这是本次要解决的核心痛点：以前只看到笼统 500）。
7. **健康探测**：`async def health(self) -> bool`（`GET /health`，2xx→True，异常吞掉返回 False）与 `async def probe(self) -> bool`（用一对明显相关/无关的文本真打一次 `/rerank`，校验分数有序；供评测 harness 与启动日志用）。
8. 顶部 docstring 与函数注释用中文，风格对齐 `app/rag/bm25.py`、`app/cache/retrieval_cache.py`。

## 5. 接线：retriever、lifespan、harness

- `app/rag/retriever.py`
  - `from app.rag.reranker import get_reranker` 取代 `OllamaReranker` 导入；`self.reranker = get_reranker()`（与 `get_es_bm25()` 同构的进程级单例，避免每次构造 retriever 新建 client）。
  - 模块 docstring 里"真阈值只在 rerank 阶段"这段保持不变；补一句 rerank 分数标度为 0~1。
  - 现有 `except Exception -> 降级 RRF + logger.warning` 分支**保留原样**（真 rerank 失败仍不该拖垮对话）。
- `app/main.py::lifespan`
  - 在记忆层初始化之后加一段就绪日志：`ok = await get_reranker().probe()` → `logger.info("Rerank 后端就绪 (TEI %s)", settings.tei_rerank_url)` 或 `logger.warning("TEI rerank 不可用(%s), 本次进程检索将按查询降级 RRF 融合序", settings.tei_rerank_url)`。整体包 `try/except` 不阻断启动（与 PG/Mongo/记忆层同样的降级风格）。
  - `yield` 后追加 `await close_reranker_client()`。
- `scripts/msmarco_eval/harness.py`（不改就会 ImportError）
  - 导入改 `from app.rag.reranker import get_reranker`；`make_retriever()` 里 `retriever.reranker = get_reranker()`。
  - `rerank_available()` 去掉 `OllamaReranker()._embed(...)`，改 `return await get_reranker().probe()`；docstring 从"Windows GGUF 崩溃"改写为"TEI 服务未就绪/模型非单分类序列分类则不可用"。
- `scripts/msmarco_eval/__main__.py:99` 那句提示文案 `"(Ollama GGUF crash); measuring dense+BM25+RRF order instead."` → 改为 TEI 不可用的措辞。
- `scripts/msmarco_eval/chunking_ab.py:387` 报告脚注里"本机 Ollama bge-reranker 崩溃（Windows llama.cpp）"的既有历史结论文案**不改**（是已生成报告口径的事实陈述），新报告口径自然变化。

## 6. 环境变量与文档

- `.env`：删 `RERANK_MODEL=dengcao/bge-reranker-v2-m3`，Ollama 段注释改"仅 embedding"；RAG 段加 `TEI_RERANK_URL=http://localhost:8080`。
- `docker/.env`：同样删 `RERANK_MODEL`、改段注释；加 `TEI_RERANK_URL=http://tei-rerank:8080`、`TEI_IMAGE=...:89-1.9`、`TEI_PORT=8080`（供 compose 插值）。注意既有坑：compose `${VAR:-default}` 会被本文件的值覆盖，故这里只写确认无误的值。
- `CONFIG_RULES.md` 追加第 5 条（与 #2 PG_HOST / #3 MONGO_URL 同构）：`docker/.env` — `TEI_RERANK_URL` 容器内主机名必须为服务名 `tei-rerank`，宿主机直跑才用 `localhost`；并写明"检查时机：凡改动 compose 服务段 / config.py rerank 字段 / 排查 rerank 5xx 与连接拒绝时先核对"。
- `README.md`：
  - 第 26 行架构图的"模型底座: Ollama (bge-m3 / bge-reranker-v2-m3)"改为"Ollama (bge-m3 embedding) + TEI (bge-reranker-v2-m3 重排) + MinerU (图片OCR)"。
  - 第 148 行目录注释 `reranker.py # bge-reranker-v2-m3 重排` → 标注 TEI `/rerank`。
  - 快速开始第 185-191 行：删 `ollama pull dengcao/bge-reranker-v2-m3`，改为"重排走 compose 的 `tei-rerank` 服务；首次需按 §权重预下载把 `BAAI/bge-reranker-v2-m3` 落到 `data/tei_models/`（内网 huggingface.co 不可达，TEI 不会自下载）"；启动命令补 `docker compose -f docker/docker-compose.yml up -d tei-rerank` 的说明（`up -d --build` 已会带起它，此处仅为单独重启场景）。
  - 检索章节补一句：唯一相关性阈值作用于真 cross-encoder 的 0~1 分数。

## 7. 验证（不跑评测）

1. **TEI 单服务自检**：`docker compose -f docker/docker-compose.yml up -d tei-rerank` → 等权重加载完 → `curl http://localhost:8080/health` 返回 200；`curl -X POST http://localhost:8080/rerank -d '{"query":"报销单怎么提交","texts":["员工可在费控系统中提交报销单并上传发票","今天天气不错"],"return_text":false}'` → 断言第一条 `score` 明显高于第二条（>0.5 vs <0.1）。此步同时确认响应字段名与 §4 解析假设一致。
2. **新增 `scripts/smoke_rerank.py`**（对齐 `scripts/test_*.py` 风格，只读知识库、不改数据）：对 3-5 条真实 KB 查询（如"报销额度"、"技术滑行教程 SAJ"）跑 `HybridRetriever.retrieve`，打印 `score_mode`、每条 `chunk_id`+分数、阈值过滤前后条数；并额外跑一次"完全无关查询"验证会被裁空（阈值语义生效 = 能明确拒答）。
   - **必做**：跑之前先清一次 Retrieval Cache（`app/cache/retrieval_cache.py::invalidate_all()` 或 `redis-cli --scan --pattern "mxi:cache:retrieval:*"` 删除），否则可能读到旧 cosine 标度的缓存分数（TTL 300s，不主动清则 5 分钟后自愈）。
3. **降级路径回归**：`docker compose stop tei-rerank` 后再跑一次冒烟脚本 → 必须仍返回候选且 `score_mode == "rrf"`、日志出现一条 warning，且**不再出现 28s 级卡顿**（超时上限已压到 3s）。
4. **部署生效**：`docker compose -f docker/docker-compose.yml build assistant; docker compose -f docker/docker-compose.yml up -d assistant`（既有流程），看网关启动日志的 rerank 就绪行。
5. **阈值判定**：据步骤 2 打印的实际分数分布决定 `RETRIEVAL_SCORE_THRESHOLD` 是否微调（默认 0.4 不动；若真相关块普遍落在 0.2~0.4 之间才下调，若噪声普遍 >0.5 才上调），并在 `.env` 注释里记下实测值。

## 任务依赖

- §1 权重预下载 → §2 compose 服务 → §7.1 TEI 自检（后续所有验证的前置）。
- §3 config → §4 reranker 重写 → §5 接线（retriever/lifespan/harness）。
- §7.1 可与 §3-§5 并行（curl 直连 TEI，不经应用）。
- §7.2 冒烟依赖 §5 全部完成；§7.3 依赖 §7.2 通过。
- §6 文档与红线可与 §7 并行，但须在 §5 字段定稿后写（避免配置项名字写错）。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| GPU 镜像 `89-1.9` 起不来（驱动/CUDA 版本、flash-attn 精度） | 只改 `TEI_IMAGE=...:cpu-1.9` 一个变量即可切 CPU 路（8 候选约 0.5-1s/查询，仍可接受）；命令与代码零改动 |
| daocloud ghcr 镜像限流/缺 tag | 回退 `TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:89-1.9`（已实测 manifest 可解析） |
| 权重目录不全 → TEI 424/加载失败 | §7.1 先单独自检 TEI，再动应用；核对 `config.json`+`model.safetensors`+tokenizer 三件套齐全 |
| 响应字段与假设不符 | §4 解析对 `index`/`id`、`score`/`relevance_score`、裸数组/Cohere 包裹三态都兼容；§7.1 实测确认 |
| 分数标度变化把答案裁光（表现为"未检索到相关文档"） | 保留 RRF 降级；阈值语义只在 rerank 生效；§7.2 打印真实分数分布再定阈值；§7.3 反向验证无关查询确实被裁 |
| 旧缓存分数串标度 | 冒烟前 `invalidate_all()`，或等 `retrieval_cache_ttl=300s` 自愈 |
| 显存争用（bge-m3 host Ollama + TEI 同卡 8GB，另有已拉的 Qwen3-Reranker-4B 2.5GB） | TEI `--max-batch-tokens 8192` 限流；必要时 `ollama stop bge-m3` 之外不再常驻其他大模型；仍 OOM 则退 `cpu-1.9` |
| compose 不支持 `deploy.resources.devices` | 改服务级 `runtime: nvidia`（`docker info` 已注册该 runtime） |
| 8080 端口被占 | `TEI_PORT` 变量化，同步改两个 `.env` 的 `TEI_RERANK_URL` |
| 容器内把 URL 写成 `localhost` | 写进 `CONFIG_RULES.md` 第 5 条红线（与 PG_HOST/MONGO_URL 同构） |
| Ollama 旧 rerank 模型残留误导 | 不自动 `ollama rm`；在 README 注明 `dengcao/bge-reranker-v2-m3` 已不被使用，可自行删除 |

## 已否决的替代方案

- **升级/开启 Ollama 原生 `/api/rerank`**：实测本机 0.32.5 二进制不含该路由（`/api/embed` 等在、`api/rerank` 字符串缺失），HTTP 404；且需重启宿主机 Ollama 服务并依赖 experimental 开关，收益不确定。
- **裸跑 Ollama 自带的 `llama-server.exe --rerank`（复用已下载的 GGUF）**：字符串证据显示该 build 有 `--rerank/--reranking` 与 `/v1/rerank`，零下载零依赖最省事；但依赖 Ollama 私有二进制与 `lib/ollama/*.dll` 路径，Ollama 升级即失效，且仍需实测 llama.cpp 的 bert+分类头在 Windows 是否复现 `0xc0000409` —— 稳定性不足以作为生产路径。
- **进程内 `sentence-transformers`/`FlagEmbedding` CrossEncoder**：无新服务、可吃 4060 GPU；但要把 torch（CUDA 轮子 ~2.5GB）塞进生产镜像（当前 Dockerfile 是 `python:3.11-slim` + `uv sync --no-dev`），并需 `asyncio.to_thread` 防阻塞事件循环，模型加载还会拖慢冷启动 —— 代价远超收益。
- **在线 rerank API（硅基流动 / DashScope gte-rerank / Cohere）**：零本地资源，但要引第三方 key、企业知识库正文出网（与 `app/security` 的脱敏/审计取向冲突），且本机对外网连通性本身不稳（huggingface.co 已实测不可达），离线开发直接断链。
- **保留多 provider 抽象（ollama/tei/http 三选一）**：用户明确要求只留一个实现；且旧通道正是故障源，留着只会掩盖问题。
- **继续用 RRF 不修**：等价于放弃精排，`rerank_effective=false` 的评测口径与"唯一阈值裁剪"设计都会长期失真。
- **顺带把 `top_k`/`rerank_top_n` 一起调大**：与本次目标无关，会同时改变评测口径；如需调优另开任务。
