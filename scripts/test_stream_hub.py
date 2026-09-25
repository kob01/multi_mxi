"""StreamHub 断点续传行为自测(手动运行: python scripts/test_stream_hub.py)。"""

import asyncio

from app.assistant.stream import get_stream_hub


async def replay_test() -> None:
    hub = get_stream_hub()
    buf = hub.create("r2")
    for i in range(5):
        await buf.append({"type": "token", "delta": str(i)})

    async def finisher() -> None:
        await asyncio.sleep(0.2)
        await buf.append({"type": "done", "status": "completed"})
        await hub.finish("r2")

    task = asyncio.create_task(finisher())
    pairs = [(i, e) async for i, e in buf.iterate(2)]  # 断点: 从 id=2 之后重放
    await task
    assert [i for i, _ in pairs] == [3, 4, 5, 6], [i for i, _ in pairs]  # id3/4/5 重放 + id6 done
    got = [e for _, e in pairs]
    deltas = [e["delta"] for e in got if e["type"] == "token"]
    assert deltas == ["2", "3", "4"], deltas  # id1="0" id2="1" 被断点跳过
    assert got[-1]["type"] == "done"
    print("replay-from-breakpoint OK:", deltas)


async def live_test() -> None:
    hub = get_stream_hub()
    buf = hub.create("r3")
    received = []

    async def reader() -> None:
        async for _, e in buf.iterate(0):
            received.append(e)

    t = asyncio.create_task(reader())
    await asyncio.sleep(0.1)
    await buf.append({"type": "token", "delta": "a"})
    await buf.append({"type": "token", "delta": "b"})
    await asyncio.sleep(0.2)
    assert [e["delta"] for e in received] == ["a", "b"], received
    await buf.append({"type": "done", "status": "completed"})
    await hub.finish("r3")
    await asyncio.wait_for(t, timeout=2)
    print("live-follow OK:", received)


async def main_all() -> None:
    await replay_test()
    await live_test()
    print("stream hub tests passed")


if __name__ == "__main__":
    asyncio.run(main_all())
