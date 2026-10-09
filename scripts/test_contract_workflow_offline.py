"""Contract_Agent 合同初审工作流改造的离线单测: 不需要整栈, 不需要 LLM/DB/网络/A2A 下游。

跑法::

    uv run python -m scripts.test_contract_workflow_offline

覆盖可离线判定的部分(DAG 的真实推理、跨容器检索/落台账在整栈里回归):
  1. 可逆 PII 脱敏往返(``mask_round_trip`` / ``restore``): ID/账号/手机/金额命中、同值
     同占位、URL 段原样、还原无损且无占位符残留;
  2. 条款切块(``_split_clauses``): 按标记切、单块 <= 上限、块数封顶不丢内容;
  3. JSON 抠取(``_parse_json``)与金额解析(``_parse_amount``, 含万/亿单位);
  4. 强制溯源: 用节点同一口径(_normalize 包含判定)验证"引用不在原文即丢弃";
  5. 合并去重(``_merge_findings``): 语义项按(条款,引用)去重、规则项优先保留;
  6. 规则红线(``rules._penalty_over_cap``): 违约金/赔偿 > 30% 命中, <= 30% 不报;
  7. HITL 确认(``procurement_server.confirm_contract_review`` 纯校验分支): 非法 action 拒、
     普通员工越权拒(在触库前返回);
  8. Agent Card: 含 contract_review(溯源) 与新增 hitl_confirm 技能;
  9. 提示词模板: STRUCTURE/CLAUSE_REVIEW/AGGREGATE/LEGACY 可 format 且无残留花括号;
  10. 配置项: contract_* 齐备且阈值/超时为正、开关为布尔。
"""

from __future__ import annotations

from app.agents.contract_agent import prompts
from app.agents.contract_agent.agent_card import build_agent_card
from app.agents.contract_agent.executor import (
    _amount_from_text,
    _merge_findings,
    _normalize,
    _parse_amount,
    _parse_json,
    _restore_items,
    _split_clauses,
)
from app.config import get_settings
from app.mcp_servers import procurement_server
from app.procurement import rules
from app.schemas import Role
from app.security.masking import mask_round_trip, restore

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def test_mask_round_trip() -> None:
    original = (
        "合同金额 65000 元，乙方收款账号 6222021234567890123，法人身份证 "
        "11010119900101123X，联系人电话 13800138000。付款 65000 元 见 "
        "/api/files/reports/CT8001 与 https://x.com/a/b"
    )
    masked, rmap = mask_round_trip(original)
    check("身份证被打码", "[ID_1]" in masked, masked)
    check("银行账号被打码", "[ACCOUNT_1]" in masked, masked)
    check("手机号被打码", "[PHONE_1]" in masked, masked)
    check("金额被打码", "[AMOUNT_1]" in masked, masked)
    check("同值同占位(两处 65000 元 用同一占位符)", masked.count("[AMOUNT_1]") == 2, masked)
    check("URL/下载链接段原样保留", "/api/files/reports/CT8001" in masked and "https://x.com/a/b" in masked, masked)
    check("还原后与原文逐字一致", restore(masked, rmap) == original, restore(masked, rmap))
    check("还原后无占位符残留", "[AMOUNT_" not in restore(masked, rmap) and "[ID_" not in restore(masked, rmap))


def test_split_clauses() -> None:
    text = "第一条 标的与范围。" + "内容" * 60 + "\n第二条 违约责任。" + "细则" * 60 + "\n第三条 争议解决。"
    chunks = _split_clauses(text, max_chars=120, max_chunks=24)
    check("按条款标记切出多块", len(chunks) >= 2, f"chunks={len(chunks)}")
    check("每块不超过字符上限", all(len(c) <= 120 for c in chunks), str([len(c) for c in chunks]))
    many = "\n".join(f"第{i}条 条款内容填充内容填充内容填充" for i in range(1, 40))
    capped = _split_clauses(many, max_chars=40, max_chunks=8)
    check("块数封顶且未丢内容(合并尾部)", len(capped) <= 8 and "".join(capped).replace("\n", "").find("第39条") >= 0,
          f"n={len(capped)}")
    check("空文本切空列表", _split_clauses("", 120, 24) == [])


def test_parse_json_and_amount() -> None:
    check("裸 JSON", _parse_json('{"a": 1}') == {"a": 1})
    check("``` 包裹", _parse_json('```json\n{"is_review": true}\n```') == {"is_review": True})
    check("前后杂字", _parse_json('结论: {"risk_level": "高"} 完') == {"risk_level": "高"})
    check("非法返回 None", _parse_json("不是 JSON") is None)
    check("金额解析-万元", abs(_parse_amount("6.5万元") - 65000) < 1e-6, str(_parse_amount("6.5万元")))
    check("金额解析-千分位元", _parse_amount("65,000 元") == 65000.0, str(_parse_amount("65,000 元")))
    check("金额解析-亿", abs(_parse_amount("1.2亿元") - 1.2e8) < 1e-6, str(_parse_amount("1.2亿元")))
    check("金额解析-空为0", _parse_amount("") == 0.0)
    check("原文确定性抽金额", _amount_from_text("含税合同金额 880000 元") == 880000.0,
          str(_amount_from_text("含税合同金额 880000 元")))
    check("原文抽金额-万元单位", abs(_amount_from_text("总价 6.5 万元") - 65000) < 1e-6,
          str(_amount_from_text("总价 6.5 万元")))


def test_grounding_validation() -> None:
    chunk = "乙方逾期每日按合同金额的百分之五支付违约金，且甲方可随时解除合同。"
    grounded = "甲方可随时解除合同"
    fabricated = "本合同适用英国法律仲裁"
    # 与节点同一口径: 引用去空白后必须真在该块原文出现, 否则丢弃。
    check("真实引用命中原文", _normalize(grounded) in _normalize(chunk))
    check("编造引用不命中原文(应被丢弃)", _normalize(fabricated) not in _normalize(chunk))


def test_merge_findings() -> None:
    rule = [{"item": "缺少违约责任", "level": "critical", "detail": "全文未出现", "suggestion": "补充"}]
    sem = [
        {"clause_no": "第八条", "quote": "随时解除", "risk": "单方解除", "level": "高", "suggestion": "修订", "source": "semantic"},
        {"clause_no": "第八条", "quote": "随时解除", "risk": "重复项应去重", "level": "高", "suggestion": "", "source": "semantic"},
    ]
    merged = _merge_findings(rule, sem)
    check("规则项保留", any(m["source"] == "rule" and m["item"] == "缺少违约责任" for m in merged), str(merged))
    check("语义项按(条款,引用)去重", sum(1 for m in merged if m["source"] == "semantic") == 1, str(merged))
    check("规则 critical 归一为高风险级", any(m["source"] == "rule" and m["level"] == "高" for m in merged), str(merged))
    restored = _restore_items(
        [{"source": "semantic", "quote": "付款 [AMOUNT_1]", "risk": "含 [AMOUNT_1]", "level": "高"}],
        {"[AMOUNT_1]": "880000 元"},
    )
    check("风险卡落台账前还原占位符",
          restored[0]["quote"] == "付款 880000 元" and restored[0]["risk"] == "含 880000 元", str(restored))


def test_penalty_cap_rule() -> None:
    check("违约金 40% 超上限命中", rules._penalty_over_cap("违约金按合同金额的40%支付") == 40.0,
          str(rules._penalty_over_cap("违约金按合同金额的40%支付")))
    check("违约金 20% 不报", rules._penalty_over_cap("违约金为损失的20%") is None,
          str(rules._penalty_over_cap("违约金为损失的20%")))
    check("无比例不报", rules._penalty_over_cap("违约应承担赔偿责任") is None)
    # E2E 暴露的语序: "%" 在"违约金"之前(按句判定应兼容)。
    check("反序(40% 在违约金前)仍命中", rules._penalty_over_cap("每日按合同金额的40%支付违约金") == 40.0,
          str(rules._penalty_over_cap("每日按合同金额的40%支付违约金")))
    outcome = rules.precheck_contract(content="合同金额10万元，违约金按合同总金额的50%计算。", party_b="", amount=100000)
    check("precheck_contract 命中违约金超上限红线",
          any("超法定上限" in f.item and f.level == "critical" for f in outcome.findings),
          str([f.item for f in outcome.findings]))


def test_confirm_pure_guards() -> None:
    fn = getattr(procurement_server.confirm_contract_review, "fn", procurement_server.confirm_contract_review)
    bad = fn(contract_no="CT8001", action="approve", caller_user_id="E1", caller_role="manager")
    check("非法 action 被拒", isinstance(bad, dict) and "error" in bad, str(bad))
    denied = fn(contract_no="CT8001", action="confirm", caller_user_id="E1", caller_role="employee")
    check("普通员工越权确认被拒(触库前)", isinstance(denied, dict) and denied.get("forbidden") is True, str(denied))
    noid = fn(contract_no="CT8001", action="confirm", caller_user_id="", caller_role="admin")
    check("无调用者身份被拒", isinstance(noid, dict) and noid.get("forbidden") is True, str(noid))


def test_agent_card() -> None:
    card = build_agent_card()
    skill_ids = {s.id for s in card.skills}
    check("含 contract_review 技能", "contract_review" in skill_ids, str(skill_ids))
    check("新增 hitl_confirm 技能", "hitl_confirm" in skill_ids, str(skill_ids))
    cr = next(s for s in card.skills if s.id == "contract_review")
    check("contract_review 描述了原文溯源", "溯源" in cr.description, cr.description)
    check("原有采购/供应商/统计技能仍在",
          {"create_purchase_order", "purchase_precheck", "supplier_check", "procurement_text2sql"} <= skill_ids,
          str(skill_ids))


def test_prompts_format() -> None:
    label = prompts.ROLE_LABELS[Role.MANAGER]
    caps = prompts.ROLE_CAPABILITIES[Role.MANAGER]
    try:
        st = prompts.STRUCTURE_PROMPT.format(max_chunk_chars=1200)
        cr = prompts.CLAUSE_REVIEW_PROMPT.format(role_label=label, rag_reference="(无)")
        ag = prompts.AGGREGATE_PROMPT.format(merged_items="[1] xx", rule_risk_level="高")
        lg = prompts.LEGACY_SYSTEM_PROMPT.format(role_label=label, capabilities=caps, schema="DDL")
    except KeyError as exc:
        check("提示词全部可格式化", False, f"KeyError: {exc}")
        return
    named = ("{role_label}", "{capabilities}", "{schema}", "{max_chunk_chars}", "{rag_reference}", "{merged_items}", "{rule_risk_level}")
    leftover = [ph for ph in named for t in (st, cr, ag, lg) if ph in t]
    check("具名占位符均已消费", not leftover, f"残留: {leftover}")
    check("CLAUSE_REVIEW 保留强制溯源口径", "逐字引用" in cr and "严禁" in cr, cr[:0])
    check("AGGREGATE 保留只升不降口径", "绝不能低于" in ag, ag[:0])


def test_config_fields() -> None:
    st = get_settings()
    check("contract_workflow_enabled 为布尔", isinstance(st.contract_workflow_enabled, bool))
    check("contract_clause_chunk_chars 为正", st.contract_clause_chunk_chars > 0)
    check("contract_max_clause_chunks 为正", st.contract_max_clause_chunks > 0)
    check("contract_chunk_review_concurrency 为正", st.contract_chunk_review_concurrency > 0)
    check("contract_step_timeout 为正", st.contract_step_timeout > 0)
    check("contract_pii_mask_enabled 为布尔", isinstance(st.contract_pii_mask_enabled, bool))
    check("contract_rag_augment_enabled 为布尔", isinstance(st.contract_rag_augment_enabled, bool))


def main() -> int:
    test_mask_round_trip()
    test_split_clauses()
    test_parse_json_and_amount()
    test_grounding_validation()
    test_merge_findings()
    test_penalty_cap_rule()
    test_confirm_pure_guards()
    test_agent_card()
    test_prompts_format()
    test_config_fields()

    failed = [name for ok, name, _ in _results if not ok]
    print("\n" + ("全部通过" if not failed else f"失败 {len(failed)} 项: " + ", ".join(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
