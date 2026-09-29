---
trigger: model_decision
description: 验证代码改动的唯一环境是 docker 容器（assistant 网关必须跑在 compose 内）：禁止宿主机直跑 uvicorn/网关/服务做验证，禁止用宿主 .env 结论代表容器行为；改过 app/ 代码必须重建镜像（dev.ps1 -Build 或 dev_services up --build）再看结果；前端 vite dev 与 Ollama 是仅存的宿主例外。凡涉及"验证/测试/跑一下/启动后端/热重载/改了没反应"的任务先读本规则。
---

# 容器唯一验证环境（禁止宿主验证）

自 2026-09 起，本项目的**代码验证只允许在 docker 容器内进行**。这是硬性限定，优先于任何
历史文档、脚本注释或记忆里的旧宿主直跑做法（包括 README 旧版"宿主网关 --reload"拓扑）。

## 不许做

1. **不许在宿主机直跑网关/后端服务做验证**：不得起 `uvicorn app.main:app`、不得起任何
   `app/mcp_servers/*`、`app/agents/*` 的宿主进程来"先看一眼"。宿主轨 `.env` 与容器轨
   （compose 注入）配置视角不同，宿主验证通过的结论对容器部署**不成立**。
2. **不许用宿主热重载代替镜像重建**：改过 `app/` 下任何后端代码，容器跑的是构建时快照，
   必须 `./scripts/dev.ps1 -Build`（或 `uv run python -m scripts.dev_services up --build`）
   重建后再验证。"改了没反应"的第一排查项就是忘了重建。
3. **不许在容器外复现问题后只修宿主侧**：定位 bug 可以用宿主脚本（评测/切块/自检类），
   但**修复结论必须在容器里复核**才算完成。
4. **不许把 assistant 从 compose dev 主轨里拿掉**：`DEV_SERVICES` 默认含 `assistant`；
   不得为"给宿主网关让端口"而 `docker compose stop assistant`（旧动作，已作废）。

## 必须做

- 起全栈：`./scripts/dev.ps1`（docker 全栈 + 自检 + vite）；改后端：加 `-Build`。
- 网关健康与对接结论以 `uv run python -m scripts.dev_services check --gateway` 为准，
  它探的是 **assistant 容器**发布端口的 `/api/health`；配置防覆盖用 `env-check`。
- 看网关日志：`docker logs -f assistant`（不再落宿主 logs/dev-gateway.log）。
- 宿主机仅存的两个合法前台：`web-ui` 的 vite dev（前端页面，不属于后端验证）与
  Ollama（:11434，唯一非 docker 依赖）。
- 更新任何描述开发拓扑的文档/注释时，同步按本规则口径改写，不得保留"宿主网关"表述。

## 相关

双轨配置红线见 `config-env-tracks.md`（compose 红线键已字面量锁死，docker/.env 覆盖不动）；
症状定位速查见 `config-redlines-checklist.md`。
