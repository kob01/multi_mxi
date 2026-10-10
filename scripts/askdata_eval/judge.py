"""两级判据: 先确定性比结果集(EX), 再可选地让 LLM 判"业务上是否等价"。

顺序与权限刻意设计成不对称的:

1. **列比对**(名字集合 + 顺序): 确定性, 列都不同不可能算对;
2. **EX 结果集比对**: 确定性, 数值带容差比较, 行序不参与(同一份数的不同排序是同一答案);
3. **LLM judge**: 只在 EX 判"不等"时出场, 且**只能把 fail 标成"疑似等价"**, 不能把
   已通过的结果改成 fail, 更不能把 fail 计入主指标 —— 主指标永远是 EX。

为什么这么定: LLM 判等价的绝对准确率明显低于一次数值比对, 让它当主判据等于用一把
会晃的尺子量另一把尺子。它的真实价值是**发现期望结果本身过期了**(数据变了/口径改了),
所以报告里单独列 "judge_rescued" 一栏, 提示这些用例需要人复核而不是直接改分数。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

JUDGE_PROMPT = """你在判断两份统计结果在业务上是否等价(不是文本是否相同)。

问题: {question}
期望口径说明: {note}

A(标准答案) 列: {a_columns}  行数: {a_count}
A 前若干行: {a_rows}

B(系统给出) 列: {b_columns}  行数: {b_count}
B 前若干行: {b_rows}

只输出 JSON: {{"equivalent": true/false, "reason": "一句话", "suspect": "stale_expected|column_naming|row_order|real_diff|none"}}
判定要求: 数值本身相同但列名/列序不同 -> equivalent=true, suspect=column_naming;
A 与 B 都是 0 行 -> equivalent=true, suspect=none;
数值不同 -> equivalent=false, suspect=real_diff; 你无法判断就 equivalent=false。"""

_NUMERIC_TOLERANCE = 0.01


def compare_columns(expected: list[str], actual: list[str]) -> tuple[bool, str]:
    """第一级: 期望列必须全在(缺列就是没算出来); 额外列只算警告不算失败。

    为什么额外列不直接判错: 状态机常会多带一个时间桶列(如 ``period``), 而 EX 比的是
    期望那些**单元格**对不对 —— 把"多一个列"判成错会把一堆真正算对了的用例误伤,
    反过来去掉"缺列"判据又会把真错藏起来。所以取前者: 缺列零容忍, 多列记警告。
    """
    missing = [c for c in expected if c not in actual]
    if missing:
        return False, f"缺列: {missing}"
    extra = [c for c in actual if c not in expected]
    if extra:
        return True, f"多了列 {extra}(按期望列投影后比对)"
    if list(expected) != list(actual):
        return True, f"列顺序不同(内容一致): {expected} vs {actual}"
    return True, ""


def _norm_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if isinstance(value, str):
        text = value.strip()
        try:
            return round(float(text.replace(",", "")), 4)
        except ValueError:
            return text
    return value


def _row_key(row: dict[str, Any], columns: list[str]) -> tuple:
    return tuple(_norm_value(row.get(c)) for c in columns)


def compare_rows(expected: list[dict[str, Any]], actual: list[dict[str, Any]],
                 columns: list[str]) -> tuple[bool, str]:
    """第二级(EX): 行集比对, 忽略行序, 数值容差 0.01(浮点/四舍五入差)。

    比对先把两侧都**投影到期望列**上再取多重集: 系统多算了一个时间桶时,
    投影后行集仍然应相等; 真算错了(按月拆开导致值不同)仍然会不通过。

    期望只存了前 20 行(seed 脚本的截断约定), 所以行数只比"前 20 行内是否一致",
    总行数由 ``rowcount`` 单独比 —— 拿被截断的列表去判"总行数不同"是假阳性。
    """
    if len(expected) != len(actual):
        return False, f"参与比对的行数不同: 期望 {len(expected)} 行, 实际 {len(actual)} 行"
    want = sorted(_row_key(r, columns) for r in expected)
    got = sorted(_row_key(r, columns) for r in actual)
    if want == got:
        return True, ""
    diff = [(w, g) for w, g in zip(want, got) if w != g][:3]
    return False, f"结果集不同(按 {columns} 忽略行序比对), 前 3 处差异: {diff}"


def judge_with_llm(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """第三级(可选): LLM 判等价。任何失败都降级成"没判", 不影响 EX 结论。"""
    expected = case.get("expected") or {}
    try:
        from app.config import get_settings
        from app.llm import get_chat_model

        settings = get_settings()
        llm = get_chat_model(settings.llm_model, temperature=0, json_mode=True)
        prompt = JUDGE_PROMPT.format(
            question=case.get("question", ""),
            note=(case.get("note") or "")[:400],
            a_columns=expected.get("columns"),
            a_count=expected.get("rowcount"),
            a_rows=json.dumps((expected.get("rows") or [])[:5], ensure_ascii=False, default=str)[:1200],
            b_columns=actual.get("columns"),
            b_count=actual.get("rowcount"),
            b_rows=json.dumps((actual.get("rows") or [])[:5], ensure_ascii=False, default=str)[:1200],
        )
        content = str(llm.invoke(prompt).content)
        parsed = _parse(content)
        if parsed is None:
            return {"available": False, "reason": "模型输出不是 JSON"}
        return {"available": True, **parsed}
    except Exception as exc:  # noqa: BLE001 - judge 是加分项, 挂了就只报 EX
        logger.warning("[askdata_eval] LLM judge 不可用(本轮只报 EX): %s", exc)
        return {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _parse(text: str) -> dict[str, Any] | None:
    cleaned = (text or "").strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:]
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def grade(case: dict[str, Any], actual: dict[str, Any], *, use_judge: bool) -> dict[str, Any]:
    """一条用例的完整判定(返回体就是报告里的一行)。"""
    expected = case.get("expected") or {}
    exp_cols = list(expected.get("columns") or [])
    act_cols = list(actual.get("columns") or [])
    verdict: dict[str, Any] = {
        "case_id": case.get("case_id", ""),
        "question": case.get("question", ""),
        "tags": case.get("tags", ""),
        "ex_pass": False,
        "judge": "",
        "suspect": "",
        "reason": "",
        "column_warn": "",
    }
    if not exp_cols:
        verdict["reason"] = "期望结果缺列信息(用例本身不完整)"
        return verdict
    cols_ok, cols_note = compare_columns(exp_cols, act_cols)
    if not cols_ok:
        verdict["reason"] = cols_note
        return verdict
    verdict["column_warn"] = cols_note if cols_note else ""
    # 比对只按**期望列**投影: 额外列(时间桶/对比列)不参与行集比对。
    rows_ok, rows_note = compare_rows(list(expected.get("rows") or []),
                                      list(actual.get("rows") or []), exp_cols)
    if int(expected.get("rowcount") or 0) != int(actual.get("rowcount") or 0):
        rows_ok = False
        rows_note = (rows_note + f"; 总行数不同 {expected.get('rowcount')} vs {actual.get('rowcount')}")
    if rows_ok:
        verdict["ex_pass"] = True
        verdict["reason"] = "EX 通过"
        return verdict
    verdict["reason"] = rows_note
    if use_judge:
        judged = judge_with_llm(case, actual)
        if not judged.get("available"):
            verdict["judge"] = "unavailable"
            verdict["reason"] += f"; judge 不可用({judged.get('reason', '')})"
            return verdict
        equivalent = bool(judged.get("equivalent"))
        verdict["judge"] = "equivalent" if equivalent else "different"
        verdict["suspect"] = str(judged.get("suspect") or "")
        verdict["judge_reason"] = str(judged.get("reason") or "")[:200]
    return verdict
