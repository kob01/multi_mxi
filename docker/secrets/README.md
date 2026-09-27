# docker/secrets —— 运行期密钥的唯一存放目录

本目录内容已被 `.gitignore` 排除（只提交本 README），**不要**把任何密钥写进
`.env` / `docker/.env`：那两个文件会被 compose 当作插值源并注入容器环境变量，
复制/截图/备份时极易连带带走凭据。

`docker/docker-compose.yml` 以 secret 文件形式把本目录下的文件挂载到容器内
`/run/secrets/<name>`，应用侧由 `app/config.py` 的校验器读取；在宿主机直跑网关或
评测脚本时，同一份文件作为回退路径被读取（`docker/secrets/<name>.txt`）。

## 需要创建的文件

| 文件 | 用途 | 读取方 |
| --- | --- | --- |
| `pg_password.txt` | PostgreSQL 口令（compose 的 postgres 初始化 + 应用连接） | postgres / assistant / *-mcp / *-agent |
| `deepseek_api_key.txt` | DeepSeek 在线 API Key | assistant / *-agent |
| `langsmith_api_key.txt` | LangSmith Key（仅开发机启用 tracing 时需要） | assistant（容器侧 tracing 恒为 false） |

文件内容 = 单行裸值，无引号、无 `KEY=` 前缀、行尾不要有多余空格（读取时会 `strip()`）。

## 创建方式（PowerShell）

```powershell
# 1) 随机生成本地 PG 口令(首次建库前执行; 已有 pg_data 卷时须与卷内口令一致, 不要重生成)
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 24 | ForEach-Object {[char]$_}) |
  Set-Content -NoNewline -Encoding ascii docker/secrets/pg_password.txt

# 2) DeepSeek API Key(从供应商控制台粘贴; 输入不回显, 也不会进入命令历史)
$s = Read-Host "DeepSeek API Key" -AsSecureString
([PSCredential]::new('x', $s).GetNetworkCredential().Password) |
  Set-Content -NoNewline -Encoding ascii docker/secrets/deepseek_api_key.txt

# 3) (可选, 仅开发机) LangSmith API Key
Set-Content -NoNewline -Encoding ascii docker/secrets/langsmith_api_key.txt "粘贴密钥"

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
