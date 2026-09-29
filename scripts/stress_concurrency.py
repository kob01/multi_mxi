"""对容器内 assistant 网关做并发压测(容量验证用, 不是功能测试)。

存在理由: 代码层面"能不能扛 N 人同时用"只能靠真打并发看出来 —— 连接池排队、
事件循环被同步 IO 卡住、SSE 读侧的 O(n^2) 扫描这类问题在单请求下完全不可见。

拓扑约束(见 .qoder/rules/container-first-verification.md): 被压的**必须**是容器里的
网关(默认 http://localhost:18000), 本脚本只是宿主侧的客户端, 不在宿主起任何服务。

跑法:
    uv run python -m scripts.stress_concurrency --clients 120
    uv run python -m scripts.stress_concurrency --clients 30 --kind kb
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

import httpx

# 每类问句给一批不同的原文: 全用同一句会让 Prompt Cache 命中, 测不出真实下游负载。
MESSAGES: dict[str, list[str]] = {
    "chat": [
        "你好，你是谁",
        "帮我打个招呼",
        "今天适合做什么",
        "说个冷笑话吧",
        "简单介绍一下你自己",
        "你能帮我做什么",
        "讲个短故事",
        "现在几点了",
    ],
    "kb": [
        "年假有几天",
        "报销需要哪些发票",
        "差旅费报销标准是多少",
        "公司的考勤制度是怎样的",
        "公积金缴纳比例是多少",
        "加班怎么调休",
        "在职证明怎么开",
        "报销多久到账",
    ],
}


async def one_turn(
    client: httpx.AsyncClient, base: str, message: str, user_id: str, session_id: str
) -> dict[str, Any]:
    """跑一轮流式对话: 发起 -> 读到 done 事件 -> 返回耗时与事件统计。"""
    started = time.perf_counter()
    events: dict[str, int] = {}
    answer_len = 0
    ok = False
    error = ""
    try:
        async with client.stream(
            "POST",
            f"{base}/api/chat/stream",
            json={
                "message": message,
                "user_id": user_id,
                "session_id": session_id,
                "role": "employee",
                "department": "研发部",
            },
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:200]
                return {
                    "ok": False,
                    "ms": int((time.perf_counter() - started) * 1000),
                    "status": resp.status_code,
                    "error": body,
                    "events": {},
                }
            current = ""
            async for raw in resp.aiter_lines():
                line = raw.strip()
                if line.startswith("event:"):
                    current = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    payload = line.split(":", 1)[1].strip()
                    if not payload:
                        continue
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue
                    kind = str(obj.get("type") or current or "unknown")
                    events[kind] = events.get(kind, 0) + 1
                    if kind == "token":
                        answer_len += len(str(obj.get("delta") or ""))
                    if kind == "done":
                        ok = str(obj.get("status")) == "completed"
                        if not ok:
                            error = "run 以 error 结束"
        if not ok and not error:
            error = "没收到 done 事件"
    except Exception as exc:  # noqa: BLE001 - 压测客户端要把任何异常计入失败而不是崩掉
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
    return {
        "ok": ok,
        "ms": int((time.perf_counter() - started) * 1000),
        "status": 200,
        "error": error,
        "events": events,
        "answer_len": answer_len,
    }


async def probe_readiness(client: httpx.AsyncClient, base: str) -> dict[str, Any]:
    try:
        resp = await client.get(f"{base}/api/health/ready")
        return {"status": resp.status_code, "body": resp.json()}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "body": {"error": str(exc)}}


async def main() -> int:
    parser = argparse.ArgumentParser(description="对容器内网关做并发压测")
    parser.add_argument("--base", default="http://localhost:18000")
    parser.add_argument("--clients", type=int, default=120, help="同时发起的对话轮数")
    parser.add_argument("--rounds", type=int, default=1, help="每个客户端跑几轮")
    parser.add_argument("--kind", default="chat", choices=sorted(MESSAGES))
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    pool = MESSAGES[args.kind]
    started_at = time.perf_counter()
    # 连接池上限要高于并发数, 否则测的是客户端自己的排队
    limits = httpx.Limits(max_connections=args.clients + 20, max_keepalive_connections=40)
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=10.0), limits=limits) as client:
        before = await probe_readiness(client, args.base)
        print(f"压测前 readiness: {before}")

        tasks = []
        for i in range(args.clients):
            for r in range(args.rounds):
                tasks.append(
                    one_turn(
                        client,
                        args.base,
                        pool[i % len(pool)],
                        f"load-{i % 37}",
                        f"load-{i}-{r}",
                    )
                )
        print(f"发起 {len(tasks)} 轮 {args.kind} 对话(并发 {args.clients}) ...")
        results = await asyncio.gather(*tasks)
        after = await probe_readiness(client, args.base)
        wall = time.perf_counter() - started_at

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    lat = sorted(r["ms"] for r in results)
    print("\n===== 结果 =====")
    print(f"总轮数 {len(results)} | 成功 {len(ok)} | 失败 {len(bad)}")
    if lat:
        print(
            f"墙钟 {wall:.1f}s | 吞吐 {len(results) / wall:.2f} 轮/s | "
            f"p50 {lat[len(lat) // 2]}ms | p95 {lat[int(len(lat) * 0.95) - 1]}ms | "
            f"max {lat[-1]}ms"
        )
    if bad:
        buckets: dict[str, int] = {}
        for r in bad:
            key = f"HTTP {r['status']}: {r['error'][:80]}"
            buckets[key] = buckets.get(key, 0) + 1
        print("失败分布:")
        for key, count in sorted(buckets.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {count}x  {key}")
    ev_total = sum(sum(r["events"].values()) for r in ok)
    print(f"事件总数 {ev_total} (平均每轮 {ev_total / max(1, len(ok)):.0f} 条)")
    print(f"压测后 readiness: {after}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
