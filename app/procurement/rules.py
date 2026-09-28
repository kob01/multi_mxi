"""采购与合同初审的确定性规则引擎。

为什么规则不交给 LLM(LLM 只补充语义风险, 不承担这道判断):
- 合同初审的"必备条款缺失"是**可判定**的: 关键词命中与否是确定性事实, 交给模型
  就存在漏判 —— 而漏判一份没有违约责任条款的合同, 代价远高于多问一句;
- 法务口径(哪些条款必须有、多大金额必须比价)是制度, 不是知识, 制度必须可版本化、
  可追溯、可被审计复核, 所以写成结构化规则而不是 prompt 里的一段话;
- 成本上也更省: 规则跑在进程内零 token, LLM 只处理规则给不了的"表述风险"。

判定口径: 缺条款按"命中任一关键词即视为存在"的宽松匹配(宁给 warning 不误报
critical), 真缺失才出 critical; 所有结论都带 suggestion, 让智能体能直接对用户复述。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

_CST = timezone(timedelta(hours=8))

RISK_ORDER = {"低": 0, "中": 1, "高": 2}


@dataclass
class Finding:
    """一条初审结论: 项名 + 严重度 + 建议。"""

    item: str
    level: str            # critical / warning / info
    detail: str
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrecheckOutcome:
    """一份合同/一张采购单的初审结果汇总。"""

    risk_level: str
    findings: list[Finding] = field(default_factory=list)
    conclusion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_level": self.risk_level,
            "findings": [f.to_dict() for f in self.findings],
            "conclusion": self.conclusion,
        }


# ---------------------------------------------------------------------------
# 合同必备条款(制度口径: 缺任一项即 critical, 只提示补充、不代拟条款)
# ---------------------------------------------------------------------------
REQUIRED_CLAUSES: list[tuple[str, tuple[str, ...]]] = [
    ("合同主体与统一社会信用代码", ("统一社会信用代码", "纳税人识别号", "税号", "营业执照", "身份证号")),
    ("标的与服务/货物范围", ("标的", "服务内容", "采购内容", "货物", "产品清单", "工作范围")),
    ("合同金额与币种", ("合同金额", "总价", "价款", "人民币", "金额")),
    ("付款方式与节点", ("付款方式", "支付", "预付", "分期付款", "结算")),
    ("发票与税费", ("发票", "增值税", "开票", "税票")),
    ("履行期限或交付时间", ("履行期限", "交付", "交货", "工期", "服务期", "有效期")),
    ("验收标准", ("验收", "质量标准", "验收合格", "合格标准")),
    ("违约责任", ("违约责任", "违约金", "赔偿责任", "逾期罚则")),
    ("保密条款", ("保密", "机密", "商业秘密")),
    ("知识产权归属", ("知识产权", "著作权", "专利", "成果归属")),
    ("争议解决与管辖", ("争议解决", "仲裁", "管辖", "诉讼", "协商解决")),
    ("解除与终止条件", ("解除", "终止")),
    ("签署要件(签字/盖章/日期)", ("签字", "盖章", "签章", "签署")),
]

# 高风险表述: 命中即 critical —— 这些都是"事后极难补救"的条款形态。
RISKY_TERMS: list[tuple[str, tuple[str, ...], str]] = [
    (
        "付款前置条件模糊",
        ("验收合格后付款", "满意后付款", "确认后付款", "合适时间付款"),
        "缺少明确付款期限(如“验收合格后 30 日内”), 存在无限期拖延付款的解释空间",
    ),
    (
        "单方无理由解除",
        ("甲方可无条件解除", "有权随时解除", "随时终止且不承担"),
        "赋予一方无条件解约权而无需补偿, 解约风险全压在另一方",
    ),
    (
        "无限/无上限责任",
        ("承担一切损失", "全部损失由", "不设赔偿上限", "无赔偿限额"),
        "未设赔偿上限, 潜在赔付金额不可估",
    ),
    (
        "自动续约未设退出条件",
        ("自动续约", "自动续期", "顺延"),
        "含自动续约但未同时约定提前通知退出的时限",
    ),
    (
        "先开票后付款且无付款保障",
        ("先开具全额发票", "开票后付款"),
        "开票在先、付款在后且无付款期限, 可能形成坏账与税票错配",
    ),
    (
        "预付款比例过高",
        ("预付50%", "预付 50%", "预付60%", "预付 60%", "预付80%", "全额预付", "预付全部"),
        "预付款比例超过 30% 且未见履约担保",
    ),
    (
        "违约金不对等",
        ("仅乙方承担违约", "乙方违约金的 3 倍", "乙方违约金的三倍"),
        "违约责任只约束单方, 显失公平",
    ),
]

# 我方立场红线: 这些条款形态对我方(甲方)不利, 属 warning(需商务谈判, 不必然否决)。
MINE_RED_LINES: list[tuple[str, tuple[str, ...], str]] = [
    ("付款周期短于 30 天", ("7 日内付款", "10 日内付款", "15 日内付款", "即付"),
     "付款周期过短, 与资金计划冲突"),
    ("无质保金/尾款安排", ("无质保金", "不预留质保金"), "缺少质量履约保证金安排"),
    ("管辖地约定在对方所在地", ("乙方所在地仲裁", "乙方所在地法院"), "争议解决地不利于我方应诉"),
]

# 法务要求的"必须走人工"底线: 命中即升级 risk_level 到 高, 并在结论里点名。
LEGAL_ESCALATION: list[tuple[str, tuple[str, ...]]] = [
    ("涉及个人信息处理", ("个人信息", "人脸", "生物识别", "用户数据")),
    ("涉及关联交易或利益冲突", ("关联交易", "关联方")),
    ("涉外主体或境外支付", ("境外", "跨境", "美元", "USD", "新加坡公司")),
    ("金额重大", ()),  # 由 amount 判定, 关键词留空
]

SINGLE_SIGN_LIMIT = Decimal("50000")   # 超此金额必须走集体决策/招标
PREPAY_MAX_RATIO = 0.30               # 预付款比例上限(无履约担保时)
QUOTE_REQUIRED_AMOUNT = Decimal("5000")  # 超此金额需 >=3 家比价


def _hit(text: str, keywords: Sequence[str]) -> str | None:
    """返回首个命中的关键词; 用宽松包含而非正则, 减少误配。"""
    for kw in keywords:
        if kw and kw in text:
            return kw
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text[:11], fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or 0).replace(",", ""))
    except Exception:  # noqa: BLE001 - 金额解析不出来按 0 处理, 由上层提示补充
        return Decimal("0")


def _risk_of(findings: list[Finding]) -> str:
    if any(f.level == "critical" for f in findings):
        return "高"
    if any(f.level == "warning" for f in findings):
        return "中"
    return "低"


def _conclusion(risk_level: str, findings: list[Finding]) -> str:
    critical = [f.item for f in findings if f.level == "critical"]
    warning = [f.item for f in findings if f.level == "warning"]
    if risk_level == "高":
        return (
            f"初审不通过: 命中 {len(critical)} 项红线({ '、'.join(critical[:4]) }); "
            "建议退回补充或修订后再送法务/财务复核, 初审不替代最终审批。"
        )
    if risk_level == "中":
        return (
            f"初审有条件通过: 无红线项, 但有 {len(warning)} 项需商务谈判或补充说明"
            f"({'、'.join(warning[:4])}); 可提交审批, 建议同步知会法务。"
        )
    return "初审通过: 未命中制度红线与条款缺失项; 仍建议由法务做最终复核后签署。"


def precheck_contract(
    *,
    content: str = "",
    title: str = "",
    party_a: str = "",
    party_b: str = "",
    amount: Any = 0,
    currency: str = "CNY",
    sign_date: Any = None,
    effective_date: Any = None,
    expiry_date: Any = None,
    supplier: dict[str, Any] | None = None,
    today: date | None = None,
) -> PrecheckOutcome:
    """对一份合同做确定性初审, 返回风险等级 + 逐条依据 + 建议。

    Args:
        content: 合同全文(规则判定主要输入)。
        title/party_a/party_b/amount/currency/各日期: 台账字段, 用于一致性与时效判定。
        supplier: 对手方在 proc_suppliers 中的记录(可空表示非在册供应商)。
        today: 注入当前日期以便测试(默认北京时间今天)。
    """
    today = today or datetime.now(_CST).date()
    text = content or ""
    findings: list[Finding] = []

    # --- 1. 台账基础要素 ---
    if not party_a or not party_b:
        findings.append(Finding(
            "合同主体不完整", "critical",
            f"甲方='{party_a or '(空)'}' / 乙方='{party_b or '(空)'}', 缺少任一方全称",
            "补齐双方工商全称后再送审, 简称无法核验主体资格",
        ))
    if _to_decimal(amount) <= 0:
        findings.append(Finding(
            "合同金额未填写或为 0", "warning",
            f"amount={amount!r}",
            "确认是框架/无上限协议还是漏填; 无金额协议需另附价格表或结算规则",
        ))
    if not text.strip():
        findings.append(Finding(
            "缺少合同全文", "warning",
            "本次只给了台账字段, 无法逐条核验条款",
            "把合同正文粘贴进来或先在知识库入库再送审(否则结论仅供参考)",
        ))

    # --- 2. 必备条款缺失 ---
    for clause, keywords in REQUIRED_CLAUSES:
        if text.strip() and not _hit(text, keywords):
            level = "critical" if clause in (
                "违约责任", "争议解决与管辖", "合同金额与币种", "签署要件(签字/盖章/日期)",
            ) else "warning"
            findings.append(Finding(
                f"缺少{clause}", level,
                "全文未出现该条款的任何常见表述",
                f"要求补充“{clause}”条款(制度要求必备)",
            ))

    # --- 3. 高风险表述 ---
    for item, keywords, why in RISKY_TERMS:
        kw = _hit(text, keywords)
        if kw:
            findings.append(Finding(item, "critical", f"命中表述「{kw}」: {why}", "修订该条款或删除该表述"))
    for item, keywords, why in MINE_RED_LINES:
        kw = _hit(text, keywords)
        if kw:
            findings.append(Finding(item, "warning", f"命中表述「{kw}」: {why}", "商务谈判调整"))

    # --- 4. 时效 ---
    expiry = _parse_date(expiry_date)
    if expiry:
        if expiry < today:
            findings.append(Finding(
                "合同已过期", "critical", f"到期日 {expiry.isoformat()} 早于今天", "先办续签或终止确认",
            ))
        elif expiry - today <= timedelta(days=30):
            findings.append(Finding(
                "临近到期", "warning",
                f"距到期日 {expiry.isoformat()} 仅 {(expiry - today).days} 天", "提前启动续签评估",
            ))
    elif text.strip():
        findings.append(Finding(
            "未识别到到期日", "warning", "台账 expiry_date 为空且正文未见有效期表述",
            "明确有效期(无固定期限合同需约定终止条件)",
        ))
    effective = _parse_date(effective_date)
    signed = _parse_date(sign_date)
    if effective and signed and effective < signed:
        findings.append(Finding(
            "生效日早于签署日", "warning",
            f"effective={effective.isoformat()} < sign={signed.isoformat()}", "核对日期, 倒签需说明",
        ))

    # --- 5. 对手方(供应商)交叉核验 ---
    amt = _to_decimal(amount)
    if supplier is None:
        if party_b:
            findings.append(Finding(
                "对手方不在册供应商", "warning",
                f"proc_suppliers 中查不到「{party_b}」",
                "先完成供应商准入(营业执照/开户许可/资质), 未准入不得付款",
            ))
    else:
        risk_status = str(supplier.get("risk_status") or "")
        if risk_status == "黑名单":
            findings.append(Finding(
                "对手方为黑名单供应商", "critical",
                f"{party_b} risk_status=黑名单", "禁止签约, 改用已准入供应商",
            ))
        elif risk_status == "关注":
            findings.append(Finding(
                "对手方风险状态为关注", "warning",
                f"{party_b} risk_status=关注", "追加资信调查与履约担保要求",
            ))
        account = str(supplier.get("bank_account") or "")
        if account and text.strip():
            tail = re.sub(r"\s", "", account)[-4:]
            if tail and tail not in re.sub(r"\s", "", text):
                findings.append(Finding(
                    "收款账号与在册信息不一致", "critical",
                    f"在册账号尾号 {tail} 未在合同全文出现",
                    "以在册账号为准修订付款条款(防“账号被改”类欺诈)",
                ))

    # --- 6. 金额分级与法务升级 ---
    if amt > SINGLE_SIGN_LIMIT:
        findings.append(Finding(
            "超单笔审批权限", "warning",
            f"金额 {amt:,.2f} 元 > {SINGLE_SIGN_LIMIT:,.0f} 元",
            "需走集体决策/招标并附比价记录, 不能由单人口头确认",
        ))
    for item, keywords in LEGAL_ESCALATION:
        kw = _hit(f"{title}\n{text}", keywords)
        if kw:
            findings.append(Finding(
                f"{item}", "critical", f"命中关键词「{kw}」", "转法务专项审查, 初审不作放行结论",
            ))

    risk_level = _risk_of(findings)
    return PrecheckOutcome(risk_level=risk_level, findings=findings, conclusion=_conclusion(risk_level, findings))


def precheck_purchase_order(
    *,
    amount: Any,
    department: str = "",
    supplier_name: str = "",
    quotes_count: int = 1,
    category: str = "",
    budget: dict[str, Any] | None = None,
    supplier: dict[str, Any] | None = None,
) -> PrecheckOutcome:
    """采购申请单的合规初审: 比价、供应商准入、预算余额三条硬规则。"""
    findings: list[Finding] = []
    amt = _to_decimal(amount)
    quotes = int(quotes_count or 0)

    if amt <= 0:
        findings.append(Finding("金额非法", "critical", f"amount={amount!r}", "填写实际采购金额"))
    if amt > QUOTE_REQUIRED_AMOUNT and quotes < 3:
        findings.append(Finding(
            "比价不充分", "critical",
            f"金额 {amt:,.2f} 元 > {QUOTE_REQUIRED_AMOUNT:,.0f} 元 但仅 {quotes} 份报价",
            f"补齐至 3 份及以上报价(或说明单一来源采购理由并经审批)",
        ))
    if supplier_name and supplier is None:
        findings.append(Finding(
            "供应商未准入", "critical", f"「{supplier_name}」不在 proc_suppliers 名录",
            "先走供应商准入, 或改选在册供应商",
        ))
    elif supplier:
        status = str(supplier.get("risk_status") or "")
        if status == "黑名单":
            findings.append(Finding("黑名单供应商", "critical", f"{supplier_name} 风险状态=黑名单", "禁止采购"))
        elif status == "关注":
            findings.append(Finding("关注供应商", "warning", f"{supplier_name} 风险状态=关注", "要求提供履约担保"))

    if budget:
        annual = _to_decimal(budget.get("annual_budget"))
        used = _to_decimal(budget.get("used_amount"))
        remaining = annual - used
        if annual <= 0:
            findings.append(Finding(
                f"{department} 无本年度预算记录", "warning",
                f"未查到 {budget.get('year') or ''} 年预算行", "确认是否走专项预算或跨年预算",
            ))
        elif amt > remaining:
            findings.append(Finding(
                "超出部门预算余额", "critical",
                f"{department} 年度 {annual:,.2f} 元, 已用 {used:,.2f} 元, "
                f"剩余 {remaining:,.2f} 元 < 本单 {amt:,.2f} 元",
                "先走预算追加审批, 或拆单至余额内并说明理由",
            ))
        else:
            ratio = float(amt / remaining) if remaining else 0.0
            findings.append(Finding(
                "预算占用提示", "info",
                f"{department} 剩余 {remaining:,.2f} 元, 本单占用 {ratio * 100:.1f}%",
                "",
            ))
    elif department:
        findings.append(Finding(
            f"未取到 {department} 预算数据", "warning", "fin_department_budgets 无匹配行或库不可用",
            "人工确认预算口径后再提交",
        ))

    if amt > SINGLE_SIGN_LIMIT:
        findings.append(Finding(
            "需集体决策", "warning", f"金额 {amt:,.2f} 元 > {SINGLE_SIGN_LIMIT:,.0f} 元",
            "附三方比价表与选商说明, 提交采购委员会",
        ))

    risk_level = _risk_of(findings)
    return PrecheckOutcome(risk_level=risk_level, findings=findings, conclusion=_conclusion(risk_level, findings))
