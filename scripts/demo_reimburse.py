"""End-to-end demo: 用户输入"我要报销" -> Assistant 识别 ->
A2A 委派 Finance_Agent -> MCP 调用财务系统 -> 返回办理结果。

Prerequisite: all services running (docker compose up, or run each module
locally) and Ollama models pulled.

Usage:
    python -m scripts.demo_reimburse
    # 指向其他入口: python -m scripts.demo_reimburse  (改 MXI_BASE)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx

# 宿主发布端口 18000 (容器内仍为 8000), 见 docker/docker-compose.yml 顶部约定。
ASSISTANT = os.environ.get("MXI_BASE", "http://localhost:18000") + "/api/chat"


async def turn(client: httpx.AsyncClient, session: str, message: str) -> dict:
    resp = await client.post(
        ASSISTANT,
        json={"session_id": session, "user_id": "E10001", "role": "employee", "message": message},
        timeout=180.0,
    )
    resp.raise_for_status()
    return resp.json()


async def main() -> None:
    session = f"demo-{uuid.uuid4().hex[:8]}"
    print("=" * 70)
    print("端到端演示:报销办理 (Assistant -> A2A Finance_Agent -> MCP 财务系统)")
    print("=" * 70)

    async with httpx.AsyncClient() as client:
        # Turn 1: vague request -> agent asks for details
        r1 = await turn(client, session, "我要报销")
        print(f"\n[用户] 我要报销")
        print(f"[路由] {r1['route']} -> {r1.get('target')}  [trace={r1['trace_id'][:8]}]")
        print(f"[助手] {r1['answer']}")

        # Turn 2: provide details -> Finance_Agent calls MCP create_reimbursement
        r2 = await turn(client, session, "报销上海出差的高铁票,差旅费,金额 553 元")
        print(f"\n[用户] 报销上海出差的高铁票,差旅费,金额 553 元")
        print(f"[路由] {r2['route']} -> {r2.get('target')}  [trace={r2['trace_id'][:8]}]")
        print(f"[助手] {r2['answer']}")

        # Turn 3: knowledge query -> KB path
        r3 = await turn(client, session, "差旅费报销政策是什么")
        print(f"\n[用户] 差旅费报销政策是什么")
        print(f"[路由] {r3['route']}  [trace={r3['trace_id'][:8]}]")
        print(f"[助手] {r3['answer']}")

    print("\n审计留痕见 logs/audit.jsonl,可用 trace_id 串联全链路。")
    print(json.dumps({"session": session}, ensure_ascii=False))


if __name__ == "__main__":
    # Windows 控制台默认 GBK, 而智能体回答里常带 emoji/表格符号 —— 不重配编码会在
    # print 那一步直接 UnicodeEncodeError, 看起来像链路挂了其实是打印挂了。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
