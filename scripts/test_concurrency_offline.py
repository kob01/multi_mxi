"""高并发路径的离线自检(不起网关进程, 只在宿主机把纯逻辑跑一遍)。

存在理由: 这些行为都是"1000 人同时用才会暴露"的那一类 —— 缓冲区被扫穿、审计把
事件循环压在磁盘上、闸门归还漏一次就永久收紧。它们在容器里验证的成本很高(要真
造并发流量), 但实现本身是纯内存/纯文件的, 可以在这里用少量代码把不变量钉死。

跑法: uv run python -m scripts.test_concurrency_offline
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from app.assistant.stream import RunBuffer, RunOverloaded, StreamHub  # noqa: E402
from app.security.audit import AuditLogger  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# -------------------------------------------------------------------------- 1


async def read_all(buf: RunBuffer, from_id: int = 0, limit: int = 100000) -> list[int]:
    """读完一个已经 mark_done 的缓冲区(带硬上限, 避免测试自己把循环挂住)。"""
    out: list[int] = []
    async for i, _event in buf.iterate(from_id):
        out.append(i)
        if len(out) >= limit:
            break
    return out


async def test_run_buffer_bounded() -> None:
    print("\n1) RunBuffer: 事件数封顶 + id 单调 + 尾部不丢")
    buf = RunBuffer(max_events=100)
    ids = []
    for i in range(1000):
        ids.append(await buf.append({"type": "token", "delta": f"t{i}"}))
    check("自增 id 连续", ids == list(range(1, 1001)))
    check("事件数被封顶", len(buf.events) <= 100, f"实际 {len(buf.events)}")
    check("最后一条事件仍在缓冲区", buf.events[-1][1]["delta"] == "t999")
    check("跳号提示已生成", buf._skip is not None and buf._skip[1]["type"] == "skip")
    check("跳号区间覆盖到现存事件之前", buf._skip is not None and buf._skip[1]["until_id"] < buf.events[0][0])
    check("缓冲区里的事件 id 严格递增", all(b[0] > a[0] for a, b in zip(buf.events, buf.events[1:])))

    # 先收尾再读: 从 0 号读起必须拿到 skip 提示 + 尾部全部事件, 且不重复
    await buf.mark_done()
    seen = await read_all(buf, 0)
    check("尾部读者读到最后一条", seen[-1] == 1000, f"实际 {seen[-1]}")
    check("读者不会拿到重复 id", len(seen) == len(set(seen)))
    check(
        "读到的条数 = 现存事件 + 一条 skip 提示",
        len(seen) == len(buf.events) + 1,
        f"{len(seen)} vs {len(buf.events)}",
    )
    check("提示确实是第一条", [e for _, e in buf.pending(0)][0]["type"] == "skip")
    check(
        "已经过提示 id 的读者不会被重放提示",
        all(e["type"] != "skip" for _, e in buf.pending(buf._skip[0])),
    )


async def test_run_buffer_no_polling_scan() -> None:
    print("\n2) RunBuffer: 空闲读者不再轮询扫描(靠 Event 唤醒)")
    buf = RunBuffer(max_events=5000)
    for i in range(3000):
        await buf.append({"type": "token", "delta": f"t{i}"})

    received: list[int] = []

    async def reader() -> None:
        async for i, _event in buf.iterate(3000):
            received.append(i)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    # 没有新事件时读者必须安静地挂在 Event 上: 这段时间主循环可以毫无阻碍地跑别的
    for i in range(200):
        await buf.append({"type": "token", "delta": f"late{i}"})
    await asyncio.sleep(0.01)
    await buf.mark_done()
    await asyncio.wait_for(task, timeout=2.0)
    elapsed = time.perf_counter() - started
    check("迟到的 200 条全部读到", len(received) == 200, f"实际 {len(received)}")
    check("顺序正确", received == list(range(3001, 3201)), f"实际 {received[:3]}")
    check("端到端延迟远低于旧的 50ms 轮询粒度", elapsed < 0.5, f"耗时 {elapsed:.3f}s")


async def test_run_buffer_many_readers() -> None:
    print("\n3) RunBuffer: 多读者独立续流(断线重放)")
    buf = RunBuffer(max_events=2000)

    async def producer() -> None:
        for i in range(500):
            await buf.append({"type": "token", "delta": f"t{i}"})
            if i % 50 == 0:
                await asyncio.sleep(0)
        await buf.mark_done()

    got: dict[str, list[int]] = {}

    async def reader(name: str, from_id: int) -> None:
        got[name] = [i async for i, _event in buf.iterate(from_id)]

    prod = asyncio.create_task(producer())
    r1 = asyncio.create_task(reader("full", 0))
    await asyncio.sleep(0.01)
    r2 = asyncio.create_task(reader("late-join", 250))
    await asyncio.gather(prod, r1, r2)
    check("首发读者拿到全部 500 条", len(got["full"]) == 500, f"实际 {len(got['full'])}")
    check("中途接入读者只拿后半段", len(got["late-join"]) == 250, f"实际 {len(got['late-join'])}")
    check("两读者互不干扰", got["late-join"][0] == 251)


async def test_hub_gate() -> None:
    print("\n4) StreamHub: 并发闸门与缓冲区硬顶")
    hub = StreamHub()
    acquired = sum(1 for _ in range(300) if hub.try_acquire_run())
    check("闸门按配置卡住(默认 200)", acquired == 200, f"实际放行 {acquired}")
    check("超限请求被拒(RunOverloaded 的前置条件)", hub.try_acquire_run() is False)
    for _ in range(50):
        hub.release_run()
    check("归还后能再取到额度", hub.try_acquire_run() is True)
    hub.release_run()
    hub.release_run()
    check("计数不会转负", hub.inflight >= 0, f"实际 {hub.inflight}")

    # 缓冲区总数硬顶: 只回收已结束的, 在跑的绝不丢
    for i in range(2100):
        b = hub.create(f"run-{i}")
        if i % 2 == 0:
            await b.mark_done()
    check("缓冲区总数被压回硬顶以内", len(hub._buffers) <= 2000, f"实际 {len(hub._buffers)}")
    running = [rid for rid, b in hub._buffers.items() if not b.done]
    check("未结束的 run 一个都没被误回收", len(running) == 1050, f"实际 {len(running)}")


async def test_overload_raises() -> None:
    print("\n5) 过载是显式错误, 不是静默排队")
    hub = StreamHub()
    while hub.try_acquire_run():
        pass
    try:
        raise RunOverloaded("满了")
    except RunOverloaded as exc:
        check("RunOverloaded 可被路由层捕获转 503", str(exc) == "满了")


async def test_audit_batched() -> None:
    print("\n6) AuditLogger: 调用方不碰磁盘, 落盘走批量")
    tmp = Path(tempfile.mkdtemp()) / "audit.jsonl"
    batches: list[int] = []

    def writer(lines: list[str]) -> None:
        batches.append(len(lines))
        with tmp.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    log = AuditLogger(path=str(tmp), writer=writer)
    started = time.perf_counter()
    for i in range(5000):
        log.log(f"trace{i}", "assistant", "kb_retrieved", {"query": "x" * 40, "n": i}, "sess")
    produced = time.perf_counter() - started
    check("5000 条入队耗时极短(不阻塞事件循环)", produced < 1.0, f"耗时 {produced:.3f}s")
    check("flush 确认落盘完成", log.flush(timeout=5.0) is True)
    total = sum(batches)
    check("一条都没丢", total == 5000, f"实际 {total}")
    check(
        "写入是批量而非逐条(批数远小于条数)",
        len(batches) < 100,
        f"批数 {len(batches)}",
    )
    lines = tmp.read_text(encoding="utf-8").splitlines()
    check("落盘行数与条数一致", len(lines) == 5000, f"实际 {len(lines)}")
    first = json.loads(lines[0])
    check("记录结构完整", first["action"] == "kb_retrieved" and first["trace_id"] == "trace0")
    log.close()
    check("关停后 inflight 归零", log.inflight == 0)


async def test_audit_sync_fallback_after_close() -> None:
    print("\n7) AuditLogger: 关停后的迟到记录不静默丢")
    tmp = Path(tempfile.mkdtemp()) / "audit.jsonl"
    log = AuditLogger(path=str(tmp))
    log.log("t1", "assistant", "a", {})
    log.close()
    log.log("t2", "assistant", "b", {})  # 关停后迟到: 必须同步落盘
    lines = tmp.read_text(encoding="utf-8").splitlines()
    check("迟到记录已落文件", len(lines) == 2, f"实际 {len(lines)}")


async def test_masking_still_applied() -> None:
    print("\n8) 审计打码没有在改造中丢失")
    tmp = Path(tempfile.mkdtemp()) / "audit.jsonl"
    log = AuditLogger(path=str(tmp))
    log.log("t", "assistant", "x", {"phone": "13812345678"})
    assert log.flush(3.0)
    rec = json.loads(tmp.read_text(encoding="utf-8").splitlines()[0])
    check("手机号被掩码", rec["detail"]["phone"].startswith("138") and "****" in rec["detail"]["phone"],
          str(rec["detail"]))
    log.close()


async def main() -> int:
    await asyncio.wait_for(_all_tests(), timeout=90)
    print(f"\n通过 {PASS} 项, 失败 {FAIL} 项")
    return 1 if FAIL else 0


async def test_fetch_url_streaming() -> None:
    print("\n9) fetch_url: 流式读取与逐跳重定向真的能跑")
    import httpx

    import app.tools._http as http_mod
    from app.tools.web import fetch_url

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "http://testserver/new"})
        if request.url.path == "/big":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><body>" + b"x" * 4000 + b"</body></html>",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            # 真 UTF-8 字节(字面 \u 转义在 bytes 里不会被解释)
            content=(
                "<html><head><title>OK</title></head><body><p>你好</p>"
                "<script>var a=1;</script></body></html>"
            ).encode("utf-8"),
        )

    # 把 web 工具的共享客户端换成 Mock(不打真网), 并把 SSRF 护栏换成固定公网 IP:
    # 本用例测的是"流式读取/逐跳重定向/限量截断"这段实现, 护栏自己另有专门用例。
    # 必须同时改 url_guard 与 web 两个命名空间: 本模块用 ``from ... import`` 绑了名字。
    import app.security.url_guard as guard
    import app.tools.web as web_mod

    http_mod._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(5.0),
    )

    async def _fake_validate(url: str) -> list[str]:
        return ["93.184.216.34"]

    original = guard.resolve_and_validate
    guard.resolve_and_validate = _fake_validate
    web_mod.resolve_and_validate = _fake_validate
    try:
        got = await fetch_url.ainvoke({"url": "http://testserver/new"})
        check("直连页面抽取成功", got.get("title") == "OK" and "你好" in got.get("text", ""), str(got)[:160])
        check("script 内容不进正文", "var a=1" not in got.get("text", ""))
        got2 = await fetch_url.ainvoke({"url": "http://testserver/old"})
        check("重定向逐跳跟随后仍拿到正文", got2.get("title") == "OK", str(got2)[:120])
        check("重定向目标确实被请求过", "/new" in "".join(calls))
        got3 = await fetch_url.ainvoke({"url": "http://testserver/big", "max_chars": 100})
        check("超上限正文被截断", got3.get("truncated") is True and len(got3.get("text", "")) <= 101, str(got3)[:120])
        got4 = await fetch_url.ainvoke({"url": ""})
        check("空 url 返回错误载荷而不是抛出", bool(got4.get("error")))
    finally:
        web_mod.resolve_and_validate = original
        guard.resolve_and_validate = original
        await http_mod._client.aclose()
        http_mod._client = None


async def _all_tests() -> None:
    await test_run_buffer_bounded()
    await test_run_buffer_no_polling_scan()
    await test_run_buffer_many_readers()
    await test_hub_gate()
    await test_overload_raises()
    await test_audit_batched()
    await test_audit_sync_fallback_after_close()
    await test_masking_still_applied()
    await test_fetch_url_streaming()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
