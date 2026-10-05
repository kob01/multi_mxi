"""多智能体并发委派的整栈验证(需网关已运行)。

跑法::

    ./scripts/dev.ps1 -Build          # 容器内起全栈并重建镜像(改过 app/ 代码必须重建)
    uv run python -m scripts.test_multi_agent

关闭开关验证回退(网关需以 MULTI_AGENT_ENABLED=false 启动)::

    $env:MXI_MULTI_AGENT_OFF = "1"; uv run python -m scripts.test_multi_agent

覆盖:
  1. ``GET /api/agents`` 按角色给出可点选清单(员工无 analytics, 管理角色有; 未知角色
     归一为员工);
  2. 点选两个智能体 -> route=multi_agent, 同一问题各自成节且都拿到答复;
  3. 真的并发: 从审计日志(agent_task_completed/failed 的 ts + elapsed_ms)算出各委派的
     执行区间, 两个区间必须重叠 —— 整轮墙钟含改写/记忆召回等固定开销, 拿它判并行会误判;
  4. 逐项进度事件: 每个智能体各有"处理中/已完成或未完成"两条 delegate status;
  5. 非法域与超上限: 不执行但明确交代, 且不给非法域发网络调用;
  6. 权限: 员工点 analytics -> 该节写"未执行: 权限不足", 其余节照常交付;
  7. 断点续流: 分节整篇下发后中途断开, 凭 Last-Event-ID 重连仍可拿到完整 result;
  8. 回归: 单意图三条老路由(知识库/A2A 委派/闲聊)与逐项进度不串台。知识库那条
     依赖宿主 Ollama(bge-m3), 它不可达时该断言打 SKIP 而不是 FAIL。

入参清洗/分节文案/图装配等纯逻辑在 scripts/test_multi_agent_offline.py 离线覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

BASE = os.environ.get("MXI_BASE", "http://127.0.0.1:18000")
USER = os.environ.get("MXI_USER", "E10005")
ROLE = os.environ.get("MXI_ROLE", "employee")
DEPT = os.environ.get("MXI_DEPT", "研发部")
MANAGER = os.environ.get("MXI_MANAGER", "E10001")
OFF = os.environ.get("MXI_MULTI_AGENT_OFF") == "1"
# 审计日志: 容器内落 /data/logs/audit.jsonl, 已映射回仓根 logs/。
AUDIT_PATH = Path(os.environ.get("MXI_AUDIT", "logs/audit.jsonl"))
# 审计落盘有两道异步: 写入线程 0.2s 一批 + Docker 目录挂载在主机侧可能短暂无陈旧读。
# 因此客户端刚拿到 result 就读文件会缺行, 需要按预期条数轮询(最多下面这个秒数)。
AUDIT_WAIT = float(os.environ.get("MXI_AUDIT_WAIT", "25"))
# 宿主 Ollama(bge-m3) 是全栈唯一非 docker 依赖: 它没起时知识库检索会直接报错,
# 那是环境缺项而不是本次改动改坏了路由, 所以相关的回归断言降为 SKIP。
OLLAMA = os.environ.get("MXI_OLLAMA", "http://127.0.0.1:11434")

_QUESTION = "帮我看看我的年假还剩几天，顺便看看报销政策"
_ALL_FOUR = ["hr", "finance", "analytics", "procurement"]

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def parse_frame(frame: str) -> dict | None:
    for line in frame.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                return None
    return None


async def stream_chat(
    client: httpx.AsyncClient,
    message: str,
    session_id: str,
    *,
    targets: list[str] | None = None,
    role: str = ROLE,
    user: str = USER,
    stop_after: int = 0,
) -> tuple[dict, list[dict], float, int]:
    """发一轮流式问答, 返回 ``(result 事件, 全部事件, 墙钟秒, 最后事件 id)``。

    ``targets`` 非空即开启本轮多智能体并发委派; ``stop_after`` > 0 时读到该数量事件就
    主动断开(模拟刷新/断网), 此时 result 为空字典。
    """
    body = {
        "session_id": session_id, "user_id": user, "role": role,
        "department": DEPT, "message": message, "thinking": False,
        "agent_targets": targets or [],
    }
    events: list[dict] = []
    result: dict = {}
    last_id = 0
    started = time.perf_counter()
    async with client.stream("POST", f"{BASE}/api/chat/stream", json=body) as resp:
        resp.raise_for_status()
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while (idx := buf.find("\n\n")) >= 0:
                frame, buf = buf[:idx], buf[idx + 2:]
                for line in frame.splitlines():
                    if line.startswith("id:"):
                        last_id = int(line[3:].strip() or last_id)
                event = parse_frame(frame)
                if event is None:
                    continue
                events.append(event)
                if event.get("type") == "result":
                    result = event
                if stop_after and len(events) >= stop_after:
                    return result, events, time.perf_counter() - started, last_id
                if event.get("type") == "done":
                    return result, events, time.perf_counter() - started, last_id
    return result, events, time.perf_counter() - started, last_id


def stages(events: list[dict]) -> list[str]:
    return [str(e.get("stage") or "") for e in events if e.get("type") == "status"]


def status_texts(events: list[dict], stage: str) -> list[str]:
    return [
        str(e.get("text") or "") for e in events
        if e.get("type") == "status" and e.get("stage") == stage
    ]


def sections(answer: str) -> list[str]:
    return re.findall(r"^## \d+\. .*$", answer, flags=re.M)


def agent_intervals(trace_id: str, expect: int = 0, wait_s: float | None = None) -> list[dict]:
    """从审计日志取本轮各委派的执行区间 ``[{index, domain, agent, start, end, elapsed_ms, ok}]``。

    审计行只有完成时的 ``ts`` 与 ``elapsed_ms``, 回推起点即得区间 —— 这是"是否真重叠"
    的唯一客户端证据(整轮墙钟还含改写/记忆召回的固定开销, 拿它判并发会误判)。

    客户端刚拿到 result 时审计可能还没可读(批量落盘 + 挂载层短暂陈旧), 故按 ``expect``
    条数轮询到齐或超时才返回。
    """
    deadline = time.perf_counter() + (wait_s if wait_s is not None else AUDIT_WAIT)
    out: list[dict] = []
    while True:
        out = _scan_intervals(trace_id)
        if len(out) >= expect or time.perf_counter() >= deadline:
            return out
        time.sleep(0.5)


def _scan_intervals(trace_id: str) -> list[dict]:
    if not AUDIT_PATH.is_file():
        return []
    out: list[dict] = []
    for line in AUDIT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("trace_id") != trace_id or rec.get("action") not in {"agent_task_completed", "agent_task_failed"}:
            continue
        detail = rec.get("detail") or {}
        try:
            end = datetime.fromisoformat(str(rec.get("ts")))
        except ValueError:
            continue
        elapsed = float(detail.get("elapsed_ms") or 0) / 1000.0
        out.append({
            "index": detail.get("index"), "domain": detail.get("domain"),
            "agent": detail.get("agent"), "start": end.timestamp() - elapsed,
            "end": end.timestamp(), "elapsed_ms": elapsed * 1000.0,
            "ok": rec.get("action") == "agent_task_completed",
        })
    return out


def overlaps(a: dict, b: dict) -> bool:
    """两个执行区间是否实质重叠(至少重叠 200ms, 滤掉信号量交接的贴边)。"""
    lo = max(a["start"], b["start"])
    hi = min(a["end"], b["end"])
    return (hi - lo) > 0.2


async def get_agents(client: httpx.AsyncClient, role: str) -> list[dict]:
    resp = await client.get(f"{BASE}/api/agents", params={"role": role})
    resp.raise_for_status()
    return list(resp.json())


def domains_of(items: list[dict]) -> set[str]:
    return {str(i.get("domain") or "") for i in items}


async def main() -> int:
    prefix = f"mag-{uuid.uuid4().hex[:8]}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        try:
            health = await client.get(f"{BASE}/api/health")
            check("网关可达", health.is_success, str(health.status_code))
        except Exception as exc:  # noqa: BLE001
            print(f"网关不可达({BASE}): {exc}", file=sys.stderr)
            return 2

        # 依赖预检: 知识库路由需要宿主 Ollama(bge-m3) 出向量。注意**主机自探成功不代表
        # 容器能连上**(它走 host.docker.internal), 所以不拿它当断言前提, 只在回归那一条
        # 里根据实际错误文本区分"环境缺项"与"路由真坏了"。
        ollama_host_reachable = True
        try:
            await client.get(f"{OLLAMA}/api/tags", timeout=3.0)
        except Exception:  # noqa: BLE001
            ollama_host_reachable = False
        print(f"INFO  宿主 Ollama({OLLAMA}) 探测: {'可连' if ollama_host_reachable else '不可连'}"
              "  (知识库路由还取决于容器能不能连它)")

        # ---- 1. 可点选清单(按角色过滤) ----
        emp = await get_agents(client, "employee")
        mgr = await get_agents(client, "manager")
        bogus_role = await get_agents(client, "not_a_role")
        check("员工清单含 hr 与 finance", {"hr", "finance"} <= domains_of(emp), str(emp))
        check("员工清单不含 analytics(敏感能力默认拒)", "analytics" not in domains_of(emp), str(emp))
        check("管理角色清单多一个 analytics", "analytics" in domains_of(mgr) and len(mgr) >= len(emp), str(mgr))
        check("清单项都带可读卡片名与能力说明", all(i.get("name") and i.get("description") for i in mgr), str(mgr))
        check("未知角色归一为员工清单(不放大权限)", domains_of(bogus_role) == domains_of(emp), str(bogus_role))

        # ---- 2/3/4. 点选两个智能体: 都答上、真并发、逐项进度 ----
        picked = [d for d in ("hr", "finance") if d in domains_of(emp)][:2]
        result, events, _wall, _last = await stream_chat(client, _QUESTION, f"two-{prefix}", targets=picked)
        agents_meta = (result.get("metadata") or {}).get("agents") or []
        answer = str(result.get("answer") or "")

        if OFF:
            check("关闭开关后点选被忽略(回退单意图路由)", result and result.get("route") != "multi_agent",
                  f"route={result.get('route')}")
            check("关闭开关后不产生逐个智能体的结果元数据", not agents_meta, str(agents_meta))
        else:
            check("点选两个智能体走多智能体并发路由", result.get("route") == "multi_agent",
                  f"route={result.get('route')} answer={answer[:160]}")
            check("每个智能体各占一节", len(sections(answer)) >= 2, f"sections={sections(answer)}")
            check("逐项结果元数据齐", len(agents_meta) == len(picked), str(agents_meta))
            check("intent 标为 agent_delegate 且 layer=explicit",
                  str(result.get("intent") or "") == "agent_delegate"
                  and "显式点选" in str((result.get("metadata") or {}).get("reason") or ""),
                  str(result.get("metadata")))
            check("节标题用可读卡片名", any(
                str(m.get("agent") or "") in answer for m in agents_meta if m.get("agent")), answer[:200])
            texts = status_texts(events, "delegate")
            check("逐项进度事件齐(每个智能体有开始与结束两条)",
                  len([t for t in texts if "处理中" in t]) >= len(picked)
                  and len([t for t in texts if "已完成" in t or "未完成" in t]) >= len(picked),
                  f"delegate_events={texts}")
            # 并发的证据只看各委派自己的执行区间是否重叠(墙钟判定不可靠)。
            intervals = agent_intervals(str(result.get("trace_id") or ""), expect=len(picked))
            check("审计已逐个落委派区间(可按 trace_id 复盘)", len(intervals) >= len(picked), str(intervals))
            if len(intervals) >= 2:
                pairs = [(a, b) for n, a in enumerate(intervals) for b in intervals[n + 1:]]
                check("两个委派的执行区间真重叠(并发不是串行)",
                      any(overlaps(a, b) for a, b in pairs),
                      " | ".join(f"{i['domain']}[{i['elapsed_ms']:.0f}ms]" for i in intervals))

        # ---- 5. 非法域与超上限: 不执行但要交代 ----
        if not OFF:
            bad, bad_events, _bw, _bi = await stream_chat(
                client, _QUESTION, f"bad-{prefix}", targets=["hr", "not_an_agent_domain"]
            )
            bad_answer = str(bad.get("answer") or "")
            check("非法域那一节明确写未执行", "未执行" in bad_answer and "not_an_agent_domain" in bad_answer,
                  bad_answer[:240])
            check("非法域不影响其余节正常交付", any(
                m.get("ok") for m in (bad.get("metadata") or {}).get("agents") or []), str(bad.get("metadata")))
            planned = _audit_last_detail("multi_agent_planned", str(bad.get("trace_id") or ""))
            check("非法域不发出网络调用(审计里归 invalid 而非执行结果)",
                  "not_an_agent_domain" in json.dumps(planned.get("invalid", []), ensure_ascii=False),
                  str(planned))

            over, _oe, _ow, _oi = await stream_chat(
                client, _QUESTION, f"over-{prefix}", targets=_ALL_FOUR, role="manager", user=MANAGER
            )
            over_answer = str(over.get("answer") or "")
            max_targets = len([m for m in (over.get("metadata") or {}).get("agents") or []])
            check("超出可点选上限的项被截断且说明", "超出单次可点选上限" in over_answer, over_answer[:240])
            check("上限之外的智能体未被执行", max_targets <= 4, f"sections={max_targets}")

            # ---- 6. 权限: 员工点 analytics 只拒那一节 ----
            perm, _pe, _pw, _pi = await stream_chat(
                client, _QUESTION, f"perm-{prefix}", targets=["hr", "analytics"]
            )
            perm_answer = str(perm.get("answer") or "")
            perm_meta = (perm.get("metadata") or {}).get("agents") or []
            check("无权智能体那一节写未执行并给原因", "权限不足" in perm_answer or any(
                m.get("status") == "denied" for m in perm_meta), perm_answer[:240])
            check("被拒不连坐: 有权的节仍正常办成", any(m.get("ok") for m in perm_meta), str(perm_meta))

        # ---- 7. 断点续流: 分节整篇下发后中途断开可续回 ----
        _r, mid_events, _w, last_id = await stream_chat(
            client, _QUESTION, f"rs-{prefix}", targets=["hr"], stop_after=3
        )
        run_id = next((str(e.get("run_id")) for e in mid_events if e.get("type") == "run"), "")
        resumed: dict = {}
        if run_id:
            async with client.stream(
                "GET", f"{BASE}/api/chat/stream/{run_id}",
                headers={"Last-Event-ID": str(last_id)},
            ) as resp:
                check("重连端点认这个 run_id", resp.status_code == 200, str(resp.status_code))
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    while (idx := buf.find("\n\n")) >= 0:
                        frame, buf = buf[:idx], buf[idx + 2:]
                        event = parse_frame(frame)
                        if event and event.get("type") == "result":
                            resumed = event
            check("断开后重连仍能拿到完整结果", bool(str(resumed.get("answer") or "")),
                  str(resumed)[:160])
        else:
            check("断开后重连仍能拿到完整结果", False, "未取得 run_id")

        # ---- 8. 回归: 单意图三条老路由不变, 也不串到并发分支 ----
        kb, kb_events, _w2, _i2 = await stream_chat(client, "差旅费报销标准是多少", f"kb-{prefix}")
        kb_err = next((str(e.get("message") or "") for e in kb_events if e.get("type") == "error"), "")
        if kb.get("route") is None and "connection" in kb_err.lower():
            # 容器连不上宿主 embedding 是环境缺项, 不该报成"路由被改坏"。
            print(f"SKIP  知识库单问法不变: 容器内取向量失败({kb_err[:60]}), 先起 Ollama 再跑本脚本")
        else:
            check("知识库单问法不变", kb.get("route") == "assistant_kb",
                  f"route={kb.get('route')} err={kb_err[:80]}")
        delegate, d_events, _w3, _i3 = await stream_chat(client, "我要报销", f"del-{prefix}")
        check("纯办理单问法仍走单智能体委派", delegate.get("route") == "a2a_agent", f"route={delegate.get('route')}")
        chat_, _c4, _w4, _i4 = await stream_chat(client, "你好呀", f"hi-{prefix}")
        check("寒暄仍走直答", chat_.get("route") == "direct", f"route={chat_.get('route')}")
        check("单意图轮次不携逐个智能体的结果元数据",
              not ((kb.get("metadata") or {}).get("agents") or (delegate.get("metadata") or {}).get("agents")),
              str(kb.get("metadata")))
        check("未点选时不会走并发委派节点", all(
            "并发委派" not in t for t in status_texts(d_events, "routed")),
            str(status_texts(d_events, "routed")))

    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


def _audit_last_detail(action: str, trace_id: str, wait_s: float | None = None) -> dict:
    """取本轮某 action 的最后一条审计 detail(审计是 append-only, 尾扫即可; 同样按可读轮询)。"""
    deadline = time.perf_counter() + (wait_s if wait_s is not None else AUDIT_WAIT)
    found: dict = {}
    while True:
        found = _scan_last_detail(action, trace_id)
        if found or time.perf_counter() >= deadline:
            return found
        time.sleep(0.5)


def _scan_last_detail(action: str, trace_id: str) -> dict:
    if not AUDIT_PATH.is_file() or not trace_id:
        return {}
    found: dict = {}
    for line in AUDIT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("trace_id") == trace_id and rec.get("action") == action:
            found = rec.get("detail") or {}
    return found


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
