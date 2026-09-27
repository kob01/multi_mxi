---
trigger: glob
glob:
  - app/assistant/a2a_client.py
  - app/agents/**/agent_card.py
  - app/agents/**/executor.py
---

# A2A：卡片通告地址不得用作路由依据

a2a-sdk 的 JSON-RPC transport 用 `agent_card.url` 作为 RPC 目标，因此：

- 智能体侧 `HR_AGENT_URL` / `FINANCE_AGENT_URL` 在容器里写 compose 服务名（如 `http://hr-agent:9001`）。
- Assistant 侧**必须**以配置端点为准调用，即保留 `app/assistant/a2a_client.py` 中 `_pin_card_url()` 的覆盖：
  `card.model_copy(update={"url": base_url.rstrip("/") + "/"})`。
- **禁止**改成"按 `card.url` 直连"或删掉这层覆盖。

## 原因

开发拓扑是"网关跑宿主、智能体跑容器"。卡片里通告的 `hr-agent` 在宿主机无法解析——不覆盖的话卡片发现成功，`message/send` 却连不上，委派链路只在运行时静默报 `A2A 调用失败`，看起来像"报销/工单功能坏了"。

## 检查时机

改动 `a2a_client.py`、`app/agents/*/agent_card.py`，或 compose 里的 `*_AGENT_URL`；以及排查"委派失败但卡片正常"时，先核对本条。另注意 A2A 委派整体不参与 Tool Cache（只缓存只读工具）。
