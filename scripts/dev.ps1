<#
.SYNOPSIS
    开发期一键启动: docker 全栈(含 assistant 网关) + vite dev。

.DESCRIPTION
    拓扑约定(2026-09 起, 详见 .qoder/rules/container-first-verification.md 与
    CONFIG_RULES.md 第 9 条):
      - **容器是唯一的验证环境**: assistant 网关与全部依赖(postgres / elasticsearch /
        redis / neo4j / mongo / tei-rerank / mineru / hr-mcp / finance-mcp /
        analytics-mcp / procurement-mcp / hr-agent / finance-agent / analyst-agent /
        contract-agent)都跑在 docker compose 里。
      - 宿主机**禁止**直跑网关(uvicorn app.main:app)做代码验证 —— 宿主轨与容器轨配置
        视角不同, 宿主验证通过的结论对容器部署不成立。
      - 宿主机只跑两个例外: vite dev(前端页面, 不属于后端验证) 与 Ollama(:11434,
        唯一非 docker 依赖)。
      - 改过 app/ 任何后端代码 → 必须带 -Build 重建镜像, 否则容器跑的是旧快照,
        "改了没反应"。镜像层缓存了依赖(uv sync 只随 pyproject/uv.lock 变化),
        重建通常只重 COPY 几秒。

    网关访问地址: http://127.0.0.1:<ASSISTANT_HOST_PORT> (docker/.env, 默认 18000),
    vite 代理目标读仓根 .env 的 ASSISTANT_PORT —— 两者保持同值是双轨约定(见
    CONFIG_RULES.md 第 6 条), 换端口时同步改。

.PARAMETER Stop
    结束本脚本拉起的前台进程(vite 进程树, 按 logs/dev.pid 记录), 并停 compose 服务。

.PARAMETER SkipDocker
    跳过 compose 全栈的启动与自检(已经在别的终端起过时用它), 只拉起 vite。

.PARAMETER Build
    透传给 dev_services up: 先重建镜像。改过 app/ 下任何代码时必须加。

.EXAMPLE
    ./scripts/dev.ps1                 # 起全栈(含网关容器) + 自检 + vite
    ./scripts/dev.ps1 -Build          # 改了后端代码后的标准动作
    ./scripts/dev.ps1 -SkipDocker     # 依赖已就绪, 只拉起 vite
    ./scripts/dev.ps1 -Stop           # 停 vite 与 docker 全栈

.NOTES
    编码: 本文件必须存为 UTF-8 with BOM, 因为 Windows PowerShell 5.1 无 BOM 时按 GBK 解码,
    中文注释会变成乱码并直接导致脚本解析失败(实测报错在无关的 }/参数行上)。
    日志: 网关日志走 `docker logs assistant`(不在宿主落盘); 前端日志 logs/dev-web.log。
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
$WebLog = Join-Path $RepoRoot 'logs\dev-web.log'
$WebDir = Join-Path $RepoRoot 'web-ui'
$ComposeFile = 'docker/docker-compose.yml'

function Get-GatewayUrl {
    <# 网关宿主地址: 读 docker/.env 的 ASSISTANT_HOST_PORT(容器发布端口), 兜底 18000。 #>
    $envPath = Join-Path $RepoRoot 'docker\.env'
    if (Test-Path $envPath) {
        $line = Select-String -Path $envPath -Pattern '^\s*ASSISTANT_HOST_PORT\s*=\s*(\d+)' | Select-Object -Last 1
        if ($line) { return "http://127.0.0.1:$($line.Matches[0].Groups[1].Value)" }
    }
    return 'http://127.0.0.1:18000'
}

function Stop-DevFrontends {
    if (Test-Path $PidFile) {
        $pids = Get-Content $PidFile | Where-Object { $_ -match '^\d+$' }
        foreach ($procId in $pids) {
            # /T 连子进程一起结束: vite 的 node 是 cmd.exe /c 的子进程,
            # 只 kill 父进程会留下一堆还在占 5173 的孤儿。
            & taskkill /PID $procId /T /F 2>$null | Out-Null
            Write-Host "已结束进程树 $procId"
        }
        Remove-Item $PidFile -Force
    } else {
        Write-Host '没有 logs/dev.pid, 无需结束前台进程。' -ForegroundColor Yellow
    }
    Write-Host '停止 compose 全栈(含 assistant 网关容器, 只停不删卷)...'
    & docker compose -f $ComposeFile --profile mineru down --remove-orphans 2>$null | Out-Null
}

function Start-DevFrontends {
    param([string]$GatewayUrl)

    New-Item -ItemType Directory -Force -Path (Split-Path $PidFile) | Out-Null

    # pnpm 是 .cmd 包装, 直接 Start-Process 解析不稳, 走 cmd.exe /c 并记录 cmd 的 PID 树。
    $web = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'pnpm', 'dev' `
        -WorkingDirectory $WebDir -PassThru -NoNewWindow `
        -RedirectStandardOutput $WebLog -RedirectStandardError "$WebLog.err"

    @($web.Id) | Set-Content -Path $PidFile -Encoding ascii

    Write-Host ''
    Write-Host "网关(容器): $GatewayUrl      (日志: docker logs -f assistant)" -ForegroundColor Cyan
    Write-Host "前端 dev   : http://localhost:5173         (日志 $WebLog)" -ForegroundColor Cyan
    Write-Host '页面走 vite 代理, /api 与 /health 转发到上面的网关容器发布端口。' -ForegroundColor DarkGray
    Write-Host '提醒: 改过 app/ 后端代码要 ./scripts/dev.ps1 -Build 重建镜像才生效。' -ForegroundColor Yellow
    Write-Host '停止: ./scripts/dev.ps1 -Stop (vite 与 docker 全栈一起停)' -ForegroundColor DarkGray
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
        throw "docker 全栈启动失败 (exit=$LASTEXITCODE)。"
    }
    # 依赖就绪需要时间(TEI 载权重 / ES 建索引 / neo4j bolt 起来), 自检失败不阻断 vite 启动,
    # 但一定要把结论打出来: 静默降级的层连不上时功能"看起来正常", 只是结果不对。
    # --gateway 顺带探 assistant 容器的 /api/health —— 网关本身也是被检查对象了。
    & uv run python -m scripts.dev_services check --gateway
} else {
    Write-Host '已跳过 docker 启动(-SkipDocker); 确认 assistant 容器在跑: docker ps --filter name=assistant' -ForegroundColor Yellow
}

Start-DevFrontends -GatewayUrl (Get-GatewayUrl)
