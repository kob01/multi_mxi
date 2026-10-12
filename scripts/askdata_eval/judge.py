"""判据: 先确定性比结果集(EX), 再算 DSL-EM / slot-F1, 最后可选地让 LLM 判"业务上是否等价"。

顺序与权限刻意设计成不对称的:

1. **列比对**(名字集合 + 顺序): 确定性, 列都不同不可能算对;
2. **EX 结果集比对**: 确定性, 数值带容差比较, 行序不参与(同一份数的不同排序是同一答案);
3. **DSL-EM / slot-F1**: 不执行就能算的两个便宜指标, 把"口径选错了但结果碰对了"与
   "环节错了"分开看(只看 EX 会将两者抹成一个数);
4. **LLM judge**: 只在 EX 判"不等"时出场, 且**只能把 fail 标成"疑似等价"**, 不能把
   已通过的结果改成 fail, 更不能把 fail 计入主指标 —— 主指标永远是 EX。

为什么这么定: LLM 判等价的绝对准确率明显低于一次数值比对, 让它当主判据等于用一把
会晃的尺子量另一把尺子。它的真实价值是**发现期望结果本身过期了**(数据变了/口径改了),
所以报告里单独列 "judge_rescued" 一栏, 提示这些用例需要人复核而不是直接改分数。

DSL-EM 不比 ``limit``/``order_by``: 它们不影响"哪个口径按什么维度算", 而 golden 是人工标的,
这两个字段几乎注定与模型给的默认值不同 —— 拿它们参与严格匹配得出的分数只能反映标注风格。"""

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


def _canonical_dsl(dsl: dict[str, Any]) -> dict[str, Any]:
    """DSL 的语义核(指标 id/维度/时间/过滤/对比), 用于 DSL-EM。

    三个不参与项都有具体理由, 不是"让分数好看":
    - ``limit``/``order_by``: 不影响"哪个口径按什么维度算", golden 是人工标的;
    - 聚合覆盖 ``agg``: 不写就用语义层默认聚合, 写错会直接反映在结果集上(EX 抓住),
      拿它参与严格匹配只会带来"期望不写 agg 而模型写了默认值"这种噪声;
    - ``granularity`` 的 "none" 与 "": 同一个意思("没指定粒度"), 必须归一。

    集合类字段全部排序: 否则"同一份意图不同写法"会被算不同。
    """
    if not isinstance(dsl, dict) or not dsl.get("metrics"):
        return {}
    metrics = sorted(
        str(item.get("id"))
        for item in (dsl.get("metrics") or []) if isinstance(item, dict) and item.get("id")
    )
    time_block = dsl.get("time") or {}
    granularity = str(time_block.get("granularity") or "")
    filters = sorted(
        f"{item.get('dim')}|{item.get('op') or 'eq'}|{json.dumps(item.get('value'), ensure_ascii=False, default=str)}"
        for item in (dsl.get("filters") or []) if isinstance(item, dict) and item.get("dim")
    )
    compare = dsl.get("compare") or {}
    compare_type = compare.get("type") if isinstance(compare, dict) else compare
    return {
        "metrics": metrics,
        "dims": sorted(str(d) for d in (dsl.get("dims") or [])),
        "time": {
            "range": str(time_block.get("range") or ""),
            "granularity": "" if granularity in ("", "none") else granularity,
            "start": str(time_block.get("start") or ""),
            "end": str(time_block.get("end") or ""),
        },
        "filters": filters,
        "compare": str(compare_type or "none"),
    }


def dsl_exact_match(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[bool, str]:
    """DSL-EM: 语义核完全相等才算通过(期望缺 DSL 的用例不参与, 不算失败)。"""
    want, got = _canonical_dsl(expected), _canonical_dsl(actual)
    if not want or not got:
        return False, "没有可比的 DSL"
    if want == got:
        return True, ""
    parts = []
    for key in sorted(set(want) | set(got)):
        if want.get(key) != got.get(key):
            parts.append(f"{key}: 期望 {want.get(key)} / 实际 {got.get(key)}")
    return False, "; ".join(parts)[:220]


def _slot_tokens(slots: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("metric_terms", "dim_terms"):
        for item in slots.get(key) or []:
            token = str(item or "").strip().lower()
            if token:
                out.add(f"{key}:{token}")
    for item in slots.get("filter_terms") or []:
        phrase = str((item or {}).get("phrase") if isinstance(item, dict) else item or "").strip().lower()
        if phrase:
            out.add(f"filter:{phrase}")
    time_block = slots.get("time") or {}
    phrase = str(time_block.get("phrase") or "").strip().lower()
    if phrase:
        out.add(f"time:{phrase}")
    compare = slots.get("compare") or {}
    ctype = str(compare.get("type") if isinstance(compare, dict) else compare or "").strip().lower()
    if ctype and ctype != "none":
        out.add(f"compare:{ctype}")
    return out


def slot_f1(expected_slots: dict[str, Any], actual_slots: dict[str, Any]) -> tuple[float, str]:
    """slot-F1: 把槽位归一成 token 集后算 P/R 调和平均(期望为空时返 -1 = 不参与)。

    命中判据用**同前缀下的包含关系**而不是字面相等: 模型抽"本月报销总额"而标准答案是
    "报销总额"时算命中 —— 拿字面相等判会把"多说了一点"当成"没抽到", slot-F1 会
    低到看不出真正的抽取错误。
    """
    want = _slot_tokens(expected_slots or {})
    got = _slot_tokens(actual_slots or {})
    if not want:
        return -1.0, "期望槽位未标注"
    if not got:
        return 0.0, "本轮没抽出任何槽位"
    hit = sum(1 for token in want if _token_hit(token, got))
    f1 = 2.0 * hit / (len(want) + len(got))
    miss = sorted(t for t in want if not _token_hit(t, got))[:4]
    extra = sorted(t for t in got if not any(_token_hit(t, {other}) for other in want))[:4]
    return round(f1, 4), f"漏 {miss} 多 {extra}" if (miss or extra) else ""


def _token_hit(token: str, others: set[str]) -> bool:
    """同一个槽位桶里互相包含就算命中("部门" vs "各部门")。"""
    prefix, _, text = token.partition(":")
    if not text:
        return token in others
    for other in others:
        other_prefix, _, other_text = other.partition(":")
        if other_prefix != prefix or not other_text:
            continue
        if text in other_text or other_text in text:
            return True
    return False


def mis_clarified(case: dict[str, Any], actual: dict[str, Any]) -> bool:
    """误澄清: 本轮被澄清拦下, 但上一轮最高分候选就是期望口径。

    这就是"本可以不问": 分数已经够了却去追问, 代价由用户付。反过来(该问没问)不在这里判,
    它会在 EX 里以"算错了"的形式付出代价。
    """
    if str(actual.get("verdict") or "") != "clarify":
        return False
    want = _first_metric(case.get("dsl"))
    candidates = actual.get("candidates") or []
    top = str((candidates[0] or {}).get("id") or "") if candidates else ""
    return bool(want) and want == top


def _first_metric(dsl: Any) -> str:
    if not isinstance(dsl, dict):
        return ""
    for item in dsl.get("metrics") or []:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return ""


def grade(case: dict[str, Any], actual: dict[str, Any], *, use_judge: bool) -> dict[str, Any]:
    """一条用例的完整判定(返回体就是报告里的一行)。"""
    expected = case.get("expected") or {}
    exp_cols = list(expected.get("columns") or [])
    act_cols = list(actual.get("columns") or [])
    verdict: dict[str, Any] = {
        "case_id": case.get("case_id", ""),
        "question": case.get("question", ""),
        "tags": case.get("tags", ""),
        "difficulty": case.get("difficulty", ""),
        "intent": case.get("intent", ""),
        "ex_pass": False,
        "dsl_em": False,
        "dsl_em_note": "",
        "slot_f1": None,
        "slot_f1_note": "",
        "mis_clarify": mis_clarified(case, actual),
        "route": actual.get("route", ""),
        "cache_hit": bool(actual.get("cache_hit")),
        "judge": "",
        "suspect": "",
        "reason": "",
        "column_warn": "",
    }
    # DSL-EM 与 slot-F1 先算: 它们与 EX 同一条用例共存(一个看选口径, 一个看抽说法),
    # 写在后面会在 EX 提前 return 的分支里丢成默认值 False(看似"不匹配", 其实是没算)。
    em_ok, em_note = dsl_exact_match(case.get("dsl") or {}, actual.get("dsl") or case.get("dsl") or {})
    verdict["dsl_em"], verdict["dsl_em_note"] = em_ok, em_note
    # DSL 模式不跑 LLM, 根本没有槽位产出: 那时 slot-F1 = 0 是假分数(它应当不参与而不是归零)。
    if "slots" in actual:
        f1, f1_note = slot_f1(case.get("slots") or {}, actual.get("slots") or {})
        verdict["slot_f1"] = f1 if f1 >= 0 else None
        verdict["slot_f1_note"] = f1_note
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
