"""badcase 归因路由: 一条被点踩/改写的问数, 责任落在链路的哪一段, 该怎么处置。

为什么是规则而不是 LLM: 归因结论要驱动**写动作**(补词、改语义层、补评测集),
让模型判断"这错在链接还是错在 DSL"等于把改口径的权限交给一次概率输出。
留痕里本来就有确定证据(错误码、候选分数、verdict、route), 用它们做判定就够了。

五类归因与处置(刻意不是"所有 badcase 都进训练"):
- ``intent`` 槽位抽错(置信不足却被拿去算)      -> 待标注清单 + prompt 修订建议(只出报告)
- ``link``   链接错/歧义(候选分数接近或太低)     -> 补词草案(``sem_terms`` status=draft)
- ``dsl``    DSL 结构错(语法/兼容性/口径冲突)    -> 评测集补录草案(enabled=false)
- ``exec``   取数被拒或算不出(权限/成本/编译)    -> 运维待办(记 notes, 不动口径)
- ``render`` 数对但呈现错(图型/叙述)             -> 规则引擎待办(记 notes)

草案一律写成"未生效"状态: 归因只能**提出**修订, 生效必须过人 —— 否则点踩的人
与改口径的人可以是同一个念头。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DISPOSITION_TERM = "draft_term"          # 补词草案
DISPOSITION_GOLDEN = "draft_golden"      # 评测集补录草案
DISPOSITION_REVIEW = "manual_review"     # 只登记, 人来判
DISPOSITION_NONE = "none"

# 错误码 -> 归因类别(与 app/semantic/validate 的错误码同一份词表, 新增码要同步这里)。
_CODE_TO_CATEGORY = {
    "E_SCHEMA": "dsl",
    "E_UNKNOWN_METRIC": "link",
    "E_UNKNOWN_DIM": "link",
    "E_INCOMPATIBLE_DIM": "dsl",
    "E_TABLE_CONFLICT": "dsl",
    "E_CALIBER_CONFLICT": "dsl",
    "E_TIME_INVALID": "intent",
    "E_ENUM_INVALID": "link",
    "E_NO_PERMISSION": "exec",
    "E_COL_POLICY": "exec",
    "E_ROWS_CAP": "exec",
    "E_COMPILE": "exec",
    "E_ENGINE": "exec",
    "E_ENGINE_BREAKER": "exec",
    "E_BUDGET": "exec",
    "E_SERVICE": "exec",
    "E_VERIFY": "exec",
    "E_LINK": "link",
}


def attribute(row: dict[str, Any]) -> tuple[str, str]:
    """由留痕推出 ``(类别, 处置)``。

    判定顺序是**从最确定的证据到最弱的信号**: 先看用户改了什么(edit_dsl 的 diff 是
    直证), 再按错误码, 没码看 verdict, 最后才看候选分数分布。反过来先猜"看着像链接错"
    会把 DSL 的结构性错误误归到补词上, 那才是真正让人找不到根因的漂移。
    """
    diff = dsl_diff(row.get("dsl_json"), row.get("corrected_dsl") or row.get("corrected"))
    if diff:
        category = _category_of_diff(diff)
        return category, _disposition(category, row)
    codes = _as_list(row.get("error_codes"))
    verdict = str(row.get("verdict") or "").lower()
    route = str(row.get("route") or "").lower()
    candidates = _as_list(row.get("candidates"))
    scores = [float(c.get("score") or 0) for c in candidates if isinstance(c, dict)]

    for code in codes:
        category = _CODE_TO_CATEGORY.get(str(code))
        if category:
            return category, _disposition(category, row)
    if str(row.get("feedback") or "").lower() == "rewritten":
        # 用户改了问法而不是改口径: 说存"这句话我没听懂"(①/②), 不是 DSL 错。
        return "intent", _disposition("intent", row)
    if verdict == "clarify":
        return "link", _disposition("link", row)
    if verdict in ("denied", "fail"):
        return "exec", _disposition("exec", row)
    if route in ("sandbox", "degrade"):
        return "exec", _disposition("exec", row)
    if len(scores) >= 2 and scores[0] - scores[1] < 0.1:
        # 前两名分差过小 = 系统自己也没把握, 却仍然算出了数: 这是链接段的过度自信。
        return "link", _disposition("link", row)
    if scores and scores[0] <= 0:
        return "intent", _disposition("intent", row)
    return "render", _disposition("render", row)


def dsl_diff(was: Any, now: Any) -> dict[str, tuple[Any, Any]]:
    """比较"当时 DSL"与"用户改成后的 DSL", 输出变了哪几个顶层字段。

    这是整条归因链上信息量最大的一个信号: 用户把 ``metrics[0].id`` 改了就是选错了口径(link/dsl),
    把 ``time`` 改了就是理解错了时间(intent), 把 ``filters`` 改了就是枚举/限定问题。
    拿不到 dict 形状(草案里只有 raw 文本)时返回空: 宁可不归因, 也不要猜一个分错的对象。
    """
    was_map = _flatten_dsl(was)
    now_map = _flatten_dsl(now)
    if not was_map or not now_map:
        return {}
    out: dict[str, tuple[Any, Any]] = {}
    for key in sorted(set(was_map) | set(now_map)):
        left, right = was_map.get(key), now_map.get(key)
        if left != right:
            out[key] = (left, right)
    return out


def _flatten_dsl(value: Any) -> dict[str, Any]:
    import json

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, dict) or not value.get("metrics"):
        return {}
    out: dict[str, Any] = {}
    for index, item in enumerate(value.get("metrics") or []):
        if isinstance(item, dict):
            out[f"metrics[{index}].id"] = item.get("id")
            if item.get("agg"):
                out[f"metrics[{index}].agg"] = item.get("agg")
    for index, item in enumerate(value.get("dims") or []):
        out[f"dims[{index}]"] = item
    time_block = value.get("time") or {}
    for key in ("range", "granularity", "start", "end"):
        if time_block.get(key):
            out[f"time.{key}"] = time_block[key]
    for index, item in enumerate(value.get("filters") or []):
        if isinstance(item, dict):
            out[f"filters[{index}].dim"] = item.get("dim")
            out[f"filters[{index}].op"] = item.get("op")
            out[f"filters[{index}].value"] = item.get("value")
    compare = value.get("compare") or {}
    out["compare.type"] = compare.get("type") if isinstance(compare, dict) else compare
    if value.get("order_by"):
        out["order_by"] = value["order_by"]
    if value.get("limit"):
        out["limit"] = value["limit"]
    return out


# 哪些字段变了 -> 责任落在哪一段(与模块③的字段表一致)。
_DIFF_TO_CATEGORY = {
    "metrics": "link",      # 选错口径: 链接或候选问题
    "dims": "dsl",          # 该不该下钻是 DSL 结构决策
    "filters": "link",      # 枚举/限定没对上
    "time": "intent",       # 时间理解错
    "compare": "dsl",
    "order_by": "render",   # 只要了排序差异: 不影响数, 影响呈现
    "limit": "render",
}
# 优先级而不是字母序: 用户同时改了"口径 + 时间"时, 先归到口径 —— 否则一次改写会被
# 归成一个次要字段的问题, 处置(补词)就永远提不出来。
_DIFF_PRIORITY = ("metrics", "filters", "dims", "time", "compare", "order_by", "limit")


def _category_of_diff(diff: dict[str, tuple[Any, Any]]) -> str:
    heads = {key.split("[")[0].split(".")[0] for key in diff}
    for head in _DIFF_PRIORITY:
        if head in heads:
            category = _DIFF_TO_CATEGORY.get(head)
            if category:
                return category
    return "dsl"


def _disposition(category: str, row: dict[str, Any]) -> str:
    if category == "link":
        return DISPOSITION_TERM
    if category == "dsl":
        return DISPOSITION_GOLDEN
    if category == "intent":
        return DISPOSITION_GOLDEN if row.get("feedback") == "rewritten" else DISPOSITION_REVIEW
    return DISPOSITION_REVIEW


def apply_disposition(
    row: dict[str, Any], *, category: str, disposition: str, corrected_dsl: Any = None
) -> dict[str, Any]:
    """把归因结论落成草案(或只登记), 全部标成"未生效"。"""
    result = {"category": category, "disposition": disposition, "written": ""}
    if disposition == DISPOSITION_TERM:
        term, target = _pick_term(row)
        if term and target and _draft_term(term, target):
            result["written"] = f"sem_terms:draft:{term}->{target}"
        return result
    if disposition == DISPOSITION_GOLDEN:
        dsl = corrected_dsl or row.get("dsl_json") or {}
        if isinstance(dsl, str):
            dsl = {"raw": dsl[:1500]}
        from app.askdata import store

        out = store.upsert_golden(
            question=str(row.get("question") or ""),
            dsl=dsl,
            expected_sql="",
            tags="badcase," + category,
            note=f"由 badcase 归因自动生成(类别={category}), 需人工核对口径后再启用",
            created_by=str(row.get("user_id") or "") or "askdata-badcase",
            enabled=False,
        )
        if out.get("status") == "ok":
            result["written"] = f"askdata_golden:draft:{out['case_id']}"
        result["sample"] = _maybe_sample(row, category=category, corrected=corrected_dsl)
        return result
    result["sample"] = _maybe_sample(row, category=category, corrected=corrected_dsl)
    return result


def _maybe_sample(row: dict[str, Any], *, category: str, corrected: Any = None) -> dict[str, Any]:
    """把这一轮写成 SFT/DPO 候选(先 draft, 且诚实记两道门禁)。

    只有 intent/dsl 两类产生样本: link 的正确处置是补词, exec 是权限/成本, render 是规则引擎 ——
    它们都不是"模型应该学会这个映射"的材料, 拿它们训练等于把口径问题当语言问题治。
    """
    from app.askdata import store

    if category not in store.TRAINABLE_CATEGORIES:
        return {"skipped": f"类别 {category} 不是训练材料"}
    question = str(row.get("question") or "").strip()
    if not question:
        return {"skipped": "缺原话"}
    verdict = str(row.get("verdict") or "").lower()
    feedback = str(row.get("feedback") or "").lower()
    own_dsl = _as_dict_value(row.get("dsl_json"))
    if corrected:
        # dpo: 用户给出的那一版是 chosen, 模型当时的是 rejected
        chosen = _as_dict_value(corrected)
        rejected = own_dsl
        kind = "dpo"
        confirmed = True
    elif feedback == "up" and verdict == "ok":
        chosen, rejected, kind, confirmed = own_dsl, None, "sft", True
    else:
        return {"skipped": "既无用户确认也无肯定反馈"}
    validated = _dsl_is_executable(chosen) if kind == "dpo" else (verdict == "ok" and bool(own_dsl))
    out = store.record_sample(
        kind=kind, question=question, chosen=chosen, rejected=rejected,
        slots=_as_dict_value(row.get("slots_json")),
        query_id=str(row.get("query_id") or ""), trace_id=str(row.get("trace_id") or ""),
        user_id=str(row.get("user_id") or ""), role=str(row.get("role") or ""),
        semver=str(row.get("semver") or ""), category=category,
        gate_validated=bool(validated), gate_confirmed=bool(confirmed),
        note=f"归因路由自动产生(category={category})",
    )
    return out


def _as_dict_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _dsl_is_executable(dsl: Any) -> bool:
    """这份 DSL 能不能过校验器并编成 SQL(dpo 的 chosen 必须真能跑)。

    为什么能在网关做: 校验与编译都只需要语义层快照, 不碰业务数据也不代用户执行 SQL;
    拿"看起来对"的 DSL 当 chosen 去训模型, 比不训更坏。
    """
    try:
        from app.config import get_settings
        from app.semantic import compiler, registry
        from app.semantic.dsl import parse_dsl
        from app.semantic.validate import CallerScope, validate

        settings = get_settings()
        spec = parse_dsl(dsl)
        catalog = registry.get_catalog()
        # 校验用 CallerScope(它的契约), 编译用完整 DataScope(它负责注入域谓词) ——
        # 两个形状不能拿一个对象充, 否则 `not caller.all_depts` 会把方法当假值判。
        check = validate(spec, catalog=catalog, scope=CallerScope(role="probe"), today=None,
                         max_rows=settings.askdata_max_rows,
                         max_time_span_days=settings.askdata_max_time_span_days)
        if not check.ok:
            return False
        compiler.compile_analytical(spec, catalog=catalog,
                                    scope=_probe_scope(), check=check,
                                    max_rows=settings.askdata_max_rows)
        return True
    except Exception as exc:  # noqa: BLE001 - 编不出就是不该进训练集
        logger.debug("[askdata] chosen DSL 不可执行(不进样本): %s", str(exc)[:160])
        return False


def _probe_scope():
    """构造一个真实的 `DataScope`(只用于编译期拼谓词, 本函数不执行任何 SQL)。

    为什么自己手一个形状而不是复用一个人的作用域: 把"能不能编译"与"谁能看到哪些行"
    混在一处, 日后有人改成真实身份时这道门会默默变化。
    """
    from app.db.scope import DataScope

    return DataScope(tenant_id="__probe__", dept_ids=("__probe__",), all_depts=False,
                     user_id="__probe__", role="probe", department="(编译探测)")


def _pick_term(row: dict[str, Any]) -> tuple[str, str]:
    """取出 ``(要补的说法, 目标指标 id)``: 说法用用户原话, 目标用当时最高分候选。"""
    question = str(row.get("question") or "").strip()
    candidates = _as_list(row.get("candidates"))
    top = next((c for c in candidates if isinstance(c, dict) and c.get("kind") == "metric"), None)
    if top is None:
        # 一个候选都没命中: 补词没有目标可以指向, 这种情形只能人来判。_draft_term 会拒。
        return (question[:64] if question else ""), ""
    return (question[:64] if question else ""), str(top.get("id") or "")


def _draft_term(term: str, target: str) -> bool:
    """写一条 ``sem_terms`` 草案(status=draft, 不参与召回), 成功返回 True。

    重复写入靠 (term, term_type, target_id) 唯一约束: 同一个说法被点踩十次也只
    留一条待办, 否则草案表会被重复噪声埋掉。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import sync as dbsync
    from app.db.models import TermAlias

    if not term or not target:
        return False
    stmt = pg_insert(TermAlias.__table__).values(
        term=term, term_type="metric", target_id=target, weight=0.6,
        source="badcase", status="draft",
    ).on_conflict_do_nothing(index_elements=["term", "term_type", "target_id"])
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(stmt)
    except Exception as exc:  # noqa: BLE001 - 草案写不进去只意味着这条 badcase 没有产出待办
        logger.warning("[askdata] 补词草案写入失败: %s", exc)
        return False
    # 草案是 draft 状态, 不会进召回集, 因此不需要 invalidate; 只有人工转正时才需要。
    return True


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    try:
        import json

        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
