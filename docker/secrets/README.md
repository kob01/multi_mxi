# docker/secrets —— 运行期密钥的唯一存放目录

本目录内容已被 `.gitignore` 排除（只提交本 README），**不要**把任何密钥写进
`.env` / `docker/.env`：那两个文件会被 compose 当作插值源并注入容器环境变量，
复制/截图/备份时极易连带带走凭据。

`docker/docker-compose.yml` 以 secret 文件形式把本目录下的文件挂载到容器内
`/run/secrets/<name>`，应用侧由 `app/config.py` 的校验器读取；在宿主机直跑网关或
评测脚本时，同一份文件作为回退路径被读取（`docker/secrets/<name>.txt`）。

## 需要创建的文件

| 文件                    | 用途                                                                   | 读取方                                      |
| ----------------------- | ---------------------------------------------------------------------- | ------------------------------------------- |
| `pg_password.txt`       | PostgreSQL 口令（compose 的 postgres 初始化 + 应用连接）               | postgres / assistant / _-mcp / _-agent      |
| `deepseek_api_key.txt`  | DeepSeek 在线 API Key                                                  | assistant / \*-agent                        |
| `zhipu_api_key.txt`     | 智谱开放平台 API Key（GLM 系列；`LLM_MODEL` 切到 `glm-*` 前必须填入真值，否则报"缺少在线 LLM 供应商配置"） | assistant / \*-agent / analytics-mcp |
| `langsmith_api_key.txt` | LangSmith Key（仅开发机启用 tracing 时需要）                           | assistant（容器侧 tracing 恒为 false）      |
| `langfuse_api_key.txt`  | Langfuse 项目密钥（自托管可观测；**两行格式**，见下方说明）       | assistant（`LANGFUSE_ENABLED=true` 时才读）    |
| `tavily_api_key.txt`    | Tavily 检索 Key（可选：启用 Tavily provider 时才需要；默认 ddgs 免密） | assistant（app/tools/web.py 的 search_web） |
| `serper_api_key.txt`    | Serper(Google) 检索 Key（可选：启用 Serper provider 时才需要）         | assistant（同上）                           |

文件内容 = 单行裸值，无引号、无 `KEY=` 前缀、行尾不要有多余空格（读取时会 `strip()`）。

> **补建/改完密钥文件后必须重建容器**：compose 在**容器创建时**解析 secret 挂载点，
> 源文件当时不存在不会报错，而是把 `/run/secrets/<name>` 挂成一个**空目录**；
> `app/config.py` 用 `is_file()` 判定，拿到的就是空值 → 表现为“密钥填了但功能仍不可用”
> 的静默降级。重建命令：`docker compose -f docker/docker-compose.yml up -d --force-recreate assistant`。

> `zhipu_api_key.txt` 例外地**默认挂载**在 compose 各 LLM 消费服务上（DeepSeek 同等待遇），
> 所以文件必须存在；未启用 GLM 时可留占位值 `REPLACE_ME_WITH_ZHIPU_API_KEY`，
> `app/config.py` 会把占位值视同未配置（不会拿它去调远端 API）。

> `langfuse_api_key.txt` 是全目录里唯一的**两行文件**：第一行 secret key
> （`sk-lf-...`）、第二行 public key（`pk-lf-...`），由 `app/config.py` 的
> `model_post_init` 拆开分填 `langfuse_api_key` / `langfuse_public_key`。Langfuse
> 的一个项目需要这一对密钥才能上报，而 compose 的 secret 只能挂单值文件，
> 故合成一份。它跟 `zhipu_api_key.txt` 一样被**默认挂载**在 assistant 上，
> 所以文件必须存在（否则 compose 直接报错）；**不用 Langfuse 就留空文件**，
> 空值等同未配置，`LANGFUSE_ENABLED` 也就起不了作用。

> 检索密钥（tavily/serper）是**可选**项：默认 provider `ddgs` 免密，不建这两个文件、
> 也不往 compose 里加 `secrets:` 声明，一切照常。若要启用，除创建文件外还需在
> `docker/docker-compose.yml` 的 `secrets:` 顶层声明与 `assistant.secrets:` 列表里各加一行
> （compose 对缺失的 secret 文件会直接报错，所以不能默认挂上）——宿主直跑网关则无需任何改动，
> `app/config.py` 会回退读本目录。

## 创建方式（PowerShell）

```powershell
# 1) 随机生成本地 PG 口令(首次建库前执行; 已有 pg_data 卷时须与卷内口令一致, 不要重生成)
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 24 | ForEach-Object {[char]$_}) |
  Set-Content -NoNewline -Encoding ascii docker/secrets/pg_password.txt

# 2) DeepSeek API Key(从供应商控制台粘贴; 输入不回显, 也不会进入命令历史)
$s = Read-Host "DeepSeek API Key" -AsSecureString
([PSCredential]::new('x', $s).GetNetworkCredential().Password) |
  Set-Content -NoNewline -Encoding ascii docker/secrets/deepseek_api_key.txt

# 2b) (切换到 GLM 系列前必填) 智谱开放平台 API Key
$s = Read-Host "Zhipu API Key" -AsSecureString
([PSCredential]::new('x', $s).GetNetworkCredential().Password) |
  Set-Content -NoNewline -Encoding ascii docker/secrets/zhipu_api_key.txt

# 3) (可选, 仅开发机) LangSmith API Key
Set-Content -NoNewline -Encoding ascii docker/secrets/langsmith_api_key.txt "粘贴密钥"

# 3b) Langfuse 项目密钥: 两行 = 第一行 sk-lf-... / 第二行 pk-lf-...
#     (从自建 langfuse-web 的 Project Settings > API Keys 取)
Set-Content -Encoding ascii docker/secrets/langfuse_api_key.txt "sk-lf-xxx`npk-lf-xxx"
#     不用 Langfuse 时建空文件即可(compose 要求它存在):
New-Item -ItemType File -Force docker/secrets/langfuse_api_key.txt | Out-Null

# 4) 权限: 只允许当前用户读写(Windows 下 Docker Desktop 走文件系统读取, 不影响挂载)
Get-ChildItem docker/secrets/*.txt | ForEach-Object {
  icacls $_.FullName /inheritance:r /grant "$($env:USERNAME):R" | Out-Null
}
```

## 校验

```powershell
# 只看文件是否存在、长度是否合理, 不要 cat 内容
Get-ChildItem docker/secrets/*.txt | Select-Object Name, Length
```

`uv run python -m scripts.dev_services check` 会顺带扫描 `.env` / `docker/.env`，
一旦发现密钥被直接写进配置文件就报错（不会打印值）。
