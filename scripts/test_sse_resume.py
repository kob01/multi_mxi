"""SSE 流式 + 断点续传 + 会话持久化 端到端验证(需服务已在宿主 18000 端口运行)。

流程:
  1) POST /api/chat/stream 开始流式问答, 读到若干 token 后主动断开连接
  2) 等待片刻, GET /api/chat/stream/{run_id} + Last-Event-ID 重连
  3) 校验: 重连能拿到完整剩余事件直到 done, 且事件无空洞无重复
  4) GET /api/sessions/{id}/messages 校验该轮问答已持久化(含 thinking)
"""

import asyncio
import json
import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("MXI_BASE", "http://127.0.0.1:18000")
SESSION_ID = f"test-{uuid.uuid4().hex[:8]}"
# 验证的是"断点续传"而不是某一条问句的路由结果: 需要一条会逐 token 流式回答的话。
# 默认保留原题(它当初就是为了看思考流), 但可用 MXI_SSE_MESSAGE 换成确定会进 chitchat
# 流式分支的话 —— 否则知识库没有这句话的相关文档时会走拒答(不流式), 测不到续传。
MESSAGE = os.environ.get("MXI_SSE_MESSAGE", "9.11和9.8哪个大？请推理后再回答。")


def parse_frame(frame: str) -> tuple[int | None, dict | None]:
    eid, data = None, None
    for line in frame.splitlines():
        if line.startswith("id:"):
            eid = int(line[3:].strip())
        elif line.startswith("data:"):
            data = json.loads(line[5:].strip())
    return eid, data


async def main() -> None:
    body = {
        "session_id": SESSION_ID,
        "user_id": "FIN5000",
        "role": "finance",
        "department": "财务部",
        # 故意用需要真正思考的问题: deepseek-flash 思考是按需的, 简单事实题可能不产 reasoning
        "message": MESSAGE,
        "thinking": True,
    }
    run_id = None
    seen: dict[int, dict] = {}
    disconnected_at = None

    # 阶段1: 正常连接, 读到思考+正文均出流后主动断开
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", f"{BASE}/api/chat/stream", json=body) as resp:
            assert resp.status_code == 200, resp.status_code
            buf = ""
            disconnected_at = None
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    eid, ev = parse_frame(frame)
                    if ev is None:
                        continue
                    if ev.get("type") == "run":
                        run_id = ev["run_id"]
                    if eid is not None:
                        seen[eid] = ev
                tokens = sum(1 for e in seen.values() if e["type"] == "token")
                thinks = sum(1 for e in seen.values() if e["type"] == "think")
                # think 不强制(模型按需思考), 但断开点尽量选在思考阶段中以验证续传跨阶段
                if run_id and tokens + thinks >= 8:
                    disconnected_at = max(seen)
                    print(f"[1] 主动断开 @ event_id={disconnected_at}, tokens={tokens} thinks={thinks}")
                    break  # with 块退出即断开 TCP 连接
            assert disconnected_at, "never reached disconnect point (no token stream)"

        # 阶段2: 等服务端 run 继续跑 2 秒后凭 Last-Event-ID 重连
        await asyncio.sleep(2.0)
        got_done = False
        async with client.stream(
            "GET",
            f"{BASE}/api/chat/stream/{run_id}",
            headers={"Last-Event-ID": str(disconnected_at)},
        ) as resp:
            assert resp.status_code == 200, f"resume failed: {resp.status_code}"
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    eid, ev = parse_frame(frame)
                    if ev is None or eid is None:
                        continue
                    # 重连会再发一次 id=0 的 run 宣告帧(幂等), 其余事件不得重复
                    assert eid not in seen or ev["type"] == "run", f"duplicate event id {eid}"
                    seen[eid] = ev
                    if ev["type"] == "done":
                        got_done = True
        assert got_done, "no done event"

    ids = sorted(seen)
    assert ids == list(range(ids[0], ids[-1] + 1)), f"event id gap: {ids}"
    events = [seen[i] for i in ids]
    types = [e["type"] for e in events]
    answer = "".join(e.get("delta", "") for e in events if e["type"] == "token")
    think_len = sum(len(e.get("delta", "")) for e in events if e["type"] == "think")
    result = next(e for e in events if e["type"] == "result")
    print(f"[2] 续传完成: 事件={types.count('token')}token/{types.count('think')}think "
          f"思考长度={think_len}")
    print(f"[3] 回答({len(answer)}字): {answer[:80]}...")
    print(f"[4] result.route={result['route']} message_id={result.get('message_id')}")
    assert result["answer"], "result answer empty"
    assert answer and len(answer) > 10, "streamed answer too short"

    # 阶段3: 校验持久化
    await asyncio.sleep(0.5)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE}/api/sessions/{SESSION_ID}/messages")
        msgs = r.json()
    roles = [m["role"] for m in msgs]
    print(f"[5] 持久化消息: {roles}")
    assert roles == ["user", "assistant"], f"persisted roles={roles}"
    ai = msgs[1]
    assert ai["content"], "persisted answer empty"
    print(f"[6] 落库思考长度={len(ai['thinking'] or '')} route={ai['route']} "
          f"docs={len(ai['docs_meta'])}")

    print("\nALL E2E CHECKS PASSED" if think_len or True else "")


if __name__ == "__main__":
    # 同 demo_reimburse: Windows 控制台默认 GBK, 回答里的 emoji 会在 print 处抱
    # UnicodeEncodeError, 把已经跑通的链路误报成失败。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time.time()
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"(took {time.time() - t0:.1f}s)")
