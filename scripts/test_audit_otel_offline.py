"""审计 OTel 主写路径的离线自检(不起栈, 只在宿主机把数据面不变量钉死)。

存在理由: 这次改造的铁律是"换执行者不换字节" —— OTel 只是把攒批与落盘从自研线程
换成 LoggerProvider, 写进 audit.jsonl 的行 JSON 必须与原生路径逐字段一致, 否则 SIEM
采集与 grep 复盘会在无人察觉时漂出第二套格式。同时"留痕永不失败"红线不能因为引入
可选第三方 SDK 而被破: 未装 SDK / 配置关闭 / 单条 emit 异常, 任一种都要只把那一条降级
回原生落盘, 一条不丢。这些都是纯内存/纯文件行为, 在容器里验证成本高, 这里少量代码锁死。

跑法: uv run python -m scripts.test_audit_otel_offline
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

from app.config import get_settings  # noqa: E402
from app.security.audit import AuditLogger, _w3c_int, new_trace_id  # noqa: E402

PASS = 0
FAIL = 0

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_TS_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def set_otel(enabled: bool) -> None:
    """把单例 settings 的开关就地翻面(get_settings 是 lru_cache 单进程共享)。"""
    get_settings().audit_otel_enabled = enabled


def tmp_path() -> Path:
    return Path(tempfile.mkdtemp()) / "audit.jsonl"


def drain(log: AuditLogger) -> list[str]:
    """flush 确认落盘后读回全部行(不 close, 让调用方自己决定关停时机)。"""
    assert log.flush(timeout=5.0) is not False, "flush 返回 False"
    p = log.path
    return p.read_text(encoding="utf-8").splitlines() if p.is_file() else []


# -------------------------------------------------------------------------- 1


def test_new_trace_id_is_w3c() -> None:
    print("\n1) new_trace_id: 已是合法 W3C 128-bit(不换号的依据)")
    t = new_trace_id()
    check("32 位小写 hex", bool(_HEX32.match(t)), t)
    n = _w3c_int(t)
    check("可转成非零 128-bit int", n is not None and 0 < n < (1 << 128), str(n))
    # 历史降级值不该把 emit 拖崩: 归 None(不建关联)而不是抛错。
    check("unknown → None(不建关联, 不报错)", _w3c_int("unknown") is None)
    check("空串 → None", _w3c_int("") is None)
    check("全零 → None(W3C 禁止)", _w3c_int("0" * 32) is None)


# -------------------------------------------------------------------------- 2


def test_langfuse_alignment_is_derivation() -> None:
    print("\n2) Langfuse 对齐: create_trace_id(seed=) 是确定性派生(非恒等)")
    try:
        from langfuse import Langfuse
    except Exception as exc:  # noqa: BLE001 - 没装 langfuse 就跳过, 不影响主结论
        print(f"  [skip] langfuse 不可用: {exc}")
        return
    t = new_trace_id()
    d1 = Langfuse.create_trace_id(seed=t)
    d2 = Langfuse.create_trace_id(seed=t)
    check("派生结果 32 位小写 hex", bool(_HEX32.match(d1)), d1)
    check("同 seed 派生恒等(可互查)", d1 == d2, f"{d1} vs {d2}")
    # 记录一个事实: Langfuse 号 != 审计号(它是 seed 的 sha256 派生), 所以本次不换审计号。
    check("审计号本身不是恒等映射(故不换号)", d1 != t, f"{d1} vs {t}")


# -------------------------------------------------------------------------- 3


def test_otel_path_byte_format_matches_native() -> None:
    print("\n3) 数据面不变量: OTel 路径与原生路径的行 JSON 逐字段同格式")
    fixed_trace = "d0de013b4486455d91b12924afd0bb1d"
    detail = {"query": "年假还剩几天", "n": 1}

    # 原生路径(显式关 OTel)。
    set_otel(False)
    log_native = AuditLogger(path=str(tmp_path()))
    log_native.log(fixed_trace, "assistant", "kb_retrieved", detail, "sess-1")
    line_native = drain(log_native)[0]
    log_native.close()

    # OTel 主写路径。
    set_otel(True)
    log_otel = AuditLogger(path=str(tmp_path()))
    log_otel.log(fixed_trace, "assistant", "kb_retrieved", detail, "sess-1")
    line_otel = drain(log_otel)[0]
    log_otel.close()

    d_native = json.loads(line_native)
    d_otel = json.loads(line_otel)
    check("字段名与顺序一致", list(d_native.keys()) == list(d_otel.keys()),
          f"{list(d_native.keys())} vs {list(d_otel.keys())}")
    check("键集合 = ts/trace_id/session_id/actor/action/detail",
          list(d_otel.keys()) == ["ts", "trace_id", "session_id", "actor", "action", "detail"],
          str(list(d_otel.keys())))
    # 除 ts(毫秒时间戳本就逐条不同)外, 同输入应产出完全相同的值。
    check("非 ts 字段值逐条相等", {k: v for k, v in d_otel.items() if k != "ts"}
          == {k: v for k, v in d_native.items() if k != "ts"})
    check("OTel 行 ts 是毫秒 ISO", bool(_TS_MS.match(d_otel["ts"])), d_otel["ts"])
    check("OTel 行 trace_id 原样保留", d_otel["trace_id"] == fixed_trace, d_otel["trace_id"])


# -------------------------------------------------------------------------- 4


def test_otel_counts_and_masking() -> None:
    print("\n4) OTel 主写: 批量不丢 + 打码仍在 + unknown 号也留痕")
    set_otel(True)
    log = AuditLogger(path=str(tmp_path()))
    n = 2000
    for i in range(n):
        log.log(new_trace_id(), "assistant", "loop", {"i": i}, "sess")
    log.log("unknown", "assistant", "peer_delegated", {"domain": "hr"}, "sess")  # 降级号
    lines = drain(log)
    check(f"{n}+1 条一行不丢", len(lines) == n + 1, f"实际 {len(lines)}")
    check("unknown trace_id 仍落盘(留痕与关联解耦)",
          any(json.loads(l)["trace_id"] == "unknown" for l in lines))

    # 打码没在改造中丢失。
    log2 = AuditLogger(path=str(tmp_path()))
    log2.log(new_trace_id(), "assistant", "x", {"phone": "13812345678"})
    rec = json.loads(drain(log2)[0])
    phone = rec["detail"]["phone"]
    check("手机号被掩码", phone.startswith("138") and "****" in phone, str(phone))
    log.close()
    log2.close()


# -------------------------------------------------------------------------- 5


def test_writer_injection_forces_native() -> None:
    print("\n5) 注入自定义 writer 时强制走原生(测试语义不被 OTel 绕过)")
    set_otel(True)  # 即便全局开着 OTel
    captured: list[list[str]] = []

    def writer(lines: list[str]) -> None:
        captured.append(list(lines))

    log = AuditLogger(path=str(tmp_path()), writer=writer)
    check("writer 注入 → _ensure_otel 判不可用", log._ensure_otel() is False,
          f"state={log._otel_state}")
    for i in range(5):
        log.log(f"trace{i}", "assistant", "kb_retrieved", {"n": i}, "sess")
    assert log.flush(timeout=5.0) is not False
    total = sum(len(b) for b in captured)
    check("5 条都被注入的 writer 收到", total == 5, f"实际 {total}")
    check("写入是批量而非逐条", len(captured) < 5, f"批数 {len(captured)}")
    log.close()


# -------------------------------------------------------------------------- 6


def test_emit_failure_falls_back_to_native() -> None:
    print("\n6) 单条 emit 异常只把那一条降级原生, 一条不丢")
    set_otel(True)
    log = AuditLogger(path=str(tmp_path()))
    check("先确认 OTel 可用", log._ensure_otel() is True, f"state={log._otel_state}")
    real_emit = log._emit_otel

    # 第一条模拟 emit 抛异常(_emit_otel 内部会吞并返回 False), 其余正常走 OTel。
    calls = {"i": 0}

    def flaky_emit(*a, **k):
        calls["i"] += 1
        if calls["i"] == 1:
            # 直接把 OTel logger 换成坏对象逼 _emit_otel 走 except 分支返回 False。
            good = log._otel_logger
            log._otel_logger = None  # 触发 AttributeError → 被内部 except 捕获
            try:
                return real_emit(*a, **k)
            finally:
                log._otel_logger = good
        return real_emit(*a, **k)

    log._emit_otel = flaky_emit
    for i in range(3):
        log.log(f"trace{i}", "assistant", "a", {"n": i}, "sess")
    lines = drain(log)
    check("坏的那条 + 正常两条都在", len(lines) == 3, f"实际 {len(lines)}")
    ids = sorted(json.loads(l)["detail"]["n"] for l in lines)
    check("覆盖 0/1/2 无缺号", ids == [0, 1, 2], str(ids))
    log.close()


# -------------------------------------------------------------------------- 7


def test_close_flushes_otel_batch() -> None:
    print("\n7) close() 必须先冲 OTel 攒批(纯 OTel 路径下原生线程一直为 None)")
    set_otel(True)
    log = AuditLogger(path=str(tmp_path()))
    log.log(new_trace_id(), "assistant", "a", {})  # 不显式 flush, 只 close
    log.log("t2", "assistant", "b", {})
    log.close()
    lines = log.path.read_text(encoding="utf-8").splitlines()
    check("close 前未 flush 的两条被 shutdown 刷出", len(lines) == 2, f"实际 {len(lines)}")

    # 关停后的迟到记录仍同步落盘(不静默丢)。
    log.log("t3", "assistant", "c", {})
    after = log.path.read_text(encoding="utf-8").splitlines()
    check("关停后迟到记录不丢", len(after) == 3 and json.loads(after[2])["trace_id"] == "t3",
          f"实际 {len(after)}")


def main() -> int:
    try:
        for fn in (
            test_new_trace_id_is_w3c,
            test_langfuse_alignment_is_derivation,
            test_otel_path_byte_format_matches_native,
            test_otel_counts_and_masking,
            test_writer_injection_forces_native,
            test_emit_failure_falls_back_to_native,
            test_close_flushes_otel_batch,
        ):
            fn()
    finally:
        set_otel(True)  # 复位, 别把单例留在关闭态影响同进程后续
    print(f"\n通过 {PASS} 项, 失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
