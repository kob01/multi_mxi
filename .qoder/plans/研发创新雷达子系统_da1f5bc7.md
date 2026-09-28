# 研发创新雷达 (R&D Innovation Scanner) 实施计划

## Summary

两条流水线 + 三种消费形态：
- **日扫描**（7×24）：`sources(arXiv 论文 / Europe PMC 专利 / RSS 行业动态 / local 目录) → 增量去重落 rd_scan_items → LLM 逐条轻量摘要`
- **周报告**（每周一 08:00 北京时间）：`聚合近 7 天 items → map-reduce LLM 提炼技术趋势/IP 空白 → Markdown 报告落 rd_radar_reports + data/reports 文件 → 复用 save_upload→ingest_confirmed 入 RAG KB + 后台 KG 建图 → 审计留痕`

消费形态：① Web 前端"创新雷达"页面（报告列表/详情/手动触发）；② `radar` MCP server（查询类工具）；③ `Radar_Agent` A2A 智能体（对话追问"本周创新雷达有什么发现"，周报同时被 KB 检索通道命中）。

架构约定（全项目一致）：总开关默认关闭、外部网络源异常静默降级绝不阻断对话、`try_redis` 锁降级为进程内标志、字段 snake_case、新表由 `init_schema()` 自动创建零迁移。

## 配置层（app/config.py + .env.example + docker/.env.example）

`Settings` 新增一段 `# ---------- 研发创新雷达 (R&D Scanner) ----------`，全部带默认值：

```python
scanner_enabled: bool = False        # 总开关: 关闭时调度循环不启动, 一切功能不可用
scanner_external_sources_enabled: bool = True  # 置 false 只用 local_dir 源(内网无外网时)
scan_interval_hours: int = 6         # 日扫描周期(粗粒度 tick)
weekly_report_day_of_week: int = 0   # 周一=0 (北京时间)
weekly_report_hour: int = 8
arxiv_categories: str = "cs.AI,cs.CL,cs.RO,cs.LG"
epc_patent_query: str = "(PATENT)"   # Europe PMC patents 源检索式, 可配置聚焦领域
scanner_topics: str = "具身智能,大模型推理,固态电池,自动驾驶感知"  # 逗号分隔订阅主题(RSS 源与报告聚焦共用)
arxiv_max_results: int = 50
epc_page_size: int = 100
rss_urls: str = ""                   # 逗号分隔 RSS 源; 空即跳过(行业报告/竞品动态通道)
local_scan_dir: str = "./data/radar_inbox"   # 投放 md/txt 的本地目录源(离线演示/人工情报)
scan_item_summary_max_chars: int = 1500
weekly_map_batch_size: int = 20      # map 阶段每批条数
weekly_top_n: int = 30               # 参与 reduce 的 Top-N 高信号 items
radar_scan_timeout: float = 30.0     # 单源 HTTP 超时(静默降级)
```

`.env.example` 同步补示例键（含注释：内网无外网时置 `SCANNER_EXTERNAL_SOURCES_ENABLED=false` 走 local 源）。

## 数据层（app/db/models.py，两张新表，init_schema 自动建）

```python
class RdScanItem(Base):          # __tablename__ = "rd_scan_items"
    id: BigInteger PK autoincr
    source: String(16)           # arxiv / epmc_patent / rss / local
    external_id: String(128)     # arxiv id / pmcid / url hash / 文件名+mtime hash
    # UniqueConstraint("source", "external_id")  ← 增量去重锚点
    title: String(512); summary: Text            # summary = LLM 摘要(失败留空, 不阻塞)
    url: String(512) = ""; published_at: DateTime(tz) nullable
    topics: JSON = list; signal_score: int = 0   # LLM 打的分发型 0-100
    kind: String(16)             # paper / patent / report / competitor
    status: String(16) = "new"   # new / summarized / failed
    created_at / scanned_at

class RdRadarReport(Base):       # __tablename__ = "rd_radar_reports"
    id: BigInteger PK autoincr
    period_start / period_end: Date              # 北京时间周期
    title: String(255)
    markdown_path: String(512)   # 正文以磁盘为准(与 report_artifacts 同哲学)
    doc_key: String(32) default ""  # KB 沉淀后的 documents.doc_key(可空=沉淀失败)
    stats: JSON                  # {items_total, by_source, trends_n, gaps_n}
    status: String(16)           # generated / kb_ingested / failed
    created_at
    # Index("ix_rd_radar_reports_created", "created_at")
```

## app/scanner/ 子系统（全新包，8 个文件）

### `app/scanner/models.py`
`@dataclass ScanItemDraft`（source/external_id/title/url/published_at/kind/raw_text）与 `RadarTopics` 解析辅助。

### `app/scanner/sources/__init__.py` — 可插拔注册表
```python
@runtime_checkable class ScanSource(Protocol):
    name: str; kind: str
    async def fetch(self, client: httpx.AsyncClient, since: datetime | None) -> list[ScanItemDraft]: ...
_REGISTRY: list[ScanSource]; def register(src); def active_sources(settings) -> list[ScanSource]
# active_sources: scanner_external_sources_enabled 决定是否收录网络源; local 源恒在
```

### `app/scanner/sources/arxiv.py`
`ArxivSource`：GET `http://export.arxiv.org/api/query`（search_query=`cat:{cats} AND submitted:[d0 TO d1]`, sortBy=submittedDate），`xml.etree.ElementTree` 解析 Atom（命名空间 `http://www.w3.org/2005/Atom`），**零新依赖**。限连：arXiv 要求 3s 间隔，源内部 `asyncio.sleep(3)` 单次批量即一次请求。

### `app/scanner/sources/europe_pmc.py`
`EuropePmcPatentSource`：GET `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=...&format=json&resultType=core&pageSize=...`，游标翻页至 `nextCursorMatchAvailable=false` 或达 `epc_page_size` 上限；专利条目 `pmcid` 作 external_id。

### `app/scanner/sources/rss.py`
`RssSource`：stdlib `xml.etree` 解析 RSS 2.0 `channel/item`（title/link/pubDate/description），`email.utils.parsedate_to_datetime` 解日期；`settings.rss_urls` 逗号拆分，空则整体跳过。承担"行业报告/竞品动态"通道。

### `app/scanner/sources/local.py`
`LocalDirSource`：扫 `local_scan_dir` 下 `*.md|*.txt`，external_id = `sha1(filename + mtime)`，读全文为 raw_text。**离线演示与内网环境的保底通道**。

### `app/scanner/store.py`（async DAO，构造零 I/O，模式对齐 `app/docs/service.py`）
```python
async def upsert_seen(drafts) -> list[ScanItemDraft]   # pginsert(...).on_conflict_do_nothing(index_elements=["source","external_id"]) 后回查新插入行(先查已存在 external_id 集合再过滤, 避免依赖 RETURNING)
async def mark_summarized(item_id, summary, topics, signal_score) -> None
async def pending_unsummarized(limit: int) -> list[RdScanItem]
async def items_in_range(start, end, min_signal: int = 0) -> list[RdScanItem]
async def last_report_period() -> tuple[date, date] | None   # 判定本周是否已出报(幂等)
async def save_report(...) -> int;  async def update_report_kb(doc_key, status)
async def list_reports(limit) / get_report(id)
async def acquire_scan_lock(ttl_seconds: int) -> bool
    # try_redis(lambda: redis.set("mxi:radar:scan_lock", token, nx=True, ex=ttl));
    # Redis 不可用时降级为模块级进程内标志, 绝不误抛
async def release_scan_lock() -> None
```
注意 PG `on_conflict_do_nothing` 须用 `sqlalchemy.dialects.postgresql.insert`（项目既有坑位记忆）。

### `app/scanner/analyzer.py`（LLM 成本控制核心）
```python
async def summarize_item(item) -> tuple[str, list[str], int]
    # 一次调用输出 JSON {summary, topics, signal_score}; 输入截断 scan_item_summary_max_chars;
    # 走 app/llm.get_chat_model(json_mode=True) + app/cache/prompt_cache.cached_llm_call; 失败返回 ("", [], 0) 由调用方标 status=failed
async def map_reduce_weekly(items: list[RdScanItem]) -> dict
    # 按 signal_score 排序取 weekly_top_n; 按 map_batch_size 分批 → 每批 LLM 产出"趋势线索/IP线索"列表;
    # reduce 一次 → 最终趋势 Top5、IP 空白 Top5; 全程失败降级为规则式统计段(源计数/标题清单), 报告照出
def render_report_markdown(period, stats, insights, source_hits) -> str
    # 结构化模板: 摘要 → 一、技术趋势 → 二、IP 空白与机会 → 三、专利/论文动向 → 四、竞品与行业信号 → 五、本期数据来源与附录链接
```

### `app/scanner/pipeline.py`
```python
async def run_daily_scan(trigger: str = "scheduler") -> dict
    # 全 try/except; 遍历 active_sources().fetch(共享 AsyncClient) → 单源失败仅 WARNING
    # → upsert_seen → 逐条 summarize_item+mark(每轮上限 100 条控成本) → audit log "scanner_daily_scan"
async def generate_weekly_report(force: bool = False) -> dict | None
    # last_report_period 幂等门禁; 聚合 items → map_reduce → render → rd_radar_reports 落库
    # → markdown 落 report_dir/radar-{ts}.md → _ingest_to_kb() → audit "scanner_weekly_report"
async def _ingest_to_kb(report_id, title, markdown) -> None
    # doc_key = sha1(title)前16; save_upload(f"radar洞察周报_{period}.md", bytes) 写 uploads 暂存
    # → ingest_confirmed(doc_key, filename, tags=["创新雷达"], uploader="rd_scanner")
    # → 成功回写 doc_key/status=kb_ingested; 失败仅 WARNING 置 status=generated(报告主体不受影响)
```
`save_upload`/`ingest_confirmed` 已内置 refresh_knowledge + KG 建图（`doc_kg_enabled` 时），**零额外接入**。

### `app/scanner/scheduler.py`
```python
def _now_cst() -> datetime  # timezone(timedelta(hours=8)), 对齐 UTC+8 口径
class RadarScheduler:
    def __init__(self): self._task: asyncio.Task | None = None
    async def start(self) -> None   # scanner_enabled=False 直接 return; 否则 create_task(self._loop())
    async def stop(self) -> None    # cancel + suppress
    async def _loop(self) -> None
        # while True: sleep(scan_interval_hours*3600) → try acquire_scan_lock 才执行:
        #   run_daily_scan(); 若 今天weekday==weekly_report_day_of_week 且 now.hour>=weekly_report_hour
        #   且 last_report_period 未覆盖本周 → generate_weekly_report()
        # 整轮 except Exception: logger.warning 继续循环 —— 永不因异常退出
```

### `app/scanner/router.py`（网关 REST，前缀 `/api/radar`）
- `GET /reports?limit=` 报告台账列表
- `GET /reports/{id}` 详情（读 markdown_path 文件内容一并返回）
- `GET /status` `{enabled, scheduler_alive, last_scan, last_report, sources: [...]}`
- `POST /scan`（query 参数 `operator`；`scanner_enabled=false` 时 400；调 `run_daily_scan("manual")`，请求内完成）
- `POST /report`（force=true 手动出周报）

## 接线改动（既有文件，均为增量小改）

| 文件 | 改动 |
|---|---|
| `app/main.py` | lifespan：KG schema 预热之后追加 `from app.scanner.scheduler import RadarScheduler; scanner = RadarScheduler(); await scanner.start()`（try/except 包裹）；yield 后 `await scanner.stop()`；`app.include_router(radar_router)`。约 12 行 |
| `app/config.py` | 新增 `scanner_mcp_url: str = "http://localhost:18007/mcp"`、`radar_agent_url: str = "http://localhost:9007"` + 上述 scanner 配置段 |
| `app/assistant/mcp_client.py` | servers dict 加 `"radar": {"url": settings.scanner_mcp_url, ...}` |
| `app/assistant/a2a_client.py` | `AGENT_URLS` 加 `"radar": lambda: get_settings().radar_agent_url` |
| `app/security/auth.py` | `MCP_WHITELIST`/`AGENT_WHITELIST` 各角色加 `"radar"`/`"radar_agent"`；新增 `RADAR_TOOL_WHITELIST`（4 个查询工具全员可见；`trigger_scan`/`run_trend_research` 仅管理角色）并登记进 `_DOMAIN_TOOL_WHITELISTS` |
| `app/assistant/intent.py` | `_AGENT_KEYWORDS` **在字典最前**插入 `"radar": ("创新雷达","技术趋势","专利","论文","竞品","前沿动态","技术情报","创新机会")`（必须排 analytics 前，否则"趋势/周报"被 analytics 抢占——该字典顺序即优先级）；`_RULES` 追加 `(re.compile(r"(本周|这周|最新).{0,6}创新雷达"), AGENT_DELEGATE)`；`_SEEDS` 加 `(TOOL_CALL,"radar")` 与 `(AGENT_DELEGATE,"radar")` 各 4-5 条种子话术 |
| `app/assistant/prompts.py` | `INTENT_PROMPT` 业务域说明加 `- radar: 技术趋势/专利/论文/竞品情报与创新机会洞察。`，target 枚举串补 `radar` |
| `docker/docker-compose.yml` | 新增 `radar-mcp`（18007:8007，pg secret，无 LLM 需求则不挂 deepseek secret——本 server 纯查库**不需要** LLM）与 `radar-agent`（9007:9007，镜像 contract-agent 段：ANALYTICS 同款 env + `SCANNER_MCP_URL: http://radar-mcp:8007/mcp` + `RADAR_AGENT_URL: http://radar-agent:9007`）；assistant 段 `environment` 追加 `SCANNER_ENABLED: ${SCANNER_ENABLED:-false}` 与 `SCANNER_EXTERNAL_SOURCES_ENABLED` 等服务端配置注入，`depends_on` 加 `radar-agent` |
| `scripts/dev_services.py` | `DEV_SERVICES` 列表追加 radar 两项（如该列表含 mcp/agent）；`_probe` 健康表补 18007/9007 |
| `REPO_MAP.md` | 完成后跑 `python scripts/gen_repo_map.py` 再生成 |
| `web-ui/src/router.js` / `components/AppHeader.vue` | 各 +1 行（radar 路由 + 导航项"创新雷达"） |

## MCP Server：`app/mcp_servers/scanner_server.py`（FastMCP, port 8007）

纯查询外壳（模式对齐 analytics_server 的"工具外壳"哲学，直接 import `app.scanner.store` 的 async DAO 用 `asyncio.run` 包裹不可行——FastMCP 工具支持 async def，直接 await）：
- `get_latest_weekly_report(limit_reports: int = 1)` → `{period, title, markdown, doc_key}`（对话追问的主数据源）
- `list_radar_reports(limit: int = 5)`
- `search_scan_items(query: str, source: str = "", since_days: int = 30, limit: int = 20)` → PG ILIKE + 时间过滤
- `get_trend_snapshot()` → 近 30 天各 source 计数 + Top 主题词频（纯 SQL/Python 统计，不调 LLM）
- `trigger_scan(operator: str)` → 直调 `run_daily_scan("mcp")`（进程内同库，写操作，白名单限管理角色）

## A2A Agent：`app/agents/radar_agent/`（三件套，完全镜像 contract_agent 模式）

- `agent_card.py`：`build_agent_card()`，name=`Radar_Agent`，domain 键 `radar`，skills：周报解读 / 技术趋势深挖 / IP 空白分析 / 竞品情报检索 / 手动扫描触发
- `executor.py`：`RadarAgent`（`MultiServerMCPClient({"radar": streamable_http})` + `filter_tools_for_role(role, "radar", tools)` + 角色分级 System Prompt：职责含"引用报告结论必须带数据来源条目；无本周报告时如实说明并建议触发扫描"）+ `RadarAgentExecutor` bridge（metadata 取可信身份、audit 留痕，逐行对齐 contract_agent/executor.py）
- `server.py`：`create_app()`，端口 9007

## 前端：`web-ui/src/views/RadarView.vue`

对齐 MemoryView 的写法（`fetch('/api/radar/...')` + Element Plus + `useEmployee`）：
- 顶部状态卡：开关状态 / 上次扫描 / 上次周报（`GET /api/radar/status`）
- 报告列表表格（标题/周期/状态/KB 沉淀标记/时间），行点击 → `el-drawer` 详情：`<pre>` 等宽滚动渲染 Markdown 正文（不新增 md 渲染依赖）+ "在对话中追问"按钮（跳转 chat 路由）
- 「立即扫描」「生成本周报告」按钮（POST，携带 operator），400/降级时 ElMessage 提示
- router.js/AppHeader 注册（见接线表）

## 脚本与依赖

- **零新增 Python 依赖**（httpx + xml.etree + email.utils + stdlib asyncio 全覆盖）；`pyproject.toml`/`requirements.txt` 不动
- 新增 `scripts/run_radar_scan.py`：CLI `python -m scripts.run_radar_scan [--days 7] [--no-report] [--report-only] [--force]`，直调 pipeline（compose 内手动补扫/回溯用），模式对齐 `scripts/ingest_knowledge.py`

## Test Plan

1. `python scripts/init_db.py` — 验证两张新表随 `init_schema` 自动创建
2. 离线单测源解析：往 `data/radar_inbox/` 放 2 个 md → `SCANNER_ENABLED=true SCANNER_EXTERNAL_SOURCES_ENABLED=false python -m scripts.run_radar_scan --days 1` → 检查 `rd_scan_items` 落库、重复跑同文件**零新增**（去重）
3. 在线源冒烟（外网可达时）：不带 `--no-external` 跑一次 arxiv+epmc，验证 Atom/JSON 解析与 `since` 增量
4. 周报链路：`--report-only` → `rd_radar_reports` 出记录、`data/reports/radar-*.md` 落盘、`documents` 表出现 doc_key 且 status=ready、`GET /api/docs` 可见
5. 对话追问：`POST /api/chat` "本周创新雷达有什么发现" → 意图命中 (agent_delegate, radar) 委派 Radar_Agent；"最近有什么新专利论文" 亦验证不被 analytics 抢占；员工角色调 `trigger_scan` 应返回权限拒绝
6. SSE 断线恢复不受新后台循环影响（回归 `scripts/test_sse_resume.py`）
7. 降级验证：`SCANNER_ENABLED=false` 时网关启动无任何 scanner 日志与任务；拔网/源 URL 填错 → 仅 WARNING，对话主链路正常
8. compose：`docker compose up -d --build radar-mcp radar-agent assistant`，宿主直跑网关 + 容器 agent 混合拓扑下验证委派连通（`_pin_card_url` 机制）

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 内网无外网（compose 注释证实 hf.co 不可达） | 总开关+外源开关双层默认安全；local 目录源永远可用作演示；单源异常仅 WARNING |
| arXiv/EBI 限流封禁 | 6h 周期 + arxiv 源内 3s 限速 + pageSize 上限；external_id 去重使重跑零成本 |
| "趋势/周报"关键词与 analytics 域冲突 | `_AGENT_KEYWORDS` 字典插入序即优先级：radar 置于最前且用"创新雷达/专利/竞品"等高区分度词，不用"周报/趋势"裸词 |
| Redis 不可用致锁失效 | try_redis 降级进程内标志（单网关进程无并发风险；多进程部署属远期，届时再上 DB advisory lock） |
| 周报 KB 沉淀失败 | 报告主体先落库落盘，沉淀失败仅降级 status=generated，页面仍可看全文（磁盘为正文事实源，与 report_artifacts 同哲学） |
| LLM 成本失控 | 每日摘要上限 100 条 + 截断 1500 字 + Prompt Cache + 周报 reduce 仅 Top-30；map 批 20 条/调用 |
| 调度循环异常退出致"7×24"失效 | _loop 全捕获永不退出；/api/radar/status 暴露 scheduler_alive 便于巡检；手动 trigger 兜底 |
| 网关 lifespan 启动变慢 | scheduler.start() 仅 create_task 无 I/O；扫描在首个 tick（默认 6h 后）才发生，不在启动路径 |

## Rejected Alternatives

1. **独立 scanner 容器跑调度**：多一个服务、宿主端口与 compose 依赖都要扩，且手动触发需跨进程 HTTP 回调 MCP；网关内 asyncio 后台任务与 docs/service 的 KG 后台建图先例一致，拓扑最简。
2. **APScheduler / celery / feedparser**：调度用 6h 粗粒度 tick + 北京时间判定足够；RSS 用 stdlib 解析足够（仅需 title/link/pubDate 四字段）；引第三方只增镜像体积与维护面。
3. **Google Patents (BigQuery) / Lens / Semantic Scholar API**：需 GCP 账号或 API key，违反"免费无密钥"决策。
4. **邮件/IM 主动推送**：项目无任何出站通道，属独立大功能；以"报告页 + KB 沉淀 + 对话追问 + 台账"等效达成"研发团队每周可获取"。
5. **周报正文直接存 PG**：与 report_artifacts 已确立的"文件为正文事实源、DB 记台账"分工冲突。
6. **仅接 MCP 不建 A2A Agent**：省 3 个文件与 intent 改动，但"对话追问深挖趋势"体验明显降级（单工具直答无法多步组合检索+总结）；且 intent/prompts/auth 的增量改动已被前四域验证为低风险模式。
7. **radar 情报设 manager-only 权限**：周报本身 visibility=public 入 KB，检索通道拦不住；在 MCP 层再收窄自相矛盾，故查询类全员、写触发类限管理。