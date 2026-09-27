<#
.SYNOPSIS
    开发期一键启动: docker 依赖服务 + 宿主网关(--reload) + vite dev。

.DESCRIPTION
    拓扑约定(详见 README「本地开发」与 CONFIG_RULES.md 第 5 条):
      - 宿主机只跑两个前台进程: uvicorn(网关, 热重载) 与 vite dev(前端页面)。
      - 其余依赖全部对接 docker compose: postgres / elasticsearch / redis / neo4j /
        mongo / tei-rerank / mineru / hr-mcp / finance-mcp / hr-agent / finance-agent。
      - 唯一非 docker 依赖是宿主机的 Ollama(:11434)。

    为什么不直接跑 `docker compose up -d`: assistant 也在里面的话, 宿主网关与它抢同一个
    宿主端口(默认 18000), 且改代码要重建镜像才生效 —— 于是"改了没反应"会长期存在。
    本脚本因此先确保 assistant 不在跑, 再把端口让给宿主网关。

.PARAMETER Stop
    结束本脚本拉起的前台进程(按 logs/dev.pid 记录的进程树 kill), 不动 docker 服务。

.PARAMETER SkipDocker
    跳过 compose 依赖服务的启动与自检(已经在别的终端起过时用它)。

.PARAMETER Build
    透传给 dev_services up: 先重建镜像。改过 app/mcp_servers 或 app/agents 里的代码时
    必须加, 否则 docker 侧跑的还是旧镜像快照。

.EXAMPLE
    ./scripts/dev.ps1                 # 起依赖 + 自检 + 拉起网关与前端
    ./scripts/dev.ps1 -SkipDocker     # 依赖已就绪, 只拉起两个前台进程
    ./scripts/dev.ps1 -Build          # 顺带重建 mcp/agent 镜像
    ./scripts/dev.ps1 -Stop           # 停掉前台进程

.NOTES
    编码: 本文件必须存为 UTF-8 with BOM, 因为 Windows PowerShell 5.1 无 BOM 时按 GBK 解码,
    中文注释会变成乱码并直接导致脚本解析失败(实测报错在无关的 }/参数行上)。
    日志: logs/dev-gateway.log / logs/dev-web.log (含 stderr)。
    端口: 网关端口读仓根 .env 的 ASSISTANT_PORT, 与 docker 发布端口、vite 代理目标同源。
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$SkipDocker,
    [switch]$Build
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot 'logs\dev.pid'
$GatewayLog = Join-Path $RepoRoot 'logs\dev-gateway.log'
$WebLog = Join-Path $RepoRoot 'logs\dev-web.log'
$WebDir = Join-Path $RepoRoot 'web-ui'
$ComposeFile = 'docker/docker-compose.yml'

function Get-AssistantPort {
    <# 网关宿主端口: 与 app/config.py 的 assistant_port 同源(仓根 .env), 兜底 18000。 #>
    $envPath = Join-Path $RepoRoot '.env'
    if (Test-Path $envPath) {
        $line = Select-String -Path $envPath -Pattern '^\s*ASSISTANT_PORT\s*=\s*(\d+)' | Select-Object -Last 1
        if ($line) { return [int]$line.Matches[0].Groups[1].Value }
    }
    return 18000
}

function Stop-DevFrontends {
    if (-not (Test-Path $PidFile)) {
        Write-Host '没有 logs/dev.pid, 无需结束前台进程。' -ForegroundColor Yellow
        return
    }
    $pids = Get-Content $PidFile | Where-Object { $_ -match '^\d+$' }
    foreach ($procId in $pids) {
        # /T 连子进程一起结束: uvicorn --reload 的 worker、vite 的 node 都是子进程,
        # 只 kill 父进程会留下一堆还在占端口的孤儿。
        & taskkill /PID $procId /T /F 2>$null | Out-Null
        Write-Host "已结束进程树 $procId"
    }
    Remove-Item $PidFile -Force
}

function Start-DevFrontends {
    param([int]$Port)

    New-Item -ItemType Directory -Force -Path (Split-Path $PidFile) | Out-Null

    # 宿主网关要 bind $Port; 若 docker 的 assistant 容器占着同一端口, 先停容器让位。
    $occupants = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($occupants) {
        Write-Host "端口 $Port 已被占用, 尝试停掉 compose 里的 assistant 容器让位..." -ForegroundColor Yellow
        & docker compose -f $ComposeFile stop assistant 2>$null | Out-Null
        Start-Sleep -Seconds 2
        if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
            throw "端口 $Port 仍被占用: 手动确认占用者后重试 (Get-NetTCPConnection -LocalPort $Port)"
        }
    }

    # 优先用 .venv 里的 python 直接跑, 避免 uv 包一层父进程导致 -Stop 杀不干净。
    $py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        $py = (Get-Command uv).Source
        $pyArgs = @('run', 'python', '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', $Port, '--reload')
    } else {
        $pyArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', $Port, '--reload')
    }
    $gateway = Start-Process -FilePath $py -ArgumentList $pyArgs `
        -WorkingDirectory $RepoRoot -PassThru -NoNewWindow `
        -RedirectStandardOutput $GatewayLog -RedirectStandardError "$GatewayLog.err"

    # pnpm 是 .cmd 包装, 直接 Start-Process 解析不稳, 走 cmd.exe /c 并记录 cmd 的 PID 树。
    $web = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'pnpm', 'dev' `
        -WorkingDirectory $WebDir -PassThru -NoNewWindow `
        -RedirectStandardOutput $WebLog -RedirectStandardError "$WebLog.err"

    @($gateway.Id, $web.Id) | Set-Content -Path $PidFile -Encoding ascii

    Write-Host ''
    Write-Host "网关   : http://127.0.0.1:$Port        (日志 $GatewayLog)" -ForegroundColor Cyan
    Write-Host "前端dev: http://localhost:5173         (日志 $WebLog)" -ForegroundColor Cyan
    Write-Host '页面走 vite 代理, /api 与 /health 转发到上面的网关端口。' -ForegroundColor DarkGray
    Write-Host '停止前台进程: ./scripts/dev.ps1 -Stop' -ForegroundColor DarkGray
}

if ($Stop) {
    Stop-DevFrontends
    return
}

if (-not $SkipDocker) {
    $upArgs = @('-m', 'scripts.dev_services', 'up')
    if ($Build) { $upArgs += '--build' }
    & uv run python @upArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker 依赖服务启动失败 (exit=$LASTEXITCODE)。"
    }
    # 依赖就绪需要时间(TEI 载权重 / ES 建索引 / neo4j bolt 起来), 自检失败不阻断前台启动,
    # 但一定要把结论打出来: 静默降级的层连不上时功能"看起来正常", 只是结果不对。
    & uv run python -m scripts.dev_services check
}

Start-DevFrontends -Port (Get-AssistantPort)
