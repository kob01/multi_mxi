"""智能问数在线自检: 必须打**容器网关**(127.0.0.1:18000), 不允许宿主直跑网关。

跑法::

    ./scripts/dev.ps1 -Build                                  # 先把改动带进镜像
    uv run python -m scripts.test_askdata                     # 打容器网关跑一轮全链路

与 ``scripts/test_askdata_offline.py`` 的分工: 离线那份测的是纯函数(形状/错误码/
编译产物), 这份测的是只有真栈才暴露的东西 —— A2A metadata 里的身份与槽位、载荷剥离、
留痕落库、治理端点的权限、反馈到归因的闭环。宿主 .env 的 ``ASSISTANT_HOST_PORT``
变了就自动跟随, 不用改这里的常量。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

ROLE = "finance"
USER = "E10001"
# 会话号带本次运行的序号: 复用旧会话时, 上一轮的历史会让 rewrite_query 把
# "报销总额是多少"补全成"今年至今的报销总额是多少"(这个行为本身是对的),
# 于是"缺时间应触发澄清"这一例会持续误报。
RUN = time.strftime("%H%M%S")


def gateway_base() -> str:
    """读 docker/.env 的 ASSISTANT_HOST_PORT(容器发布端口), 兜底 18000。"""
    port = "18000"
    for rel in ("docker/.env", "docker/.env.example"):
        path = Path(__file__).resolve().parent.parent / rel
        if path.is_file():
            hit = re.search(r"^ASSISTANT_HOST_PORT=(\d+)", path.read_text(encoding="utf-8"), re.M)
            if hit:
                port = hit.group(1)
                break
    return f"http://127.0.0.1:{port}"


BASE = gateway_base()
PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail[:300]}")


def chat(text: str, *, targets: list[str] | None = None, session: str = "", user: str = USER) -> dict:
    resp = httpx.post(f"{BASE}/api/chat", json={
        "session_id": f"selftest-{session or 'x'}-{RUN}", "user_id": user, "role": ROLE,
        "department": "研发部", "message": text, "thinking": False,
        "agent_targets": targets or [],
    }, timeout=300)
    resp.raise_for_status()
    return resp.json()


def askdata_of(data: dict) -> dict:
    return (data.get("metadata") or {}).get("askdata") or {}


def main() -> int:
    print(f"目标网关: {BASE}(容器)  角色: {ROLE}  工号: {USER}  会话后缀: {RUN}")

    health = httpx.get(f"{BASE}/api/health", timeout=30)
    check("网关容器可达", health.status_code == 200, health.text[:120])
    if health.status_code != 200:
        print("网关没起来, 先跑 ./scripts/dev.ps1 -Build", file=sys.stderr)
        return 2

    agents = httpx.get(f"{BASE}/api/agents", params={"role": ROLE}, timeout=30).json()
    check("/api/agents 含 AskData_Agent", any(a.get("domain") == "askdata" for a in agents),
          json.dumps(agents, ensure_ascii=False)[:200])
    employee_agents = httpx.get(f"{BASE}/api/agents", params={"role": "employee"}, timeout=30).json()
    check("普通员工看不到 AskData", all(a.get("domain") != "askdata" for a in employee_agents))

    multi = chat("各部门今年差旅费金额", targets=["askdata"], session="selftest-multi")
    ask = askdata_of(multi)
    check("多智能体点选路径带出结构化载荷", ask.get("kind") == "result",
          json.dumps(ask, ensure_ascii=False)[:200])
    check("多智能体正文没漏围栏", "```askdata" not in (multi.get("answer") or ""))
    check("下钻返回了数据行", int(ask.get("rowcount") or 0) > 0, f"rowcount={ask.get('rowcount')}")
    check("口径与可见范围随答案回来",
          bool(ask.get("caliber_notes")) and bool(ask.get("scope_note")))
    chart_url = ((ask.get("chart") or {}).get("url")) or ""
    check("图型按结果形状给出并落盘", bool(chart_url), chart_url)
    if chart_url:
        got = httpx.get(f"{BASE}{chart_url}", timeout=60)
        check("图表 URL 可回取(SVG)", got.status_code == 200 and "<svg" in got.text[:400].lower(),
              f"HTTP {got.status_code}")

    single = chat("各部门今年报销总额，并做环比", session="selftest-single")
    ask1 = askdata_of(single)
    check("单意图委派也带出载荷(路径=问数)", ask1.get("kind") == "result"
          and ask1.get("route") in ("metric_api", "compiler"),
          json.dumps(ask1, ensure_ascii=False)[:200])
    check("同环比走编译器且返回对比列",
          "m_expense_total_mom_pct" in (ask1.get("columns") or []), str(ask1.get("columns")))
    check("留痕号已回传(反馈链路前提)", bool(ask1.get("query_id")))

    clarify = chat("报销总额是多少", targets=["askdata"], session="selftest-clarify")
    ask2 = askdata_of(clarify)
    check("缺时间触发选项式澄清", ask2.get("kind") == "clarify" and bool(ask2.get("times")),
          json.dumps(ask2, ensure_ascii=False)[:200])
    check("澄清给的是候选口径而非开放式反问", bool(ask2.get("metrics")))
    confirm = chat("问数确认：报销总额是多少，口径用「报销总额」(m_expense_total)，统计区间用「今年至今」(this_year)",
                   targets=["askdata"], session="selftest-clarify")
    ask3 = askdata_of(confirm)
    check("澄清点选后按确定口径出数", ask3.get("kind") == "result",
          json.dumps(ask3, ensure_ascii=False)[:200])
    check("确认轮次走指标 API(无自定义谓词)", ask3.get("route") == "metric_api", str(ask3.get("route")))

    unknown = chat("上周每一单外卖的金额中位数", targets=["askdata"], session="selftest-unknown")
    ask4 = askdata_of(unknown)
    check("未登记口径不硬给数字", ask4.get("kind") in ("clarify", "refuse"),
          json.dumps(ask4, ensure_ascii=False)[:200])

    denied = httpx.get(f"{BASE}/api/askdata/stats", params={"role": "employee", "operator": "E9"}, timeout=30)
    check("看板对普通员工 403", denied.status_code == 403, f"HTTP {denied.status_code}")
    stats = httpx.get(f"{BASE}/api/askdata/stats", params={"role": ROLE, "operator": USER, "days": 2}, timeout=30)
    body = stats.json()
    check("看板聚合出全部关键指标",
          all(k in body for k in ("first_pass_rate", "clarify_rate", "degrade_rate",
                                 "p95_latency_ms", "thumbs_down_rate", "total")),
          json.dumps(body, ensure_ascii=False)[:200])
    check("本轮问数已进留痕统计", int(body.get("total") or 0) > 0, str(body.get("total")))
    sem = httpx.get(f"{BASE}/api/askdata/semantic", params={"role": ROLE, "operator": USER}, timeout=30).json()
    check("语义层快照来自库(不是兜底集)", (sem.get("catalog") or {}).get("origin") == "db",
          json.dumps(sem.get("catalog"), ensure_ascii=False)[:200])

    qid = ask1.get("query_id") or ""
    if qid:
        fb = httpx.post(f"{BASE}/api/askdata/feedback", json={
            "query_id": qid, "kind": "down", "user_id": USER, "note": "自检反馈",
            "corrected_dsl": {"metrics": [{"id": "m_expense_count"}]}}, timeout=30)
        fbody = fb.json()
        check("反馈落库并给出归因类别",
              fb.status_code == 200 and fbody.get("category") in
              ("intent", "link", "dsl", "exec", "render"), json.dumps(fbody, ensure_ascii=False)[:200])
        other = httpx.post(f"{BASE}/api/askdata/feedback", json={
            "query_id": qid, "kind": "up", "user_id": "E10099"}, timeout=30)
        check("不能替别人问的记录下反馈", other.status_code == 403, f"HTTP {other.status_code}")
        bad = httpx.get(f"{BASE}/api/askdata/badcases",
                        params={"role": ROLE, "operator": USER, "days": 3, "limit": 5}, timeout=30).json()
        check("badcase 清单可回查且问句是原话",
              int(bad.get("count") or 0) >= 1
              and not str((bad.get("badcases") or [{}])[0].get("question")).startswith("对话背景"),
              json.dumps(bad, ensure_ascii=False)[:200])
    else:
        check("反馈链路(缺 query_id 无法验证)", False)

    print(f"\n合计: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
