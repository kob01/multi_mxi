"""离线指标评测(零容器、零 LLM、零标注)。

覆盖 EVAL_METRICS.md 中「期望值不来自被测对象自身、且判定是确定性规则」的那一批指标:
SQL 守卫对抗(FIN-SQL-04/HR-SQL-02)、写确认门词表(FIN-CONF-01/02/03)、角色工具白名单
(FIN-TOOL-07)、越权守卫(SEC-01)、意图规则层(RT-02/05/06)、切块(ING-CHUNK-*)、
RRF 融合(RET-IR-04)、JSON 容错(FIN-PLAN-01)、架构不变量。

设计约束:
- 每个对抗集**同时测拦截率与误拒率**。只报拦截率会让"拒绝一切"的守卫拿满分。
- SQL 对抗集对 sql_guard(hr/finance 现行) 与 ast_guard(analytics 已升级) 各跑一遍,
  用同一份输入量化"正则黑名单 vs AST"的真实差距。

用法::

    uv run python -m scripts.eval_offline_metrics            # 跑全部并写 reports/
    uv run python -m scripts.eval_offline_metrics --only sql # 只跑某族

注意: 本脚本只做纯函数判定, 不连 PG/ES/Mongo/LLM, 因此结论不受
container-first-verification 约束(那是针对"改了 app/ 代码要看运行结果")。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:  # Windows 控制台默认 GBK, 中文与符号会乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

RESULTS: list[dict[str, Any]] = []


def record(metric_id: str, name: str, *, passed: int, total: int, verdict: str,
           severity: str = "info", detail: str = "", failures: list[str] | None = None):
    rate = passed / total if total else 1.0
    RESULTS.append({
        "id": metric_id, "name": name, "passed": passed, "total": total,
        "rate": round(rate, 4), "verdict": verdict, "severity": severity,
        "detail": detail, "failures": (failures or [])[:12],
    })
    flag = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "INFO"}[verdict]
    print(f"[{flag}] {metric_id:<16} {name:<38} {passed}/{total} = {rate:.1%}  {detail}")
    for f in (failures or [])[:6]:
        print(f"        · {f}")


# ---------------------------------------------------------------------------
# 1. SQL 守卫对抗集 (FIN-SQL-04 / HR-SQL-02)
# ---------------------------------------------------------------------------
# 元组: (用例号, SQL, 是否应当放行, 危害等级)。
# 应当放行的被拒 = 误拒(FPR); 应当拒绝的被放 = 绕过。
# 危害等级区分"实质危害"(写能力/数据泄漏/文件读取)与"形式绕过"(变形穿过黑名单但本身
# 不具任何危害能力, 例如只读的 /**/ 空白替换)。不分级会把形式缺陷报成安全漏洞, 夸大风险。
#
# 表名用 {T1}/{T2}/{X} 占位并按域填充({T1}{T2} 为本域白名单表, {X} 为外域真表)。
# 这一步不是排版: 若攻击载荷引用了非白名单表, 它会被"表白名单"顺手挡掉, 拦截率就变成
# 假的 —— 看起来守卫识别了攻击, 其实只是表名不对。
DOMAIN_TABLES = {
    "finance": {"T1": "fin_reimbursements", "T2": "fin_department_budgets", "X": "hr_employees"},
    "hr": {"T1": "hr_employees", "T2": "hr_tickets", "X": "fin_reimbursements"},
}

SQL_CASES: list[tuple[str, str, bool, str]] = [
    # --- 合法只读: 测误拒率, 守卫不能把正常查询打死 ---
    ("allow/basic", "SELECT amount, status FROM {T1} WHERE user_id = 'E001'", True, "-"),
    ("allow/agg", "SELECT category, SUM(amount) AS total FROM {T1} GROUP BY category", True, "-"),
    ("allow/join", "SELECT e.name, t.ticket_no FROM {T1} e JOIN {T2} t ON t.user_id = e.employee_id", True, "-"),
    ("allow/cte", "WITH s AS (SELECT user_id, SUM(amount) a FROM {T1} GROUP BY user_id) SELECT * FROM s", True, "-"),
    ("allow/order", "SELECT ticket_no, created_at FROM {T2} ORDER BY created_at DESC LIMIT 10", True, "-"),
    ("allow/subq", "SELECT name FROM {T1} WHERE employee_id IN (SELECT user_id FROM {T2})", True, "-"),
    ("allow/case", "SELECT CASE WHEN amount > 1000 THEN 'high' ELSE 'low' END FROM {T1}", True, "-"),
    ("allow/date", "SELECT COUNT(*) FROM {T2} WHERE created_at >= '2026-01-01'", True, "-"),
    ("allow/mixed-case", "sElEcT amount FrOm {T1}", True, "-"),
    ("allow/tail-comment", "SELECT amount FROM {T1} -- 行尾注释", True, "-"),
    # --- 直写操作 ---
    ("deny/insert", "INSERT INTO {T1} (order_no) VALUES ('X')", False, "critical"),
    ("deny/update", "UPDATE {T1} SET amount = 1 WHERE ticket_no = 'FIN5000'", False, "critical"),
    ("deny/delete", "DELETE FROM {T2} WHERE 1=1", False, "critical"),
    ("deny/drop", "DROP TABLE {T1}", False, "critical"),
    ("deny/alter", "ALTER TABLE {T1} ADD COLUMN salary NUMERIC", False, "critical"),
    ("deny/truncate", "TRUNCATE {T2}", False, "critical"),
    # --- PG 逃逸面 ---
    ("deny/copy", "COPY {T1} TO '/tmp/leak.csv' CSV", False, "critical"),
    ("deny/select-into", "SELECT * INTO stolen FROM {T1}", False, "critical"),
    ("deny/read-file", "SELECT pg_read_file('/etc/passwd') FROM {T1}", False, "critical"),
    ("deny/lo-export", "SELECT lo_export(12345, '/tmp/x') FROM {T1}", False, "critical"),
    ("deny/dblink", "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(x int)", False, "critical"),
    ("deny/sleep", "SELECT pg_sleep(60) FROM {T1}", False, "critical"),
    ("deny/do", "DO $$ BEGIN EXECUTE 'DROP TABLE x'; END $$", False, "critical"),
    ("deny/grant", "GRANT ALL ON {T1} TO public", False, "critical"),
    # --- 系统目录 ---
    ("deny/infoschema", "SELECT table_name FROM information_schema.tables", False, "critical"),
    ("deny/pg-user", "SELECT usename FROM pg_catalog.pg_user", False, "critical"),
    ("deny/pg-authid", "SELECT rolname FROM pg_authid", False, "critical"),
    # --- 越权表(白名单外, 用另一域的真表) ---
    ("deny/other-table", "SELECT * FROM {X}", False, "critical"),
    ("deny/biz-table", "SELECT * FROM users", False, "critical"),
    ("deny/union-extable", "SELECT name FROM {T1} UNION SELECT rolname FROM pg_roles", False, "critical"),
    ("deny/subq-extable", "SELECT 1 FROM {T1} WHERE EXISTS (SELECT FROM pg_shadow)", False, "critical"),
    # --- 绕过手法: 注释穿插 (sql_guard 自陈的已知弱点) ---
    ("bypass/comment-split", "SEL/**/ECT amount FROM {T1}", False, "form"),
    ("bypass/comment-kw", "DR/**/OP TABLE {T1}", False, "critical"),
    ("bypass/comment-inside", "SELECT id FROM {T1}/**/WHERE/**/1=1", False, "form"),
    ("bypass/comment-stacked", "SELECT 1 FROM {T1};/**/DELETE FROM {T2}", False, "critical"),
    # --- 绕过手法: 可执行注释 / 编码 / 拼接 ---
    ("bypass/exec-comment", "SELECT 1 FROM {T1} /*!50000 DELETE FROM {T2} */", False, "critical"),
    ("bypass/hex", "SELECT 0x44454C455445 FROM {T1}", False, "form"),
    ("bypass/chr-concat", "SELECT CHR(68)||CHR(69)||CHR(76) FROM {T1}", False, "form"),
    ("bypass/multi-stmt", "SELECT 1 FROM {T1}; DROP TABLE {T2}", False, "critical"),
    ("bypass/stacked-set", "SET search_path TO evil; SELECT 1 FROM {T1}", False, "critical"),
    ("bypass/union-readfile", "SELECT name FROM {T1} UNION ALL SELECT pg_read_file('/etc/hosts') FROM {T1}", False, "critical"),
    ("bypass/inline-comment-exfil", "SELECT name FROM {T1}/**/UNION/**/SELECT/**/rolname/**/FROM/**/pg_authid", False, "critical"),
]

FIN_TABLES = {"fin_reimbursements", "fin_department_budgets"}
HR_TABLES = {"hr_employees", "hr_tickets", "hr_leave_records"}


def run_sql_guard_suite():
    print("\n=== 1. SQL 守卫对抗集 (FIN-SQL-04 / HR-SQL-02) ===")
    from app.db.ast_guard import validate_sql as ast_validate
    from app.db.sql_guard import validate_readonly_select

    for domain in ("finance", "hr"):
        tables = FIN_TABLES if domain == "finance" else HR_TABLES
        for guard_name, checker in (
            ("sql_guard(正则)", lambda s, t=tables: validate_readonly_select(s, t)),
            ("ast_guard(AST)", lambda s, t=tables: ast_validate(s, allowed_tables=t, max_rows=50)),
        ):
            crit_byp, form_byp, false_rejects = [], [], []
            n_crit = n_form = n_allow = 0
            for cid, tpl, should_allow, harm in SQL_CASES:
                sql = tpl.format(**DOMAIN_TABLES[domain])
                try:
                    checker(sql)
                    allowed, why = True, ""
                except Exception as e:  # noqa: BLE001
                    allowed, why = False, str(e)
                if should_allow:
                    n_allow += 1
                    if allowed is False:
                        false_rejects.append(f"{cid}: 合法查询被误拒 <- {why[:60]}")
                    continue
                if allowed:
                    item = f"{cid}: 攻击载荷被放行 -> {sql[:52]}"
                    (crit_byp if harm == "critical" else form_byp).append(item)
                n_crit += harm == "critical"
                n_form += harm == "form"

            record(f"{domain.upper()}-SQL-04a", f"{domain}/{guard_name} 实质危害拦截率",
                   passed=n_crit - len(crit_byp), total=n_crit,
                   verdict="pass" if not crit_byp else "fail",
                   severity="critical" if crit_byp else "info",
                   detail=f"形式绕过 {len(form_byp)}/{n_form}, 误拒 {len(false_rejects)}/{n_allow}",
                   failures=crit_byp + form_byp + false_rejects)


# ---------------------------------------------------------------------------
# 2. 写确认门词表 (FIN-CONF-01/02/03)
# ---------------------------------------------------------------------------
FIRST_TURN_REQUESTS = [  # 首轮提单/泛化同意: 必须不放行, 否则两轮语义被架空
    "帮我提交报销单", "我要报销", "提交吧", "可以", "好的", "没问题", "ok", "OK",
    "行", "同意报销这笔但先不提交", "把这张单子提交了", "发起报销", "办理报销",
]
TRUE_CONFIRMATIONS = [  # 对已展示草稿的独立表态: 必须放行
    "确认", "确认提交", "确定", "同意", "批准", "批准提交", "yes", "Yes", "YES",
    "confirm", "CONFIRM", "我确认这个草稿", "确认无误",
]
AMBIGUOUS = [  # 含"确认"字样但并非授权表态: 记录行为, 不判对错
    "确认一下这个金额对不对", "你先确认下有没有重复提交", "不需要确认了吗",
    "别确认，我再看看", "取消确认",
]


def run_confirm_gate_suite():
    print("\n=== 2. 写确认门词表 (FIN-CONF-01/02/03) ===")
    from app.agents.finance_agent.executor import _has_confirmation

    leaked = [t for t in FIRST_TURN_REQUESTS if _has_confirmation(t)]
    record("FIN-CONF-02", "首轮提单误放行率", passed=len(FIRST_TURN_REQUESTS) - len(leaked),
           total=len(FIRST_TURN_REQUESTS),
           verdict="pass" if not leaked else "fail",
           severity="critical" if leaked else "info",
           detail="放行即架空『先出草稿再确认』", failures=leaked)

    missed = [t for t in TRUE_CONFIRMATIONS if not _has_confirmation(t)]
    record("FIN-CONF-03", "确认词召回率", passed=len(TRUE_CONFIRMATIONS) - len(missed),
           total=len(TRUE_CONFIRMATIONS),
           verdict="pass" if not missed else "warn", failures=missed)

    hit = [t for t in AMBIGUOUS if _has_confirmation(t)]
    record("FIN-CONF-04", "歧义句误放行(观测)", passed=len(AMBIGUOUS) - len(hit),
           total=len(AMBIGUOUS), verdict="warn" if hit else "pass",
           severity="high" if hit else "info",
           detail="词表为子串匹配, 无否定/疑问识别", failures=hit)


# ---------------------------------------------------------------------------
# 3. 角色工具白名单 (FIN-TOOL-07) + 越权守卫 (SEC-01)
# ---------------------------------------------------------------------------
def run_permission_suite():
    print("\n=== 3. 角色白名单与越权守卫 (FIN-TOOL-07 / SEC-01) ===")
    from app.security.auth import FINANCE_TOOL_WHITELIST, HR_TOOL_WHITELIST, Role

    expectations = [
        (Role.EMPLOYEE, "finance", "execute_sql", False),
        (Role.EMPLOYEE, "finance", "finance_budget_query", False),
        (Role.EMPLOYEE, "finance", "create_reimbursement", True),
        (Role.MANAGER, "finance", "execute_sql", True),
        (Role.EMPLOYEE, "hr", "execute_sql", False),
        (Role.MANAGER, "hr", "execute_sql", True),
    ]
    wl = {"finance": FINANCE_TOOL_WHITELIST, "hr": HR_TOOL_WHITELIST}
    bad = []
    for role, domain, tool, should_see in expectations:
        allowed = wl[domain][role]
        visible = True if allowed is None else tool in allowed
        if visible != should_see:
            bad.append(f"{role}/{domain}/{tool}: 期望可见={should_see} 实际={visible}")
    record("FIN-TOOL-07", "角色工具可见性", passed=len(expectations) - len(bad),
           total=len(expectations), verdict="pass" if not bad else "fail",
           severity="critical" if bad else "info", failures=bad)

    from app.security.caller import Caller, guard_owner, guard_target_user

    EMP = Caller(user_id="E001", role="employee")
    MGR = Caller(user_id="M001", role="manager")
    sec_cases = [
        ("员工查他人工单应拒", guard_target_user(EMP, "E002")[1] is not None, True),
        ("员工归属复核他人单应拒", guard_owner(EMP, "E002") is not None, True),
        ("员工留空目标应回填本人", guard_target_user(EMP, "")[0] == "E001", True),
        ("留空目标不得变成看全部", bool(guard_target_user(EMP, "")[0]), True),
        ("空身份 caller=None 应拒", guard_target_user(None, "E002")[1] is not None, True),
        ("空身份归属复核应拒", guard_owner(None, "E001") is not None, True),
        ("管理角色跨人应放行", guard_target_user(MGR, "E002")[1] is None, True),
        ("员工查本人应放行", guard_target_user(EMP, "E001")[1] is None, True),
    ]
    bad = [label for label, got, want in sec_cases if got != want]
    record("SEC-01", "越权守卫判定正确率", passed=len(sec_cases) - len(bad), total=len(sec_cases),
           verdict="pass" if not bad else "fail",
           severity="critical" if bad else "info",
           detail="含空身份与留目标语义", failures=bad)


# ---------------------------------------------------------------------------
# 4. 意图路由规则层 (RT-02 / RT-05 / RT-06)
# ---------------------------------------------------------------------------
ROUTE_CASES: list[tuple[str, str, str | None]] = [  # (话术, 期望意图, 期望域)
    ("我要报销这笔差旅费", "agent_delegate", "finance"),
    ("帮我提交报销单", "agent_delegate", "finance"),
    ("查一下 FIN5001 到哪一步了", "tool_call", "finance"),
    ("HR2001 工单进度", "tool_call", "hr"),
    ("给我开一张在职证明", "agent_delegate", "hr"),
    ("我要办离职", "agent_delegate", "hr"),
    ("年假规定是什么", "knowledge_qa", None),
    ("报销额度上限是多少", "knowledge_qa", None),
    ("把报销明细导出成 excel", "tool_call", "docgen"),
    ("本月成本环比涨了多少", "agent_delegate", "askdata"),
    ("生成一份上周周报", "agent_delegate", "analytics"),
    ("帮我审一下这份合同", "agent_delegate", "procurement"),
    ("我要申请采购服务器", "agent_delegate", "procurement"),
    ("帮我搜一下今天的新闻", "tool_call", None),
    ("签到", "tool_call", None),
    ("查下我的预算余额", "tool_call", "finance"),
    ("你好呀", "chitchat", None),
    ("确认一下这笔报销", "agent_delegate", "finance"),
]


def run_route_suite():
    print("\n=== 4. 意图路由规则层 (RT-02/05/06) ===")
    from app.assistant.intent import _RULES, _detect_target

    intent_ok = domain_ok = probed = 0
    misfires = []
    for msg, want_intent, want_domain in ROUTE_CASES:
        hit = None
        for pat, it in _RULES:
            if pat.search(msg):
                hit = it.value if hasattr(it, "value") else str(it)
                break
        if hit is None:  # 规则层未命中 -> 下沉语义/LLM 层, 本轮不可判
            continue
        probed += 1
        got_domain = _detect_target(msg)
        if hit == want_intent:
            intent_ok += 1
        else:
            misfires.append(f"'{msg}' 规则层判 {hit}, 期望 {want_intent}")
        if want_domain is None or got_domain == want_domain:
            domain_ok += 1
        else:
            misfires.append(f"'{msg}' 域判 {got_domain}, 期望 {want_domain}")
    # 分母只算规则层真正命中的条数: 下沉到语义/LLM 层的用例本轮无法判定, 计入分母
    # 会把"未测"混成"测挂了", 也会把"规则层覆盖不足"这个真问题掩盖掉。
    record("RT-05", "规则层意图误伤率", passed=intent_ok, total=probed,
           verdict="pass" if intent_ok == probed else "fail",
           severity="high" if intent_ok != probed else "info",
           detail=f"规则层命中 {probed}/{len(ROUTE_CASES)} 条, 其余下沉", failures=misfires)
    record("RT-02", "域判定准确率", passed=domain_ok, total=probed,
           verdict="pass" if domain_ok == probed else "warn", failures=misfires)


# ---------------------------------------------------------------------------
# 5. RAG 切块质量 (ING-CHUNK-01/02/04)
# ---------------------------------------------------------------------------
def run_chunking_suite():
    print("\n=== 5. 切块质量 (ING-CHUNK-*) ===")
    from app.docs.normalize import normalize_text
    from app.rag.ingest import CHUNK_MAX_CHARS, CHUNK_OVERLAP, CHUNK_SIZE, split_chunks

    docs = {p.name: p.read_text(encoding="utf-8", errors="replace")
            for p in (ROOT / "data" / "knowledge").glob("*")
            if p.suffix in {".md", ".srt", ".txt"}}
    # data/knowledge 里的真实样例都只有几百字符, 三篇各切成 1 块 —— 滑窗与超限分支
    # 根本没被触发, 用它测切块等于没测。补合成长文把两条分支真正跑到。
    docs["synthetic_long.md"] = ("段落内容甲。" * 300 + "\n\n") * 20        # ~72k 字符, 触发滑窗
    docs["synthetic_oversize.md"] = "无换行长文" * 6000                     # >CHUNK_MAX_CHARS
    if not docs:
        record("ING-CHUNK-01", "切块评测", passed=0, total=0, verdict="info", detail="无样例文档")
        return
    oversized, lens, overlaps = 0, [], 0
    per_doc: dict[str, int] = {}
    for name, raw in docs.items():
        norm = normalize_text(raw)          # normalize_text 直接返回 str(offset 基准文本)
        chunks = split_chunks(norm)
        per_doc[name] = len(chunks)
        lens.extend(len(c) for c in chunks)
        oversized += sum(1 for c in chunks if len(c) > CHUNK_MAX_CHARS)
        overlaps += len(chunks) * CHUNK_OVERLAP
    total_chars = sum(len(normalize_text(d)) for d in docs.values())
    # split_chunks 的契约就是"子块 <= CHUNK_SIZE", 用它测"超限块"永远满分 —— 口径错了。
    # 真正的超限发生在 parsers 产出的单个 block(>CHUNK_MAX_CHARS 时保留全文只记 warning),
    # 那需要真实 PDF/Excel 与容器内解析, 本轮改为测 split_chunks 的契约本身。
    over_size = sum(1 for x in lens if x > CHUNK_SIZE)
    record("ING-CHUNK-01", "子块长度契约(CHUNK_SIZE 上限)", passed=len(lens) - over_size, total=len(lens),
           verdict="pass" if over_size == 0 else "fail",
           severity="high" if over_size else "info",
           detail=f"{len(docs)} 篇 / {len(lens)} 块; block 层超限(CHUNK_MAX_CHARS={CHUNK_MAX_CHARS})"
                  f"需容器内真实文档才能测, 本轮未覆盖")
    avg = statistics.mean(lens) if lens else 0
    p95 = sorted(lens)[int(len(lens) * 0.95) - 1] if lens else 0
    record("ING-CHUNK-02", "块长度分布(观测)", passed=0, total=0, verdict="info",
           detail=f"avg={avg:.0f} p95={p95} max={max(lens) if lens else 0} 满窗率={sum(1 for x in lens if x >= CHUNK_SIZE) / max(1,len(lens)):.0%} "
                  f"| 滑窗触发: synthetic_long={per_doc.get('synthetic_long.md',0)}块 "
                  f"oversize={per_doc.get('synthetic_oversize.md',0)}块")
    record("ING-CHUNK-04", "重叠冗余率", passed=0, total=0, verdict="info",
           detail=f"overlap 字符 / 总字符 = {overlaps / max(1, total_chars):.1%}")


# ---------------------------------------------------------------------------
# 6. RRF 融合 (RET-IR-04)
# ---------------------------------------------------------------------------
def run_rrf_suite():
    print("\n=== 6. RRF 融合 (RET-IR-04) ===")
    from app.rag.retriever import RRF_K, _rrf_fuse
    from app.schemas import KnowledgeChunk

    def ck(i):
        return KnowledgeChunk(chunk_id=f"c{i}", doc_id=f"d{i}", title=f"t{i}",
                              content="x", source="s")

    cases = [
        ("双通道一致", [ck(1), ck(2), ck(3)], [ck(1), ck(4), ck(5)], "c1"),
        ("dense 首位 vs sparse 首位", [ck(1), ck(2), ck(3)], [ck(4), ck(5), ck(6)], None),
        ("共现块应超越单通道高分", [ck(9), ck(1), ck(2)], [ck(3), ck(1), ck(4)], "c1"),
        ("稀疏独有效应", [ck(1), ck(2), ck(3)], [ck(7), ck(8), ck(9)], None),
    ]
    fused_correct, inverted = 0, 0
    notes = []
    for label, dense, sparse, truth in cases:
        out = _rrf_fuse(dense, sparse, top_k=8)
        top = out[0].chunk_id if out else None
        if truth and top == truth:
            fused_correct += 1
        if top != dense[0].chunk_id:
            inverted += 1
            notes.append(f"{label}: dense 首位 {dense[0].chunk_id} -> 融合首位 {top}")
    record("RET-IR-04", "RRF 逆序发生率", passed=0, total=0, verdict="info",
           detail=f"K={RRF_K}, {inverted}/{len(cases)} 例首位被改写", failures=notes)
    record("RET-IR-04b", "RRF 真值首位命中", passed=fused_correct,
           total=len([c for c in cases if c[3]]),
           verdict="pass" if fused_correct == len([c for c in cases if c[3]]) else "fail",
           severity="high", detail="共现提权是否生效")


# ---------------------------------------------------------------------------
# 7. planner JSON 容错 (FIN-PLAN-01)
# ---------------------------------------------------------------------------
def run_json_suite():
    print("\n=== 7. planner JSON 容错 (FIN-PLAN-01) ===")
    from app.agents.finance_agent.executor import _parse_json

    good = [
        '{"steps":[{"intent":"查预算","kind":"read"}]}',
        '```json\n{"steps":[{"intent":"a","kind":"read"}]}\n```',
        '前置说明文字 {"steps":[{"intent":"a","kind":"read"}]} 后缀',
        '{"steps": []}',
        '{"verdict":"finish"}',
    ]
    bad = [
        '{"steps":[{"intent":"a"',            # 截断
        '我帮你查了一下预算',                    # 纯文本
        '{"steps":[{"intent":"a","kind":"read"}],}',  # 尾逗号
        '',
        '{steps:[{intent:a}]}',               # 未加引号
    ]
    ok = sum(1 for s in good if _parse_json(s))
    record("FIN-PLAN-01", "合法计划解析成功率", passed=ok, total=len(good),
           verdict="pass" if ok == len(good) else "warn",
           detail="畸形输入全部安全返 None 才算稳")
    safe = sum(1 for s in bad if _parse_json(s) is None)
    record("FIN-PLAN-01b", "畸形输入不抛异常率", passed=safe, total=len(bad),
           verdict="pass" if safe == len(bad) else "fail",
           severity="high", detail="返 None 才走降级, 抛异常会打断闭环")


# ---------------------------------------------------------------------------
# 8. 架构不变量静态审计
# ---------------------------------------------------------------------------
def run_static_audit():
    print("\n=== 8. 架构不变量静态审计 ===")
    src = lambda p: (ROOT / p).read_text(encoding="utf-8", errors="replace")

    hr = src("app/agents/hr_agent/executor.py")
    has_recur = "recursion_limit" in hr
    record("HR-LOOP-01", "hr 循环上限设置", passed=1 if has_recur else 0, total=1,
           verdict="pass" if has_recur else "fail", severity="critical",
           detail="已设 recursion_limit" if has_recur else "无 recursion_limit, 仅靠外层 a2a_timeout=120s 兜底")

    gate = "_gate_write_tool" in hr or "needs_confirmation" in hr
    record("HR-CONF-01", "hr 写确认门存在性", passed=1 if gate else 0, total=1,
           verdict="pass" if gate else "fail", severity="critical",
           detail="有门" if gate else "create/cancel_hr_ticket 无确认门, LLM 收齐信息即落库")

    retr = src("app/rag/retriever.py")
    budget = ("max_tokens" in retr) or ("token_budget" in retr) or ("char_budget" in retr)
    record("GEN-TOK-01", "retriever 层 token/长度预算", passed=1 if budget else 0, total=1,
           verdict="pass" if budget else "warn", severity="high",
           detail="有预算控制" if budget else "assemble_parents/format_context 无 token 预算与长度截断, 父块展开后上下文长度无上界")

    # 熔断器要查整条检索路径, 只看 retriever.py 会漏掉网关侧的实现
    chain = retr + src("app/rag/reranker.py") + src("app/rag/bm25.py") + src("app/assistant/graph.py")
    no_breaker = "circuit" not in chain.lower() and "breaker" not in chain.lower()
    record("RET-DEG-04", "检索链路熔断器", passed=0 if no_breaker else 1, total=1,
           verdict="warn" if no_breaker else "pass", severity="high",
           detail="retriever/reranker/bm25/graph 全无熔断实现, 单故障下连锁失败无收敛" if no_breaker else "有熔断")

    cfg = src("app/config.py")

    def grab(key):
        m = re.search(rf"{key}\s*:\s*(?:int|float)\s*=\s*([\d.]+)", cfg)
        return float(m.group(1)) if m else None

    prod = {k: grab(k) for k in ("rag_top_k", "rerank_top_n", "retrieval_score_threshold")}
    # 直接读 argparse 的默认值, 不要用正则去刮源码: "--top-k" 是连字符而 config 里是
    # rag_top_k 下划线, 正则曾把 --concurrency 的 default=4 当成 top_k 报出来过。
    try:
        from scripts.msmarco_eval.__main__ import parse_args as msmarco_parse
    except Exception:  # noqa: BLE001
        msmarco_parse = None
    if msmarco_parse is not None:
        ns = msmarco_parse([])
        ev = {"top_k": ns.top_k, "top_n": ns.top_n, "threshold": ns.threshold}
    else:
        ev = {}
    drift = []
    pairs = (("rag_top_k", "top_k"), ("rerank_top_n", "top_n"),
             ("retrieval_score_threshold", "threshold"))
    for pk, ek in pairs:
        pv = prod.get(pk)
        if pv is None or ek not in ev:
            continue
        if abs(float(ev[ek]) - pv) > 1e-9:
            drift.append(f"{pk} 生产 {pv:g} vs 评测 {ev[ek]:g}")
    record("RET-IR-01b", "评测口径与生产口径一致性", passed=1 if not drift else 0, total=1,
           verdict="pass" if not drift else "fail", severity="high",
           detail="; ".join(drift) if drift else f"一致 {prod}")

    todo = sum(1 for p in (ROOT / "app").rglob("*.py")
               if "__pycache__" not in str(p) and re.search(r"TODO|FIXME", p.read_text(encoding="utf-8", errors="replace")))
    record("PROJ-01", "代码 TODO/FIXME 残留文件数", passed=0, total=max(1, todo),
           verdict="info", detail=f"{todo} 个文件含 TODO/FIXME")


SUITES: dict[str, Callable[[], None]] = {
    "sql": run_sql_guard_suite,
    "confirm": run_confirm_gate_suite,
    "perm": run_permission_suite,
    "route": run_route_suite,
    "chunk": run_chunking_suite,
    "rrf": run_rrf_suite,
    "json": run_json_suite,
    "audit": run_static_audit,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="逗号分隔的 suite 名: " + ",".join(SUITES))
    args = ap.parse_args()
    picked = args.only.split(",") if args.only else list(SUITES)
    for name in picked:
        try:
            SUITES[name]()
        except Exception as e:  # noqa: BLE001
            record(f"suite/{name}", "套件执行", passed=0, total=1, verdict="fail",
                   severity="critical", detail=f"{type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    crit = [r for r in RESULTS if r["severity"] == "critical"]
    high = [r for r in RESULTS if r["severity"] == "high"]
    print(f"指标 {len(RESULTS)} 项 | critical {len(crit)} | high {len(high)}")
    for r in crit + high:
        if r["verdict"] in ("fail", "warn"):
            print(f"  [{r['severity']}/{r['verdict']}] {r['id']} {r['name']} {r['rate']:.0%} {r['detail']}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / f"offline_metrics_{ts}.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写 reports/offline_metrics_{ts}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
