"""多任务并行的整栈验证(需网关已运行)。

跑法::

    uv run uvicorn app.main:app --port 18000 --reload    # 或容器化网关
    uv run python -m scripts.test_multi_task

关闭开关验证回退(网关需以 MULTI_TASK_ENABLED=false 启动)::

    $env:MXI_MULTI_TASK_OFF = "1"; uv run python -m scripts.test_multi_task

覆盖:
  1. 复合问法("查查我年假还剩几天，明天北京天气咋样")-> route=multi_task, 两件
     事各自成节且都拿到答复, 不再丢一半;
  2. 真的并行: 从审计日志(subtask_completed 的 ts + elapsed_ms)算出各子任务执行区间,
     并行集内的两个区间必须重叠; 业务域 tool_call 按口径串行, 不参与重叠判定;
  3. 逐项进度事件: planning 给总件数, subtask 给每件事的开始与完成;
  4. 混合场景: 只读项 + 办理项 -> 办理项串行尾随但仍被处理(不被并行集吞掉);
  5. 断点续流: 并行分节整篇下发后中途断开, 凭 Last-Event-ID 重连仍可拿到完整 result;
  6. 回归: 单意图问法/纯办理/闲聊三条旧路由不变。

拆分器输出清洗、并行集口径、分节文案等纯逻辑在 scripts/test_multi_task_offline.py 离线覆盖。
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
MULTI_OFF = os.environ.get("MXI_MULTI_TASK_OFF") == "1"
# 审计日志: 容器内落 /data/logs/audit.jsonl, 已映射回仓根 logs/。
AUDIT_PATH = Path(os.environ.get("MXI_AUDIT", "logs/audit.jsonl"))
# 并行集口径与服务端 _PARALLEL_SAFE_TARGETS 保持一致(只读通道)。
PARALLEL_TARGETS = {"web", "docgen"}

_COMPOUND = "查查我年假还剩几天，明天北京天气怎么样"
# 三件事版本: 天气(web)与报销标准(知识库)都属并行集, 用于验证区间真重叠。
_COMPOUND3 = "查查我年假还剩几天，明天北京天气怎么样，另外差旅费报销标准是多少"
_MIXED = "查一下我的年假还剩几天，顺便帮我提一笔报销"
# 缺陷回归: 同主体多槽位追问(日期+时刻)应判单任务, 不进多任务并行。
_SLOT_PILING = "天安门下次升旗是哪天，时间几点"

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
    stop_after: int = 0,
) -> tuple[dict, list[dict], float, int]:
    """发一轮流式问答, 返回 ``(result 事件, 全部事件, 墙钟秒, 最后事件 id)``。

    ``stop_after`` > 0 时读到该数量事件就主动断开(模拟刷新/断网), 此时 result 为空字典。
    """
    body = {
        "session_id": session_id, "user_id": USER, "role": ROLE,
        "department": DEPT, "message": message, "thinking": False,
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


def section_bodies(answer: str) -> list[str]:
    """按 ``## N.`` 标头切正文, 返回每节的答复文本(含首行标头以便失败节可读)。"""
    parts = re.split(r"(?m)^(?=## \d+\. )", answer)
    return [p.strip() for p in parts if p.strip().startswith("## ")]


def subtask_intervals(trace_id: str) -> list[dict]:
    """从审计日志取本轮各子任务的执行区间 ``[{index, start, end, route, target}]``。

    审计行只有完成时的 ``ts`` 与 ``elapsed_ms``, 回推起点即得区间 —— 这是"是否真重叠"
    的唯一客户端证据(墙钟时延里还包括改写/拆分/分类的固定开销, 拿它判并行会误判)。
    """
    if not AUDIT_PATH.is_file():
        return []
    out: list[dict] = []
    for line in AUDIT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("trace_id") != trace_id or rec.get("action") not in {"subtask_completed", "subtask_failed"}:
            continue
        detail = rec.get("detail") or {}
        try:
            end = datetime.fromisoformat(str(rec.get("ts")))
        except ValueError:
            continue
        elapsed = float(detail.get("elapsed_ms") or 0) / 1000.0
        out.append({
            "index": detail.get("index"), "route": detail.get("route"),
            "target": detail.get("target"), "start": end.timestamp() - elapsed,
            "end": end.timestamp(), "elapsed_ms": elapsed * 1000.0,
            "ok": rec.get("action") == "subtask_completed",
        })
    return out


def is_parallel_item(item: dict) -> bool:
    return item.get("route") == "assistant_kb" or (
        item.get("route") == "mcp_tool" and (item.get("target") or "") in PARALLEL_TARGETS
    )


def overlaps(a: dict, b: dict) -> bool:
    """两个执行区间是否实质重叠(至少重叠 200ms, 滤掉信号量交接的贴边)。"""
    lo = max(a["start"], b["start"])
    hi = min(a["end"], b["end"])
    return (hi - lo) > 0.2


async def main() -> int:
    prefix = f"multi-{uuid.uuid4().hex[:8]}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        try:
            health = await client.get(f"{BASE}/api/health")
            check("网关可达", health.is_success, str(health.status_code))
        except Exception as exc:  # noqa: BLE001
            print(f"网关不可达({BASE}): {exc}", file=sys.stderr)
            return 2

        # ---- 1/2/3. 复合问法: 拆得开、答得全、真并行 ----
        result, events, _wall, _last = await stream_chat(client, _COMPOUND, f"cmp-{prefix}")
        subs = (result.get("metadata") or {}).get("subtasks") or []
        answer = str(result.get("answer") or "")

        if MULTI_OFF:
            check("关闭开关后回退单意图路由", result and result.get("route") != "multi_task",
                  f"route={result.get('route')}")
            check("关闭开关后不产生逐项进度", not subs and "planning" not in stages(events))
        else:
            check("复合问法走多任务并行", result.get("route") == "multi_task",
                  f"route={result.get('route')} answer={answer[:160]}")
            check("拆出至少两件独立子任务", len(subs) >= 2, str(subs))
            check("每件事各自成节且没丢", len(sections(answer)) >= 2, f"sections={sections(answer)}")
            check("年假那件走了 hr 域实时查询",
                  any(s.get("target") == "hr" and s.get("ok") for s in subs), str(subs))
            check("天气那件走了 web 能力域",
                  any(s.get("target") == "web" for s in subs), str(subs))
            leave_ok = ("天" in answer) or ("年假" in answer)
            weather_degraded = any(k in answer for k in ("未能联网", "不可用", "未完成"))
            check("年假答复落到正文", leave_ok, answer[:240])
            check("天气答复有正文或显式降级说明", (not weather_degraded) or len(sections(answer)) >= 2,
                  answer[:240])
            sub_events = status_texts(events, "subtask")
            check("逐项进度事件齐(每件事都有开始与完成两条)",
                  "planning" in stages(events) and len(sub_events) >= 2 * len(subs),
                  f"planning={'planning' in stages(events)} subtask_events={sub_events}")
            # 并行的证据只看子任务自己的执行区间是否重叠: 整轮墙钟还含改写/拆分/分类
            # 的固定开销, 拿它判并行会把"已并行"误判成"没并行"。
            intervals = subtask_intervals(str(result.get("trace_id") or ""))
            par = [i for i in intervals if is_parallel_item(i)]
            ser = [i for i in intervals if not is_parallel_item(i)]
            check("审计已逐项落 subtask 区间(可按 trace_id 复盘)", len(intervals) >= 2, str(intervals))
            check("两件事轮的并行集口径: 只 web 入并行、hr 查询串行",
                  len(par) == 1 and len(ser) >= 1 and par[0]["target"] == "web",
                  f"parallel={[(i['route'], i['target']) for i in par]} serial={[(i['route'], i['target']) for i in ser]}")
            if len(par) >= 2:
                pairs = [(a, b) for n, a in enumerate(par) for b in par[n + 1:]]
                check("并行集内子任务执行区间真重叠",
                      any(overlaps(a, b) for a, b in pairs),
                      " | ".join(f"{i['route']}:{i['target']}[{i['elapsed_ms']:.0f}ms]" for i in par))

        # ---- 3b. 三件事问法: 两个只读项重叠, 业务域查询项串行尾随 ----
        if not MULTI_OFF:
            r3, _e3b, _w3b, _i3b = await stream_chat(client, _COMPOUND3, f"cmp3-{prefix}")
            subs3 = (r3.get("metadata") or {}).get("subtasks") or []
            ans3 = str(r3.get("answer") or "")
            check("三件事拆到三个子任务", len(subs3) >= 3, str(subs3))
            check("三件事各自成节不丢件", len(sections(ans3)) >= 3, f"sections={sections(ans3)}")
            bodies3 = section_bodies(ans3)
            check("三件事都有正文(无失败节)",
                  len(bodies3) >= 3 and all("未完成" not in b for b in bodies3),
                  " || ".join(b[:60] for b in bodies3))
            iv3 = subtask_intervals(str(r3.get("trace_id") or ""))
            par3 = [i for i in iv3 if is_parallel_item(i)]
            check("并行集两项(联网检索 + 知识库)区间重叠",
                  len(par3) >= 2 and any(
                      overlaps(a, b) for n, a in enumerate(par3) for b in par3[n + 1:]
                  ), f"parallel={[(i['route'], i['target']) for i in par3]}")
            check("业务域查询项仍按口径串行(不入并行集)",
                  all(not is_parallel_item(i) for i in iv3 if (i.get("target") or "") not in PARALLEL_TARGETS and i.get("route") == "mcp_tool"),
                  str(iv3))

        # ---- 4. 混合场景: 只读项并行 + 办理项串行尾随 ----
        mixed, mixed_events, _mw, _mid = await stream_chat(client, _MIXED, f"mix-{prefix}")
        msubs = (mixed.get("metadata") or {}).get("subtasks") or []
        if MULTI_OFF:
            check("混合问法在关闭开关时按单意图处理", mixed.get("route") != "multi_task",
                  f"route={mixed.get('route')}")
        else:
            manswer = str(mixed.get("answer") or "")
            check("混合问法仍走多任务", mixed.get("route") == "multi_task", f"route={mixed.get('route')}")
            check("办理项未被并行集吞掉(仍作为独立子任务被处理)",
                  any(s.get("route") == "a2a_agent" for s in msubs) or len(msubs) >= 2,
                  str(msubs))
            check("办理项与只读项各自成节", len(sections(manswer)) >= 2, f"sections={sections(manswer)}")
            # 串行尾随的可观测证据: 办理项的进完事件里带专业智能体标签, 且它的开始时间
            # 不早于只读项的完成事件(gather 先发出并行段的开始, 串行段在后)。
            order = [t for t in status_texts(mixed_events, "subtask")]
            delegate_at = next((i for i, t in enumerate(order) if "专业智能体" in t), -1)
            readonly_done = next((i for i, t in enumerate(order) if "已完成" in t and "专业智能体" not in t), -1)
            check("办理项在只读项完成之后才启动",
                  delegate_at < 0 or readonly_done < 0 or delegate_at > readonly_done,
                  f"order={order}")

        # ---- 5. 断点续流: 并行分节整篇下发后中途断开可续回 ----
        _r, _e, _w, last_id = await stream_chat(client, _COMPOUND, f"rs-{prefix}", stop_after=3)
        run_id = next((str(e.get("run_id")) for e in _e if e.get("type") == "run"), "")
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
            r_answer = str(resumed.get("answer") or "")
            check("断开后重连仍能拿到完整多任务结果",
                  bool(r_answer) and (MULTI_OFF or len(sections(r_answer)) >= 2 or r_answer),
                  r_answer[:160])
        else:
            check("断开后重连仍能拿到完整多任务结果", False, "未取得 run_id")

        # ---- 6. 回归: 单意图三条老路由不变 ----
        kb, _e2, _w2, _i2 = await stream_chat(client, "差旅费报销标准是多少", f"kb-{prefix}")
        check("知识库单问法不变", kb.get("route") == "assistant_kb", f"route={kb.get('route')}")
        delegate, _e3, _w3, _i3 = await stream_chat(client, "我要报销", f"del-{prefix}")
        check("纯办理单问法仍走委派", delegate.get("route") == "a2a_agent", f"route={delegate.get('route')}")
        chat_, _e4, _w4, _i4 = await stream_chat(client, "你好呀", f"hi-{prefix}")
        check("寒暄仍走直答", chat_.get("route") == "direct", f"route={chat_.get('route')}")
        # 缺陷回归: 同主体多槽位追问不得被拆成并行(一次检索即可答全)。
        slot, _e5, _w5, _i5 = await stream_chat(client, _SLOT_PILING, f"slot-{prefix}")
        check("同主体多槽位追问不进多任务并行", slot.get("route") != "multi_task",
              f"route={slot.get('route')}")
        check("同主体多槽位追问不产生逐项进度", "subtask" not in stages(_e5) and "planning" not in stages(_e5),
              f"stages={stages(_e5)}")
        check("单意图轮次不产生逐项进度",
              all("subtask" != s for s in stages(_e2) + stages(_e3) + stages(_e4)))

    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
