# web\_search / web\_fetch + docgen —— 纯进程内 tool 版（含 PDF）

## 摘要（Summary）

- 两组能力都做成 **进程内** **`@tool`**（LangChain `tool`，与 [lookup\_employee\_by\_name](file:///d:/ai/mxi/app/agents/common_tools.py#L20) 同源同构），放新包 `app/tools/`。**不新增 MCP server / 容器 / 端口 / compose service**。
- 落点归属：`common_tools.py` 文档定位就是"跨域基础能力"，web 检索/抓取、文档生成都属此类；因此进程内 tool 是**有先例且更贴合现状**的做法，运维面比 MCP 版小一大截。
- 路由仍需扩：时效类/"生成文件"类问题今天被分到 `knowledge_qa`/`chitchat`，进不到工具循环——无论形态如何都要在 [intent.py](file:///d:/ai/mxi/app/assistant/intent.py)/INTENT\_PROMPT 扩出 `web`/`docgen` 两个 `tool_call` 目标，并在 [tool\_execute](file:///d:/ai/mxi/app/assistant/graph.py#L641-L705) 里为"非 MCP 能力域"加一个**小分支**取进程内工具集（跳过 MCP 连接池与 MCP 权限校验）。
- 缓存沿用现成机制：`search_web` 命名命中 [\_CACHEABLE\_PREFIXES](file:///d:/ai/mxi/app/cache/tool_cache.py#L33) → 被 [wrap\_tools\_for\_cache](file:///d:/ai/mxi/app/cache/tool_cache.py#L83) 自动包 Redis（对任何 `BaseTool` 都生效）。`fetch_url`/`generate_*` 命名不命中前缀 → 有意不缓存。
- docgen 生成是同步 CPU/IO：统一用 `await asyncio.to_thread(...)` 卸载，避免阻塞网关事件循环（这是纯 tool 形态唯一实质代价，已接受；过大再迁 worker/MCP）。
- 下载链接仍要新增网关路由 `GET /api/files/{token}/{name}`（现状无下载端点，且 SPA 通配会吞）。因 tools 与路由同进程、同 `UPLOAD_DIR`，**不再有跨容器卷/路径不一致问题**。
- 依赖仅加 3 个纯 Python/轮子包：`reportlab`（PDF，中文走内置 `UnicodeCIDFont('STSong-Light')`，**不打包字体文件**）、`ddgs`（免密默认检索）、`beautifulsoup4`（HTML→文本，标准库解析器）。同步 `pyproject.toml`+`requirements.txt` 并 **重生成** **`uv.lock`**（镜像 `uv sync --frozen`）。

分 A–F 六组，含依赖先后与可分期标注。相对上一版：C/E 从"新 MCP server"改为"新 tool 模块 + 能力注册表"，F 去掉 compose/mcp\_client/ACL-MCP/dev\_services 服务改动。

***

## A. 依赖与锁文件（必须先做）

**A1.** [pyproject.toml](file:///d:/ai/mxi/pyproject.toml) `dependencies` 与 [requirements.txt](file:///d:/ai/mxi/requirements.txt) 两处同步新增：

- `reportlab>=4.1`（PDF，内置 CJK CID 字体）
- `ddgs>=9.0`（默认检索 provider，免密）
- `beautifulsoup4>=4.12`（`fetch_url` 正文抽取，stdlib `html.parser`，不引 `lxml`）
- Tavily/Serper 走 `httpx` REST 直连（已有），**不加 SDK**。
- docx/xlsx/pptx 已由 `python-docx`/`openpyxl`/`python-pptx` 覆盖，零新增。

**A2.** `uv lock` 更新 [uv.lock](file:///d:/ai/mxi/uv.lock) → `uv sync` 本地验证。⚠️ 红线：[Dockerfile](file:///d:/ai/mxi/docker/Dockerfile#L25-L26) 用 `uv sync --frozen`，锁不同步镜像构建直接失败。

***

## B. 配置项（比 MCP 版更少；去掉 `*_MCP_URL`）

**B1.** [app/config.py](file:///d:/ai/mxi/app/config.py) 新增字段：

- web：`web_search_provider: str = "ddgs"`（`ddgs|tavily|serper`）、`web_search_max_results: int = 5`、`web_search_timeout: float = 10.0`、`web_search_cache_ttl: int = 300`
- 检索密钥（可选，默认 ddgs 免密）：`tavily_api_key`/`serper_api_key` 设 `Field(default="", repr=False)`，加入 [\_SECRET\_FILES](file:///d:/ai/mxi/app/config.py#L12-L17)/[\_SECRET\_HOST\_FALLBACK](file:///d:/ai/mxi/app/config.py#L18-L23) 与 [\_read\_from\_secret\_file 校验器字段列表](file:///d:/ai/mxi/app/config.py#L255-L272)。
- web\_fetch：`web_fetch_allowlist: str = ""`、`web_fetch_timeout: float = 15.0`、`web_fetch_max_bytes: int = 2_000_000`、`web_fetch_max_chars: int = 8000`、`web_fetch_max_redirects: int = 3`
- docgen：`docgen_max_bytes: int = 20_000_000`、`docgen_retention_hours: int = 24`
- `public_base_url: str = ""`（下载链接前缀；空则用请求 host 兜底）
- **红线**：`env_file` 保持 `(".env", ".env.local")`，**不加** `docker/.env`。

**B2/B3.** 宿主 [.env.example](file:///d:/ai/mxi/.env.example) + 容器 `docker/.env.example` + 真实 `docker/.env` 同步补上述 `WEB_*/DOCGEN_*/PUBLIC_BASE_URL`（`TAVILY_API_KEY=`/`SERPER_API_KEY=` 留空，注释指向 secrets）。**不再需要**新增任何 `*_MCP_URL` 或端口。

***

## C. web tools（`app/tools/web.py`）+ SSRF 护栏

**C1. 新增** **`app/security/url_guard.py`（SSRF 单一实现）**

- `async def resolve_and_validate(url) -> list[str]`：仅 http/https；拒 userinfo；host 非空；若 `web_fetch_allowlist` 非空→default-deny 白名单外；先拒内网字面名（`localhost`/`host.docker.internal`/compose 服务名 `postgres|redis|mongo|elasticsearch|neo4j|tei-rerank|mineru|hr-mcp|finance-mcp|…`）；`asyncio.to_thread(socket.getaddrinfo)` 解 A/AAAA，逐条 `ipaddress.ip_address(ip).is_global` 判定，任一非 global（私网/回环/link-local/组播/保留/`169.254.169.254`）即拒。
- 说明 TOCTOU：拿到已校验 IP 后建议以固定 IP 发起连接（http 直连 IP+`Host`；https 走已校验 IP+SNI），退一步用"逐跳重解析+重校验"（fetch 里实现）。

**C2. 进程级共享 httpx 客户端** `app/tools/_http.py`：仿 [\_get\_client](file:///d:/ai/mxi/app/rag/reranker.py#L32-L44) 建 `AsyncClient(follow_redirects=False, timeout=..., limits=...)` 单例，检索/抓取复用（含关闭钩子，挂到 lifespan 或首次惰性建，与 reranker 关闭风格一致）。

**C3.** **`app/tools/web.py`（`@tool`）**

- `async def search_web(query: str, max_results: int = 0, recency: str = "") -> dict`：provider 分派 `ddgs`（`DDGS().text(...)` 同步→`to_thread`）/`tavily`/`serper`（httpx REST）；无 key 或失败回退 ddgs。返回 `{query, provider, results:[{title,url,snippet}], degraded}`。**永不抛未捕获异常**，失败返回 `{error, results:[]}` 让 ReAct 优雅降级。名字命中 `search_` → 自动被 Tool Cache 包。
- `async def fetch_url(url: str, max_chars: int = 0) -> dict`：先 `url_guard` 校验；`follow_redirects=False` 手动跟随≤`web_fetch_max_redirects`，**每跳重校验**；`stream` 累加字节超 `web_fetch_max_bytes` 立即中断；`Content-Type` 限 `text/html|text/plain|application/json|text/markdown`；`BeautifulSoup(..., "html.parser").get_text` 截断 `web_fetch_max_chars`。返回 `{url, final_url, title, text, truncated}` 或 `{error}`。命名不命中缓存前缀→不缓存。

***

## E. docgen tools（`app/tools/docgen.py`）+ 构建器 + 磁盘治理

**E1.** **`app/docgen/`** **包（纯函数构建器，被 tool 调用）**

- `docx_builder.py`（python-docx：title/heading/para/bullets 或轻量 markdown）、`xlsx_builder.py`（openpyxl：sheets→表头+二维数组）、`pptx_builder.py`（python-pptx：slides→{title,bullets}）、`pdf_builder.py`（reportlab：`pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))` 渲染中文，**零字体文件**）。
- `store.py`：`new_token()->uuid4().hex`；`gen_dir(token)=_upload_dir()/gen/<token>`（复用 [docs.service.\_upload\_dir](file:///d:/ai/mxi/app/docs/service.py#L46-L52)）；`cleanup_expired()` 按 `docgen_retention_hours` 扫 `gen/` 删过期。

**E2.** **`app/tools/docgen.py`（`@tool`，全部 async +** **`to_thread`** **卸载）**

- `generate_docx/generate_xlsx/generate_pptx/generate_pdf(...)`：入参结构化 spec（docstring 写清字段）；`await asyncio.to_thread(builder, ...)` 落盘到 `gen/<token>/<file>`；受 `docgen_max_bytes` 限；构建失败返回 `{error}`。
- 返回 `{file_name, doc_token, download_url, size_bytes, mime}`；`download_url = <base>/api/files/<token>/<file_name>`，`base = settings.public_base_url`（非空），否则由路由侧用请求 host 兜底。命名不命中缓存前缀→不缓存。

**E3. 下载路由** **`app/files/router.py`（网关；必须在 SPA 通配前注册）**

- `APIRouter(prefix="/api/files")`；`GET /{token}/{file_name}`：校验 token 形状（定长 hex）+ `file_name` 仅取 basename（复用 [\_safe\_filename](file:///d:/ai/mxi/app/docs/service.py#L55-L60) 语义，拒 `..`/隐藏）；路径 = `gen/<token>/<file>` `.resolve()` 后断言仍在 `gen/` 子树（防穿越）；命中→`FileResponse(path, filename=..., media_type=...)`（流式）；不存在→404。
- 在 [main.py create\_app](file:///d:/ai/mxi/app/main.py#L168-L171) 的 `include_router` 段加 `files_router`（在通配 `/{full_path:path}` 之前）；顺带把 `public_base_url` 补进 [\_log\_dependency\_endpoints](file:///d:/ai/mxi/app/main.py#L52-L79)。
- 访问控制：不可猜测 uuid 能力令牌为最小面（与网关现状一致）；绑定身份的强校验列后续。

**E4. 磁盘治理**：生成时 opportunistic 触发一次 `cleanup_expired()`；`docgen_retention_hours` 控上限。

***

## D. 能力注册表 + 路由/意图/缓存

**D1.** **`app/tools/__init__.py`** **暴露能力注册表**

- `CAPABILITY_TOOLS: dict[str, list[BaseTool]] = {"web": [search_web, fetch_url], "docgen": [generate_docx, generate_xlsx, generate_pptx, generate_pdf]}`；供 graph 按 target 取用。

**D2.** **[app/assistant/graph.py](file:///d:/ai/mxi/app/assistant/graph.py#L641-L705)** **[`tool_execute`](file:///d:/ai/mxi/app/assistant/graph.py#L641-L705)** **加"能力域"分支**

- `target = intent.target or "hr"`。
- 若 `target in CAPABILITY_TOOLS`：`all_tools = CAPABILITY_TOOLS[target]`；**跳过** `get_mcp_pool().get_tools` 与 `check_mcp_permission`（那是 MCP 专用，会默认拒未知 server）。可按 target 追加一句域内提示进 `system_context`：web→"回答须附来源 URL"；docgen→"生成后把返回的 `download_url` 原样完整展示为纯文本链接"。
- 否则：保持原 MCP 分派逻辑完全不变。
- 之后统一：`tools=[*all_tools, lookup_employee_by_name]` → `filter_tools_for_role(role, target, tools)`（能力域未建矩阵→透传全部，见下条）→ `wrap_tools_for_cache(tools, target, role.value)` → `create_agent(...)`。热路径其余不动。

**D3.** **[app/security/auth.py](file:///d:/ai/mxi/app/security/auth.py)**

- **无需**改 `MCP_WHITELIST`（web/docgen 不是 MCP server）。`filter_tools_for_role` 对无矩阵的 target 透传全部工具 → web/docgen 直接可用，**不必**建角色×工具矩阵。
- （可选）若要对 docgen 做角色限制，另加轻量判断；本期全角色开放。

**D4.** **[app/cache/tool\_cache.py](file:///d:/ai/mxi/app/cache/tool_cache.py)**：检索比业务实时数据可缓存更久——给 [\_key/cached\_tool\_call](file:///d:/ai/mxi/app/cache/tool_cache.py#L42-L80) 增一个按 server 解析的 TTL：`ttl = settings.web_search_cache_ttl if server=="web" else settings.tool_cache_ttl`（小改，可选；不做则沿用 30s 也满足"可缓存"）。

**D5.** **[app/assistant/intent.py](file:///d:/ai/mxi/app/assistant/intent.py)（让时效/生成类问题真正路由到 web/docgen）**

- `_parse_llm`：target 白名单 `("finance","hr")` → `("finance","hr","web","docgen")`（[L214-L216](file:///d:/ai/mxi/app/assistant/intent.py#L214-L216)）。
- `_AGENT_KEYWORDS`/`_detect_target`：加 `web`（最新/新闻/网上/搜/查一下网上/天气/股价/汇率/实时/近期/目前…）、`docgen`（生成/导出/做成/写份/文档/报告/表格/excel/word/ppt/pptx/pdf/幻灯片…）。
- `_RULES`：加 docgen 确定性规则（`(生成|导出|做成|写).{0,6}(文档|报告|表格|excel|word|ppt|pdf|幻灯片)`→`TOOL_CALL`）；web 主要靠 embedding+LLM，规则只兜显式联网动词。
- `_SEEDS`：加 `(TOOL_CALL,"web")`、`(TOOL_CALL,"docgen")` 各若干话术。
- [\_TIME\_CONTEXT\_PATTERNS](file:///d:/ai/mxi/app/assistant/intent.py#L49-L53)：补 `最新|最近|近期`，使 web 轮次预取平台时钟供检索按时间过滤。

**D6.** **[app/assistant/prompts.py INTENT\_PROMPT](file:///d:/ai/mxi/app/assistant/prompts.py#L3-L22)**：`tool_call` 现覆盖四域 `finance|hr|web|docgen`，给 web（时效/外部事实/联网）与 docgen（生成并下载文件）正反例；末尾 JSON `target` 取值集合更新。

***

## F. 打包/自检/最小接线（比 MCP 版小很多）

- **compose / Dockerfile / mcp\_client / dev\_services 服务项**：**都不需要改**（无新容器/端口/服务）。
- [scripts/package.py](file:///d:/ai/mxi/scripts/package.py)：若启用 Tavily/Serper，把 `TAVILY_API_KEY`/`SERPER_API_KEY` 加进 [`SECRET_KEYS`](file:///d:/ai/mxi/scripts/package.py#L106-L112)；[.dockerignore](file:///d:/ai/mxi/.dockerignore) 不动（`data/uploads/` 已排除，生成物不入包/镜像；无字体资产，扫描器无新增负担）。
- [scripts/dev\_services.py](file:///d:/ai/mxi/scripts/dev_services.py)：无新服务需登记；若加检索密钥，`SECRET_KEYS` 同步（可选）。
- 若用 Tavily/Serper：新建 `docker/secrets/tavily_api_key.txt`/`serper_api_key.txt`，[secrets README](file:///d:/ai/mxi/docker/secrets/README.md) 表格补行（默认 ddgs 免密→可不建）。
- 前端 [ChatView](file:///d:/ai/mxi/web-ui/src/views/ChatView.vue) 以 `pre-wrap` 纯文本渲染：`download_url` 必须是绝对 URL 便于点击/复制；渲成超链接为可选小改（非必需）。

***

## 依赖关系（顺序）

- A（依赖+uv.lock）是一切前置。
- B（config）先于 C/D/E（都读新配置）。
- C（web+guard）与 E（docgen+files 路由）相互独立，可并行。
- D（注册表/tool\_execute 分支/intent/cache）依赖 C、E 的工具最终命名；D1/D2 可先起草。
- F 仅密钥审计为条件项。
- **落地顺序**：A→B→C1(SSRF)→C2/C3→E1/E2/E3/E4→D1/D2/D4→D5/D6(路由)→F→自检。
- **可分期**：一期即含 PDF（reportlab 内置 CJK，风险可控）；若求稳可先 docx/xlsx/pptx + `search_web`（ddgs），`pdf`+`fetch_url`+Tavily/Serper 二期——但本次按你要求 PDF 纳入首期。

***

## 测试与验收

1. `uv run python -m scripts.package --check-only`：源侧密钥审计通过（如启用检索密钥）。
2. 依赖：`uv lock`+`uv sync` 通过；`docker compose -f docker/docker-compose.yml build` 在 `uv sync --frozen` 下成功。
3. web 路由：问"今天/最新的<外部事实>"→意图判 `tool_call/web`→`search_web` 返回结果；同参二次命中 Tool Cache（审计 `cache_hit`）。
4. SSRF：`fetch_url` 分别试 `http://169.254.169.254/`、`http://localhost`、`http://postgres:5432`、会 302 到内网的公网 URL、`WEB_FETCH_ALLOWLIST` 外域名——**全拒**；一条正常公网 HTML 成功抽正文。
5. docgen：问"生成一份 X 的 Word 报告"→`generate_docx` 返回 `download_url`→浏览器点链接下载成功；`generate_pdf` 打开中文不乱码（验 CID 字体）；文件确落 `data/uploads/gen/<token>/`。
6. **masking 校验（易踩坑）**：确认 [mask\_text](file:///d:/ai/mxi/app/security/masking.py) 不会把 URL/令牌里的数字段当敏感数据打码而破坏 `download_url`；若会，则把链接放 `ChatResponse.metadata` 结构化字段，或让 masking 跳过 URL 片段。
7. 事件循环：并发触发一次较大 pptx/pdf 生成，确认对话不被卡住（`to_thread` 生效）。
8. 回归：旧 finance/hr 的 tool\_call/agent\_delegate 判定与调用不变。

***

## 风险与缓解

- **mask\_text 损坏下载链接**：验收第 6 项显式核对；必要时链接走 metadata 结构化字段/URL 段豁免。
- **docgen 同步 CPU 阻塞事件循环**：所有 builder/ddgs/DNS 一律 `asyncio.to_thread`；生成体量受 `docgen_max_bytes`；过大再迁 worker/MCP（本文档记为纯 tool 形态的已知取舍）。
- **生成物撑爆共享卷**：`cleanup_expired()`+`docgen_retention_hours`+`docgen_max_bytes`。
- **SSRF/DNS-rebinding**：C1 解析即校验 + C3 逐跳重校验 + 建议固定 IP 连接；`is_global` 覆盖私网/元数据/IPv6/link-local；tools 在具备外联且身处内网的网关进程里，护栏更关键。
- **下载路由被 SPA 通配吞/目录穿越**：E3 在通配前注册 + `.resolve()` 后断言在 `gen/` 子树。
- **中文 PDF 豆腐块**：reportlab 内置 `STSong-Light`（不打包 TTF）；验收第 5 项核对；需强嵌入字形再引 TTF（后续）。
- **意图扩 target 扰动既有分类**：仅扩枚举/加种子/加关键词，验收第 8 项回归。
- **能力令牌外泄即失守**：本期 uuid+TTL 为最小面；身份绑定强校验列后续。
- **ddgs 在部分网络不可达**：provider 可切 Tavily/Serper；`search_web` 失败优雅降级 error 载荷，绝不整体失败对话。
- **tool\_execute 加分支引入回归**：分支只影响 `target∈{web,docgen}`，其余域逻辑原样，用最小 diff（先判能力域再回退原路径）。

***

## 被拒方案 / 与上一版差异（Rejected Alternatives）

1. **两组都做独立 MCP server（上一版方案）**：与 hr/finance 对称、可被任意 MCP 客户端复用、生成 CPU 独立容器隔离，热路径零分支——但要多两容器+端口(18003/18004)+compose service+mcp\_client 连接池+MCP\_WHITELIST+dev\_services/Dockerfile/package 触点，并引入跨容器卷/路径一致性负担。经与用户确认，选纯进程内 tool（运维最省）。
2. **混合：web 进程内 + docgen 仍 MCP**：为把重 CPU 的 docgen 隔离进独立容器；本期按"全纯 tool + `to_thread`"落地，若后续 docgen 体量增大再迁 MCP/worker。
3. **PDF 用 weasyprint / docx→pdf(LibreOffice)**：需 cairo/pango 系统库或整包 LibreOffice，`python:3.11-slim` 构建/体积风险高，Windows 宿主直跑更麻烦。→ 选 reportlab 纯轮子 + 内置 CID 中文。
4. **`data/uploads`** **直接** **`StaticFiles`** **挂载当下载**：暴露用户上传的 KB 原件且绕过权限。→ 作用域限 `gen/` 子树 + 能力令牌 + 穿越断言的 `FileResponse`。
5. **`fetch_url`/`generate_*`** **接入 Tool Cache**：抓网页正文波动大、生成是写操作，缓存有害无益。→ 命名不命中只读前缀，天然不缓存。
6. **给 web/docgen 建角色×工具矩阵**：`filter_tools_for_role` 对无矩阵域已透传全部工具，加矩阵反增维护点。→ 不建（也无需 `MCP_WHITELIST`，因为不是 MCP server）。
7. **同步把 web/docgen 注入 A2A 专业智能体 executor**：扩大改动面，聚焦对话入口。→ 列后续（`CAPABILITY_TOOLS` 已可被 executor 直接复用）。

