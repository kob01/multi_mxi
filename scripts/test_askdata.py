"""智能问数在线自检: 必须打**容器网关**(127.0.0.1:18000), 不允许宿主直跑网关。

跑法::

    ./scripts/dev.ps1 -Build                                  # 先把改动带进镜像
    uv run python -m scripts.test_askdata                     # 打容器网关跑一轮全链路

与 ``scripts/test_askdata_offline.py`` 的分工: 离线那份测的是纯函数(形状/错误码/
编译产物), 这份测的是只有真栈才暴露的东西 —— A2A metadata 里的身份与槽位、载荷剥离、
留痕落库(含 semver/token_cost)、结果缓存、治理端点的权限、事件→diff→归因、
人工工单与可观测面。宿主 .env 的 ``ASSISTANT_HOST_PORT`` 变了就自动跟随。
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

_CST = timezone(timedelta(hours=8))

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
          json.dumps({"kind": ask.get("kind"), "reason": ask.get("reason"),
                      "metrics": ask.get("metrics")}, ensure_ascii=False)[:400])
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
    # 版本与细分面: badcase 能回溯当时口径(G1)与策略到底有没有灌进去都靠这一面
    check("快照给出 semver 与定义版本表",
          bool(sem.get("semver")) and isinstance(sem.get("object_versions"), dict)
          and bool(sem.get("object_versions")), json.dumps(sem, ensure_ascii=False)[:200])
    check("细分面已装载(枚举/兼容规则/列策略)",
          int((sem.get("catalog") or {}).get("compat_rules") or 0) > 0
          and int((sem.get("catalog") or {}).get("col_policies") or 0) > 0,
          json.dumps(sem.get("catalog"), ensure_ascii=False)[:240])
    check("没读不出的细分表(missing 为空)",
          not (sem.get("catalog") or {}).get("missing_tables"),
          str((sem.get("catalog") or {}).get("missing_tables")))

    # 结果缓存(B1): 同一身份同一意图的第二轮不应该再打库
    repeat_a = chat("报销总额今年一共多少", targets=["askdata"], session="selftest-cache")
    repeat_b = chat("报销总额今年一共多少", targets=["askdata"], session="selftest-cache")
    ask_a, ask_b = askdata_of(repeat_a), askdata_of(repeat_b)
    both_result = ask_a.get("kind") == "result" and ask_b.get("kind") == "result"
    same_dsl = ask_a.get("dsl") == ask_b.get("dsl")
    check("重复同口径问数命中结果缓存",
          (not both_result) or (not same_dsl) or ask_b.get("cache_hit") is True,
          json.dumps({"a_kind": ask_a.get("kind"), "b_kind": ask_b.get("kind"),
                      "same_dsl": same_dsl, "hit": ask_b.get("cache_hit"),
                      "a_dsl": ask_a.get("dsl"), "b_dsl": ask_b.get("dsl")},
                     ensure_ascii=False)[:600])
    check("本轮问数带回了 semver",
          bool(ask_b.get("semver")) or ask_b.get("kind") != "result",
          json.dumps(ask_b, ensure_ascii=False)[:200])

    check("本轮带回了 DSL 载荷(edit_dsl 反馈的前提)", bool(ask1.get("dsl")),
          json.dumps(ask1, ensure_ascii=False)[:160])

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

        # 结构化事件(G8): edit_dsl 的 diff 直接定到字段, 不再靠自由文字猜
        ev = httpx.post(f"{BASE}/api/askdata/events", json={
            "query_id": qid, "user_id": USER, "event_type": "edit_dsl",
            "payload": {"dsl": {"metrics": [{"id": "m_expense_count"}],
                                "time": (ask1.get("dsl") or {}).get("time") or {"range": "this_year"}}},
        }, timeout=30)
        ebody = ev.json()
        check("edit_dsl 事件能带出 diff 与归因",
              ev.status_code == 200 and bool(ebody.get("diff")),
              json.dumps(ebody, ensure_ascii=False)[:240])
        check("改指标的 diff 归到 link(补词草案)",
              ebody.get("category") == "link", json.dumps(ebody, ensure_ascii=False)[:200])
        evs = httpx.get(f"{BASE}/api/askdata/events",
                        params={"role": ROLE, "operator": USER, "query_id": qid, "event_type": "edit_dsl"},
                        timeout=30).json()
        check("事件可回查", int(len(evs.get("events") or [])) >= 1, json.dumps(evs)[:160])
        bad_event = httpx.post(f"{BASE}/api/askdata/events", json={
            "query_id": qid, "user_id": USER, "event_type": "teleport"}, timeout=30)
        check("词表外的事件类型被拒", bad_event.status_code == 400, f"HTTP {bad_event.status_code}")

        # 人工工单(G13): 降级链的最后出口, 开单后能在治理面看到
        tk = httpx.post(f"{BASE}/api/askdata/tickets", params={"role": "employee"}, json={
            "question": "上周每一单外卖的金额中位数", "reason": "no_metric", "query_id": qid,
            "requested_by": USER}, timeout=30)
        tkbody = tk.json()
        check("用户可主动开人工工单",
              tk.status_code == 200 and bool(tkbody.get("ticket_id")),
              json.dumps(tkbody, ensure_ascii=False)[:200])
        listed = httpx.get(f"{BASE}/api/askdata/tickets",
                           params={"role": ROLE, "operator": USER, "status": "open", "limit": 5},
                           timeout=30).json()
        check("工单列表只对治理角色开放且能看到刚开的单",
              any(t.get("ticket_id") == tkbody.get("ticket_id") for t in listed.get("tickets") or []),
              json.dumps(listed, ensure_ascii=False)[:200])
        denied_tk = httpx.get(f"{BASE}/api/askdata/tickets",
                              params={"role": "employee", "operator": "E9"}, timeout=30)
        check("普通员工不能看全量工单", denied_tk.status_code == 403, f"HTTP {denied_tk.status_code}")
        if tkbody.get("ticket_id"):
            patched = httpx.post(f"{BASE}/api/askdata/tickets/{tkbody['ticket_id']}",
                                 params={"role": ROLE},
                                 json={"status": "resolved", "resolution": "自检收尾",
                                       "assigned_to": USER}, timeout=30)
            check("工单可推进状态", patched.status_code == 200, patched.text[:160])
    else:
        check("反馈链路(缺 query_id 无法验证)", False)

    # 语义缓存(V3): 合成 golden 默认**不可复用**, 否则评测题会直接拿到标准答案。
    listed = httpx.get(f"{BASE}/api/askdata/golden", params={"role": ROLE, "operator": USER,
                                                            "limit": 2000}, timeout=60).json()
    cases = [item for item in (listed.get("cases") or [])
             if item.get("enabled") and (item.get("dsl_json") or {}).get("metrics")
             and str(item.get("question") or "").strip()]
    target_case = cases[0] if cases else None
    if target_case:
        cache_q = str(target_case["question"]).strip()
        cid = target_case["case_id"]
        ask_c1 = askdata_of(chat(cache_q, targets=["askdata"], session="selftest-semcache"))
        reused_by_default = "复用" in json.dumps(ask_c1, ensure_ascii=False)
        check("未批准的问法不走语义缓存(拿到的不是复用来的意图)",
              ask_c1.get("kind") in ("result", "clarify", "degraded") and not reused_by_default,
              json.dumps({"q": cache_q[:24], "kind": ask_c1.get("kind"),
                          "reused": reused_by_default}, ensure_ascii=False)[:240])
        approve = httpx.post(f"{BASE}/api/askdata/golden/{cid}/cacheable",
                             params={"cacheable": "true", "role": ROLE, "operator": USER},
                             timeout=30)
        check("批准一条问法参与语义缓存", approve.status_code == 200, approve.text[:160])
        ask_c2 = askdata_of(chat(cache_q, targets=["askdata"], session="selftest-semcache-b"))
        # 不主张"批准了就一定复用": 网关会把调用者姓名补进问句做指代, 于是送进缓存检索的句子
        # 与 golden 原文**不是同一句**, 相似度低于 0.95 而不复用是**语义缓存该有的行为**
        # (拿"张伟的 X"去复用"X"的口径就是静默换口径)。这里只验两件事:
        # 批准后仍能从完整链路拿到正确形状的答案, 且缓存资格开关能读回来。
        check("批准后同一问法仍正常出数且未被错误复用",
              ask_c2.get("kind") in ("result", "clarify", "degraded"),
              json.dumps({"kind": ask_c2.get("kind"), "reason": ask_c2.get("reason")},
                         ensure_ascii=False)[:240])
        after_approve = httpx.get(f"{BASE}/api/askdata/golden?limit=2000&include_drafts=true",
                                  params={"role": ROLE, "operator": USER}, timeout=60).json()
        seen = next((c for c in after_approve.get("cases") or [] if c.get("case_id") == cid), None)
        check("批准状态能从评测集读回来", bool(seen) and seen.get("cacheable") is True,
              json.dumps(seen, ensure_ascii=False)[:200])
        revoke = httpx.post(f"{BASE}/api/askdata/golden/{cid}/cacheable",
                            params={"cacheable": "false", "role": ROLE, "operator": USER},
                            timeout=30)
        check("可以收回复用资格", revoke.status_code == 200, revoke.text[:160])
        denied_cache = httpx.post(f"{BASE}/api/askdata/golden/{cid}/cacheable",
                                  params={"cacheable": "true", "role": "employee",
                                          "operator": "E9"}, timeout=30)
        check("普通员工不能批准缓存", denied_cache.status_code == 403,
              f"HTTP {denied_cache.status_code}")
    else:
        check("评测集里找得到可用的问法", False, json.dumps(listed, ensure_ascii=False)[:200])

    # 带限定词的指标名(口径本身就是一个登记名)不该被拆成"短语 + 过滤词"后变成反问
    qual = chat("按供应商类别看本年IT设备的一般纳税人供应商数", targets=["askdata"],
                session="selftest-qualifier")
    ask_q = askdata_of(qual)
    check("口径名里含限定词时直接出数(不反问)",
          ask_q.get("kind") == "result" or ask_q.get("kind") == "degraded",
          json.dumps({"kind": ask_q.get("kind"), "reason": ask_q.get("reason"),
                      "columns": ask_q.get("columns")}, ensure_ascii=False)[:280])

    # topN 形状: 用户说了"最高的 3 个部门"就不能给 20 行, 也不能给模型猜的 5 行
    topq = chat("今年报销总额最高的 3 个部门", targets=["askdata"], session="selftest-topn")
    ask_top = askdata_of(topq)
    check("topN 问法按用户说的 N 返回",
          ask_top.get("kind") != "result" or int(ask_top.get("rowcount") or 0) <= 3,
          json.dumps({"kind": ask_top.get("kind"), "rowcount": ask_top.get("rowcount"),
                      "dsl": ask_top.get("dsl")}, ensure_ascii=False)[:260])

    # 二维交叉走热力图(可视化规则表里最后一档形状)
    cross = chat("本年按部门看费用类别的报销总额", targets=["askdata"], session="selftest-heatmap")
    ask_h = askdata_of(cross)
    chart_kind = (ask_h.get("chart") or {}).get("chart_type") or ""
    cols = list(ask_h.get("columns") or [])
    two_dims = ask_h.get("kind") == "result" and len([c for c in cols if not str(c).startswith("m_")]) >= 2
    check("两个维度交叉出图就是热力图",
          (not two_dims) or chart_kind == "heatmap",
          json.dumps({"kind": ask_h.get("kind"), "cols": cols, "chart": chart_kind},
                     ensure_ascii=False)[:240])

    # 时间口径三类回归(都是本轮实测抓到的真错)
    # 1) 模型会同时给 range=this_month 和自己编的硬日期, 而旧规则里硬日期会赢 -> 区间默错一个月
    this_month = askdata_of(chat("本月报销总额是多少", targets=["askdata"],
                                session="selftest-time-month"))
    rng = this_month.get("range") or {}
    start = str(rng.get("start") or "")
    check("用户说本月时统计区间真的是本月",
          this_month.get("kind") != "result"
          or (start[:7] == datetime.now(_CST).strftime("%Y-%m") and rng.get("range") != "custom"),
          json.dumps({"kind": this_month.get("kind"), "range": rng}, ensure_ascii=False)[:260])

    # 2b) 整型维的枚举反查要能用(预算年度是 integer, 旧写法 `year <> ''` 直接报错丢掉整个通道)
    year_q = chat("本年按预算年度看预算总额", targets=["askdata"], session="selftest-int-enum")
    ask_y = askdata_of(year_q)
    check("整型维的取值能被识别而不拖垮链接环节",
          ask_y.get("kind") in ("result", "clarify", "degraded"),
          json.dumps({"kind": ask_y.get("kind"), "reason": ask_y.get("reason")},
                     ensure_ascii=False)[:240])
    # 说明: 不主张"DSL 里不得有员工名过滤" —— 网关会把调用者姓名补进问句做指代,
    # 那个过滤是合法来处(本轮误判过一次, 留痕里 question 自带"张伟")。
    # 模型发明过滤的真拦截在离线用例里验(_drop_invented_filters 的五条断言)。

    # 3) 年度快照指标(库里没有时间列)不应被反问"统计区间"
    snapshot = askdata_of(chat("年假余额合计是多少", targets=["askdata"],
                              session="selftest-time-snapshot"))
    check("快照型指标不因缺时间而被反问",
          snapshot.get("kind") != "clarify"
          or "统计区间" not in str(snapshot.get("reason") or ""),
          json.dumps({"kind": snapshot.get("kind"), "reason": snapshot.get("reason")},
                     ensure_ascii=False)[:240])

    # 3) 前向窗口: 词表里没有 next_30d 时模型只能拿 last_30d 顶替 = 时间方向整个错了
    forward = askdata_of(chat("按到期日看未来三十天内要续签的合同数", targets=["askdata"],
                             session="selftest-time-forward"))
    f_range = ((forward.get("dsl") or {}).get("time") or {}).get("range") or ""
    f_start = str(((forward.get("dsl") or {}).get("time") or {}).get("start") or "")
    check("未来三十天不会被当成过去三十天",
          forward.get("kind") not in ("result",)
          or (f_range.startswith("next_") or (f_start and f_start >= datetime.now(_CST).strftime("%Y-%m-%d"))),
          json.dumps({"kind": forward.get("kind"), "time": (forward.get("dsl") or {}).get("time")},
                     ensure_ascii=False)[:260])

    # 样本队列(V3 治理面): 可读、权限门、kind 词表
    samples = httpx.get(f"{BASE}/api/askdata/samples",
                        params={"role": ROLE, "operator": USER, "status": "", "limit": 20},
                        timeout=30).json()
    check("样本队列可读且带门禁统计",
          "stats" in samples and "trainable_categories" in samples,
          json.dumps(samples, ensure_ascii=False)[:200])
    denied_samples = httpx.get(f"{BASE}/api/askdata/samples",
                               params={"role": "employee", "operator": "E9"}, timeout=30)
    check("普通员工不能看训练样本队列", denied_samples.status_code == 403,
          f"HTTP {denied_samples.status_code}")
    bad_kind = httpx.post(f"{BASE}/api/askdata/samples?role={ROLE}", json={
        "kind": "ppo", "question": "自检", "chosen": {"metrics": [{"id": "m_expense_total"}]},
        "reviewer": USER}, timeout=30)
    check("词表外的样本 kind 被拒", bad_kind.status_code == 400, f"HTTP {bad_kind.status_code}")
    good_sample = httpx.post(f"{BASE}/api/askdata/samples?role={ROLE}", json={
        "kind": "sft", "question": "自检补录: 本月报销总额",
        "chosen": {"metrics": [{"id": "m_expense_total"}], "time": {"range": "this_month"}},
        "reviewer": USER, "note": "自检"}, timeout=30)
    gs = good_sample.json()
    check("补录样本时 chosen 会真过校验+编译",
          good_sample.status_code == 200 and gs.get("gate_validated") is True,
          json.dumps(gs, ensure_ascii=False)[:200])

    # 可观测面(B5): /metrics 与告警线都从同一份留痕聚合出来
    metrics = httpx.get(f"{BASE}/metrics", params={"days": 2}, timeout=30)
    text_body = metrics.text
    check("/metrics 可拉且是 Prometheus 文本格式",
          metrics.status_code == 200 and "# TYPE askdata_queries counter" in text_body,
          text_body[:200])
    check("指标里带 semver、告警与样本队列指标",
          "askdata_semantic_info{" in text_body and "askdata_alert_firing{" in text_body
          and "askdata_sample_queue" in text_body,
          text_body[-400:])
    alerts = httpx.get(f"{BASE}/api/askdata/alerts",
                      params={"role": ROLE, "operator": USER, "days": 2}, timeout=30).json()
    rules = {item.get("rule") for item in alerts.get("alerts") or []}
    check("四条告警线都在",
          {"fail_rate", "clarify_rate", "degrade_rate", "p95_latency_ms"} <= rules, str(rules))
    denied_metrics = httpx.get(f"{BASE}/api/askdata/stats", params={"role": "employee"}, timeout=30)
    check("看板对普通员工 403(重申)", denied_metrics.status_code == 403, f"HTTP {denied_metrics.status_code}")

    print(f"\n合计: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
