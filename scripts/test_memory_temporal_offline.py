"""个人记忆双时态(时间序列属性)的离线单测: 不需要整栈, 不需要 LLM/DB/网络。

跑法::

    uv run python -m scripts.test_memory_temporal_offline

覆盖"波动类属性不能按写入顺序覆盖"这条主线, 全部是可离线判定的纯函数:
  1. temporal: 生效时间解析(含历史年份/中文季节/荒谬值)、值内嵌时间剥离;
  2. profile_store.merge_attributes: 当前值按生效时间派生、历史入列不覆盖当前态、
     三类键(观测/修正/多值)各自的语义、观测数封顶、老行自愈;
  3. profile_store.render_summary: 只有用户明说时间才渲染"（自 YYYY-MM）", 历史值不进 prompt;
  4. extraction._parse: profile 的 at 落到 valid_at/explicit, 键名去时间修饰, 关系带 valid_at;
  5. vector_store 的观测窗口判定与 occurred_at 不回退(纯函数部分, 不连库)。

端到端(真提取 + 真落库 + 召回)在 docker 容器里走整栈验证, 见 README 记忆层小节。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from app.memory.extraction import _parse
from app.memory.profile_store import merge_attributes, render_summary
from app.memory.temporal import parse_embedded_date, parse_event_time
from app.memory.vector_store import _is_same_observation, _later

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []

# 固定"现在": 全部断言只依赖注入时间, 不依赖真实时钟, 跨天跑也不会飘。
NOW = datetime(2026, 9, 29, 3, 0, 0, tzinfo=timezone.utc)
AUTUMN_2015 = datetime(2015, 10, 1, tzinfo=timezone.utc)


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def _item(key: str, value: str, at: str = "", *, explicit: bool | None = None, now: datetime = NOW) -> dict:
    """模拟提取器的一条画像原子: ``at`` 为空即"用户没提时间, 本轮日期当下生效"。"""
    return {
        "key": key,
        "value": value,
        "valid_at": parse_event_time(at) if at else now,
        "explicit": (bool(at) if explicit is None else explicit),
    }


def test_temporal_parse() -> None:
    check("ISO 年月能解析", parse_event_time("2015-10") == AUTUMN_2015, str(parse_event_time("2015-10")))
    check("中文季节能落到月", parse_event_time("2015年秋") == datetime(2015, 9, 1, tzinfo=timezone.utc))
    check("历史年份不丢(画像要能排序十年前)", parse_event_time("2015-10-08") is not None)
    check("未来时间当猜错", parse_event_time((NOW + timedelta(days=90)).strftime("%Y-%m-%d")) is None)
    check("1900 年前当脏值", parse_event_time("1899-05") is None)
    check("空串解析不出", parse_event_time("") is None)

    value, stamp = parse_embedded_date("64kg（2015年秋）")
    check("值里的时间被剥到生效轴", (value, stamp) == ("64kg", datetime(2015, 9, 1, tzinfo=timezone.utc)), f"{value} {stamp}")
    value, stamp = parse_embedded_date("70kg")
    check("纯值不受影响", (value, stamp) == ("70kg", None), f"{value} {stamp}")
    value, _ = parse_embedded_date("173cm（赤脚）")
    check("非时间的括号说明保留", value == "173cm（赤脚）", value)
    value, stamp = parse_embedded_date("体重（2015年）")
    check("键名里的时间也能剥出", (value, stamp) == ("体重", datetime(2015, 1, 1, tzinfo=timezone.utc)), f"{value} {stamp}")


def test_measure_keys_never_overwritten_by_past_statements() -> None:
    """事故现场: 先说"现在 70kg", 后说"2015 年秋 64kg" —— 当前值必须仍是 70kg。"""
    attrs, history, added, _updated, superseded = merge_attributes({}, [_item("体重", "70kg")], now=NOW)
    check("第一轮: 当前值 = 70kg", attrs.get("体重") == ["70kg"], str(attrs))
    attrs, history, _a, updated, superseded = merge_attributes(
        attrs, [_item("体重", "64kg", "2015-10")], history=history, now=NOW + timedelta(days=1)
    )
    check("历史陈述不改当前值", attrs.get("体重") == ["70kg"], str(attrs))
    check("历史陈述计入 superseded", superseded == 1, f"superseded={superseded}")
    check("历史陈述算一次更新(库里确实多了观测)", updated == 1, f"updated={updated}")
    check("旧值进历史且带生效时间", _has(history, "体重", "64kg"), str(history))

    # 反过来说: 先讲过去, 再讲现在 —— 现在那条应当胜出。
    attrs, history, *_rest = merge_attributes({}, [_item("体重", "64kg", "2015-10")], now=NOW)
    check("首轮就是历史值: 当前值取它", attrs.get("体重") == ["64kg"], str(attrs))
    attrs, history, *_rest = merge_attributes(attrs, [_item("体重", "70kg")], history=history, now=NOW)
    check("后来的当下陈述顶掉更早的历史值", attrs.get("体重") == ["70kg"], str(attrs))
    check("被顶掉的 64kg 仍在历史里", _has(history, "体重", "64kg"), str(history))


def test_single_statement_semantics_per_key_class() -> None:
    # 没标时间的两次陈述: 后听到的赢(今天的修正语义不能因为改造而退化)。
    attrs, history, *_r = merge_attributes({}, [_item("体重", "70kg")], now=NOW)
    later = NOW + timedelta(hours=1)
    attrs, _h, *_r = merge_attributes(attrs, [_item("体重", "75kg", now=later)], history=history, now=later)
    check("同键未标时间再说一次仍是后者胜", attrs.get("体重") == ["75kg"], str(attrs))

    # 部门变更(两次都是当轮陈述, 第二次带新起始时间): 旧部门入历史, 新部门是当前态。
    attrs, history, *_r = merge_attributes({}, [_item("部门", "研发部", "2026-05")], now=datetime(2026, 5, 20, tzinfo=timezone.utc))
    attrs, history, *_r = merge_attributes(attrs, [_item("部门", "财务部", "2026-08")], history=history, now=NOW)
    check("部门变更后当前值是新部门", attrs.get("部门") == ["财务部"], str(attrs))
    check("旧部门进历史", _has(history, "部门", "研发部"), str(history))

    # 姓名修正不留历史。
    attrs, history, *_r = merge_attributes({"姓名": ["朱"]}, [_item("姓名", "朱斌")], now=NOW)
    check("姓名修正直接覆盖", attrs.get("姓名") == ["朱斌"], str(attrs))
    check("身份类键不产生历史", history == {}, str(history))

    # 多值键行为不变: 只增不删。
    attrs, history, *_r = merge_attributes({"技能": ["Python"]}, [_item("技能", "Go")], now=NOW)
    check("多值键并集累积", attrs.get("技能") == ["Python", "Go"], str(attrs))
    check("多值键不产生历史", "技能" not in history, str(history))

    # 同值同时间再说一遍: 不新增观测, 也不计更新。
    attrs, history, added, updated, superseded = merge_attributes({}, [_item("体重", "70kg")], now=NOW)
    attrs2, history2, added2, updated2, _s = merge_attributes(attrs, [_item("体重", "70kg")], history=history, now=NOW)
    check("同值同时间重复说被忽略", (attrs2, added2, updated2) == (attrs, 0, 0), str((attrs2, added2, updated2)))

    # 隔几天把同一件事(没带新时间)再说一遍: 只推新当前值的时间, 不堆重复历史。
    later_day = NOW + timedelta(days=5)
    attrs, history, added2, updated2, _s = merge_attributes(
        attrs2, [_item("体重", "70kg", now=later_day)], history=history2, now=later_day
    )
    check("隔几天重说同一值不堆历史", attrs.get("体重") == ["70kg"] and len(history.get("体重", [])) == 1, str(history))
    check("重说当前值不计新增/更新", (added2, updated2) == (0, 0), f"added={added2} updated={updated2}")

    # 同一历史陈述隔几天再说一遍: 归一, 不产生第二条同样的历史。
    attrs, history, *_r = merge_attributes({}, [_item("部门", "数据部", "2020-01")], now=NOW)
    attrs, history, *_r = merge_attributes(
        attrs, [_item("部门", "数据部", "2020-01")], history=history, now=NOW + timedelta(days=3)
    )
    check("同一历史陈述重说不堆叠", len(history.get("部门", [])) == 1, str(history))

    # 观测封顶: 超过上限只留最近的, 当前值仍是最晚生效的那条。
    items = [_item("体重", f"{60 + i}kg", f"20{10 + i:02d}-05") for i in range(14)]
    attrs, history, *_r = merge_attributes({}, items, now=NOW)
    check("观测数按上限封顶", len(history.get("体重", [])) == 10, str(len(history.get("体重", []))))
    check("封顶后当前值仍是最晚生效的", attrs.get("体重") == [items[-1]["value"]], str(attrs))


def test_recent_dated_change_beats_same_day_statement() -> None:
    """"从上个月起…" 是持续到当下的变更, 不能被当成历史只入历史。"""
    attrs, history, *_r = merge_attributes({}, [_item("汇报对象", "李总")], now=NOW, grace_days=90)
    last_month = NOW - timedelta(days=55)
    attrs, history, *_r = merge_attributes(
        attrs,
        [_item("汇报对象", "张总", last_month.strftime("%Y-%m"))],
        history=history,
        now=NOW,
        grace_days=90,
    )
    check("近期起始日期的变更成为当前值", attrs.get("汇报对象") == ["张总"], str(attrs))
    check("被取代的旧值入历史", _has(history, "汇报对象", "李总"), str(history))

    # 超出宽限期的日期才算历史: 不能顶掉今天说的当前值。
    attrs, history, *_r = merge_attributes({}, [_item("体重", "70kg")], now=NOW, grace_days=90)
    long_ago = NOW - timedelta(days=120)
    attrs, history, *_r = merge_attributes(
        attrs,
        [_item("体重", "68kg", long_ago.strftime("%Y-%m-%d"))],
        history=history,
        now=NOW,
        grace_days=90,
    )
    check("超出宽限期的陈述只入历史", attrs.get("体重") == ["70kg"], str(attrs))
    check("宽限期外的旧值仍可被回放", _has(history, "体重", "68kg"), str(history))


def test_legacy_row_self_heals() -> None:
    """老库里 ``64kg（2015年秋）`` 这种把时间写进 value 的行要能自己归位。"""
    attrs, history, *_r = merge_attributes(
        {"体重": ["64kg（2015年秋）"], "身高": ["173cm"]}, [], now=NOW, stored_at=NOW
    )
    check("老值剥掉时间后仍是当前值", attrs.get("体重") == ["64kg"], str(attrs))
    top = (history.get("体重") or [{}])[0]
    check("老值的时间归位到 valid_at", str(top.get("valid_at", "")).startswith("2015-09"), str(top))
    entries = history.get("体重") or []
    check("自愈后有一条观测", len(entries) == 1, str(entries))


def test_render_summary() -> None:
    attrs, history, *_r = merge_attributes({}, [_item("体重", "70kg")], now=NOW)
    attrs, history, *_r = merge_attributes(attrs, [_item("身高", "173cm", "2026-01")], history=history, now=NOW)
    text = render_summary(attrs, 400, history=history)
    check("未标时间的值不带日期后缀", "体重：70kg；" in text or text.startswith("体重：70kg"), text)
    check("明说时间的值带生效月", "身高：173cm（自2026-01）" in text, text)

    # 历史值不能挤进 prompt: 被拦下的 64kg 不出现在画像里。
    attrs, history, *_r = merge_attributes({}, [_item("体重", "70kg")], now=NOW)
    attrs, history, *_r = merge_attributes(
        attrs, [_item("体重", "64kg", "2015-10")], history=history, now=NOW + timedelta(days=1)
    )
    text = render_summary(attrs, 400, history=history)
    check("画像摘要只有当前值, 不含被拦下的历史值", text == "体重：70kg", text)


def test_extraction_time_fields() -> None:
    today = datetime(2026, 9, 29, tzinfo=timezone.utc)
    payload = json.dumps(
        {
            "profile": [
                {"key": "体重", "value": "64kg（2015年秋）", "at": "2015-10"},
                {"key": "体重（2020年）", "value": "68kg"},
                {"key": "身高", "value": "173cm", "at": ""},
            ],
            "entities": [
                {"name": "我", "type": "person"},
                {"name": "财务部", "type": "department"},
                {"name": "王总", "type": "person"},
            ],
            "relations": [
                {"src": "我", "dst": "财务部", "relation": "任职于", "valid_at": "2026-08"},
                {"src": "我", "dst": "王总", "relation": "汇报给"},
            ],
        },
        ensure_ascii=False,
    )
    extraction = _parse(payload, today=today)
    weights = [p for p in extraction.profile if p["key"] == "体重"]
    heights = [p for p in extraction.profile if p["key"] == "身高"]
    check("键名时间修饰归一", len(weights) == 2 and all(p["key"] == "体重" for p in weights), str(extraction.profile))
    check("value 里的时间被剥进 valid_at", weights[0]["value"] == "64kg", str(weights[0]))
    check("at 优先于 value 内嵌时间", weights[0]["valid_at"] == datetime(2015, 10, 1, tzinfo=timezone.utc), str(weights[0]))
    check("只写在键名里的时间也算用户明说的时间", weights[1]["valid_at"] == datetime(2020, 1, 1, tzinfo=timezone.utc) and weights[1]["explicit"], str(weights[1]))
    check("没标时间的按本轮日期当下生效", heights[0]["valid_at"] == today and not heights[0]["explicit"], str(heights[0]))
    check("关系补齐 valid_at", extraction.relations[0]["valid_at"] == datetime(2026, 8, 1, tzinfo=timezone.utc), str(extraction.relations[0]))
    check("未标时间的关系按本轮日期成立", extraction.relations[1]["valid_at"] == today, str(extraction.relations[1]))
    check(
        "关系端点未列进 entities 时会被补上(不整条丢掉)",
        all(name in {e["name"] for e in extraction.entities} for name in ("我", "财务部", "王总")),
        str(extraction.entities),
    )

    # 端点完全没列在 entities 里: 关系仍要活下来(补 other 型实体)。
    orphan = _parse(
        json.dumps(
            {"entities": [], "relations": [{"src": "我", "dst": "李总", "relation": "汇报给", "valid_at": "2026-08"}]},
            ensure_ascii=False,
        ),
        today=today,
    )
    check("只出现在关系里的实体不会被丢", len(orphan.relations) == 1 and len(orphan.entities) == 2, str(orphan.entities))


def test_observation_window_and_no_time_regression() -> None:
    """记忆条目侧: 不同时刻的观测各存一条, 合并时 occurred_at 不回退。"""
    check("相隔超出窗口算不同观测", not _is_same_observation(NOW, NOW - timedelta(days=400), 7))
    check("窗口内算同一条观测", _is_same_observation(NOW, NOW - timedelta(days=3), 7))
    check("任一没时间按同一条处理", _is_same_observation(NOW, None, 7) and _is_same_observation(None, NOW, 7))
    old = NOW - timedelta(days=400)
    check("取较新时间时不会回退", _later(NOW, old) == NOW and _later(old, None) == old)
    naive = old.replace(tzinfo=None)
    check("naive 与 aware 混比不抛异常", _later(NOW, naive) == NOW)


def _has(history: dict, key: str, value: str) -> bool:
    return any(str(entry.get("value")) == value for entry in (history.get(key) or []))


def main() -> int:
    test_temporal_parse()
    test_measure_keys_never_overwritten_by_past_statements()
    test_single_statement_semantics_per_key_class()
    test_recent_dated_change_beats_same_day_statement()
    test_legacy_row_self_heals()
    test_render_summary()
    test_extraction_time_fields()
    test_observation_window_and_no_time_regression()
    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
