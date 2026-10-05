"""Langfuse 上报冒烟脚本(在 assistant 容器内跑, 不依赖宿主网关)。

用途: 验证"容器内一轮真实对话 -> 自建 Langfuse 里出现一条完整 trace"这条链路,
并打印审计 trace_id 与按同一规则派生的 Langfuse trace id, 供人工在 UI 里对照。

前置:
  1. ``docker compose -f docker/docker-compose.yml --profile langfuse up -d`` 起栈;
  2. UI 里建好项目, 把 pk/sk 写进 docker/secrets/langfuse_api_key.txt(两行: sk / pk);
  3. docker/.env 置 LANGFUSE_ENABLED=true 并重建镜像。

跑法(改过 app/ 或 scripts/ 后必须先重建镜像, 见 container-first-verification 规则):

    ./scripts/dev.ps1 -Build
    docker compose -f docker/docker-compose.yml run --rm --no-deps assistant `
        python scripts/smoke_langfuse.py

未启用(开关关/密钥缺/镜像没装包)时打印 FAIL + 可执行原因并退出码 1, 不抛栈。
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from app.assistant.graph import get_orchestrator
from app.config import get_settings
from app.schemas import ChatRequest
from app.tracing import init_langfuse


def _why_off() -> str:
    """未启用时给出口径明确的排障结论(区分开关 / 密钥 / 地址)。"""
    s = get_settings()
    if not s.langfuse_enabled:
        return "LANGFUSE_ENABLED 未置 true (改 docker/.env)"
    if not (s.langfuse_api_key and s.langfuse_public_key):
        return (
            "密钥缺失: docker/secrets/langfuse_api_key.txt 需两行"
            "(第一行 sk-lf-..., 第二行 pk-lf-...)"
        )
    return f"开关与密钥都在但未生效 (base_url={s.langfuse_base_url})"


async def main() -> int:
    if not init_langfuse():
        print(f"FAIL langfuse 未启用 -> {_why_off()}")
        return 1

    orch = get_orchestrator()
    await orch.setup()
    session = f"lf-smoke-{uuid.uuid4().hex[:8]}"
    try:
        resp = await orch.handle(
            ChatRequest(
                session_id=session,
                user_id="lf-smoke-user",
                message="你好，用一句话介绍你自己",
            )
        )
    except Exception as exc:  # noqa: BLE001 - 冒烟只报告结论, 不在这里重试
        print(f"FAIL 对话链路异常: {type(exc).__name__}: {exc}")
        return 1

    from langfuse import Langfuse, get_client

    # 与 app/tracing.py 同一派生规则: 审计号 -> 确定性 Langfuse trace id。
    derived = Langfuse.create_trace_id(seed=resp.trace_id)
    get_client().flush()  # 批量队列同步落库, 否则立刻去 UI 查会缺数据

    print(f"OK 一轮对话已上报: session={session} route={resp.route}")
    print(f"   审计 trace_id        = {resp.trace_id}   (见 logs/audit.jsonl)")
    print(f"   Langfuse trace id    = {derived}")
    print("   UI 里按该 trace id 应能看到 LangGraph -> build_context/rewrite_query/"
          "classify_intent/... -> ChatDeepSeek 的完整层级")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
