"""时间语义(层 2 的一部分): 把"上个月/今年至今"解释成唯一的左闭右开区间。

为什么单独一个模块而不是让模型直接给日期: 相对时间是问数里最高频的错源 ——
"上个月"到底是 9/1~9/30 还是 30 天滑窗、本周从周一还是周日算、跨年环比的上一期是哪三个月,
每次都让 LLM 现推就会每次不一样。这里把解释权收归服务端: DSL 只允许出现**词表内的区间词**
(或显式 ISO 日期), 具体区间、粒度、对比期全部由本模块确定性算出, 且必须显式回给用户看。

口径与周报一致(见 app/analytics/reports.resolve_range): ISO 周(周一起)、自然月、
左闭右开(上界取"下一周期首日"), 日历基准是东八区 —— 容器 TZ 默认 UTC, 直接用
``date.today()`` 会差 8 小时。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.semantic.dsl import SpecTime

_CST = timezone(timedelta(hours=8))

# 区间词表: DSL 里只允许出现这些值(或显式 start/end)。
RANGE_TOKENS: frozenset[str] = frozenset({
    "this_week", "last_week", "this_month", "last_month",
    "this_quarter", "last_quarter", "this_year", "last_year",
    "last_7d", "last_30d", "last_90d", "last_365d", "all_time",
})

# 中文/口语说法 -> 区间词。刻意只做"确定性可判"的同义替换, 判不出的一律走澄清门。
_SYNONYMS: dict[str, str] = {
    "本周": "this_week", "这周": "this_week", "当周": "this_week",
    "上周": "last_week", "上一周": "last_week", "前一周": "last_week",
    "本月": "this_month", "这个月": "this_month", "当月": "this_month",
    "上月": "last_month", "上个月": "last_month", "上一月": "last_month",
    "本季度": "this_quarter", "本季": "this_quarter", "当季": "this_quarter",
    "上季度": "last_quarter", "上一季度": "last_quarter", "去年四季": "last_quarter",
    "今年": "this_year", "本年度": "this_year", "年初至今": "this_year", "ytd": "this_year",
    "去年": "last_year", "上一年": "last_year",
    "近一周": "last_7d", "最近7天": "last_7d", "近7天": "last_7d",
    "近一月": "last_30d", "最近30天": "last_30d", "近30天": "last_30d",
    "近三月": "last_90d", "最近90天": "last_90d", "近90天": "last_90d",
    "近一年": "last_365d", "最近365天": "last_365d",
    "全部": "all_time", "所有": "all_time", "历史": "all_time", "总计": "all_time",
}

# 默认粒度是 "none"(整个区间一个聚合值)而不是"按天展开": 用户问"本月各部门报销总额"
# 要的是每个部门一个数, 而不是 部门×天 的 30 行。只有明说"趋势/每天/按月"时
# 才会有粒度(由槽位抽取负责), 所以这里不能替用户默认一个时序粒度。
# 粒度与区间不搭时(如 all_time)在 :func:`resolve` 里显式拒, 不默默展开。

# 澄清面板上的时间段候选(顺序即展示顺序)。
TIME_CHOICES: tuple[tuple[str, str], ...] = (
    ("this_month", "本月"),
    ("last_month", "上月"),
    ("this_quarter", "本季度"),
    ("this_year", "今年至今"),
    ("last_30d", "近 30 天"),
)


class TimeError(ValueError):
    """时间语义解析不出来(拒因要能直接回给用户, 不含内部术语)。"""


@dataclass(frozen=True)
class ResolvedRange:
    """一段确定的统计区间 + 它的对比期。"""

    token: str
    start: date | None
    end: date | None          # 左闭右开
    gran: str
    label: str
    prev_start: date | None = None
    prev_end: date | None = None
    compare: str = "none"
    whole: bool = False       # all_time: 不加时间谓词
    note: str = ""            # 对比期的人读说明("环比(上一月同期)")

    @property
    def days(self) -> int:
        if self.start is None or self.end is None:
            return 0
        return (self.end - self.start).days

    def to_payload(self) -> dict[str, object]:
        """进澄清/结果载荷的可读形式(人看的 label 与机器看的日期都给)。"""
        out: dict[str, object] = {
            "range": self.token,
            "granularity": self.gran,
            "label": self.label,
        }
        if self.whole:
            out["range_note"] = "全部历史(不限时间)"
            return out
        out["start"] = self.start.isoformat() if self.start else ""
        out["end"] = self.end.isoformat() if self.end else ""
        out["range_note"] = f"{out['start']} ~ {out['end']}(左闭右开)"
        if self.compare != "none" and self.prev_start and self.prev_end:
            out["prev_start"] = self.prev_start.isoformat()
            out["prev_end"] = self.prev_end.isoformat()
            out["compare"] = self.compare
            if self.note:
                out["compare_note"] = self.note
        return out


def today_cst() -> date:
    """东八区今天(与 reports.current_cst_date 同口径)。"""
    return datetime.now(_CST).date()


def normalize_range(raw: str) -> str:
    """把口语说法归一到区间词; 归不出来抛 :class:`TimeError`(让澄清门接管, 而不是猜)。"""
    text = (raw or "").strip()
    if not text:
        return ""
    key = text.lower().replace(" ", "")
    if key in RANGE_TOKENS:
        return key
    hit = _SYNONYMS.get(text) or _SYNONYMS.get(key)
    if hit:
        return hit
    raise TimeError(f"无法识别的时间说法: {raw!r}")


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _quarter_start(d: date) -> date:
    return d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1)


def _add_months(d: date, months: int) -> date:
    """按月平移并对月末取整(1/31 平移一个月是 2/28, 不是 3/3)。"""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2/29 -> 平年 2/28
        return d.replace(year=d.year + years, month=2, day=28)


def _coerce(time: SpecTime | dict | None) -> SpecTime:
    """把 dict 形状的时间槽转成 SpecTime, 并把 pydantic 错统一成 :class:`TimeError`。

    不让 ValidationError 外溢: 上层(校验器与编译器)统一只认 TimeError/DslError 两种拒因。
    """
    try:
        return SpecTime.model_validate(time or {})
    except Exception as exc:  # noqa: BLE001 - pydantic 的错要压成一句可回给模型的拒因
        raise TimeError(f"时间槽形状不合法: {exc}") from exc


def resolve(
    time: SpecTime | dict | None,
    *,
    compare: str = "none",
    today: date | None = None,
    default_gran: str = "none",
    max_span_days: int | None = None,
) -> ResolvedRange:
    """把 DSL 的时间槽解析成确定区间; 槽位空则抛 :class:`TimeError`(触发澄清)。

    ``default_gran`` 只在用户没明说粒度时用, 默认 ``none`` = 不按时序展开。
    """
    raw = time if isinstance(time, SpecTime) else _coerce(time)
    day = today or today_cst()
    gran = raw.granularity or (default_gran or "none")

    if raw.start and raw.end:
        try:
            start = date.fromisoformat(raw.start)
            end = date.fromisoformat(raw.end)
        except ValueError as exc:
            raise TimeError(f"日期不合法: {exc}") from exc
        if end <= start:
            raise TimeError(f"结束日期必须晚于开始日期({raw.start} ~ {raw.end})")
        token = "custom"
        label = f"{raw.start} ~ {raw.end}(自定义区间)"
    elif raw.start or raw.end:
        raise TimeError("显式区间必须同时给出 start 与 end")
    else:
        token = normalize_range(raw.range)
        if not token:
            raise TimeError("缺少时间范围")
        start, end, label = _bounds(token, day)

    if max_span_days is not None and start and end and (end - start).days > max_span_days:
        raise TimeError(
            f"时间跨度 {(end - start).days} 天超过上限 {max_span_days} 天, 请缩小范围"
        )

    whole = token == "all_time"
    if whole:
        start, end = None, None
        if compare != "none":
            raise TimeError("全部历史无法做同环比, 请先给一个时间区间")

    resolved = ResolvedRange(
        token=token, start=start, end=end, gran=gran, label=label,
        compare=compare, whole=whole,
    )
    if compare != "none" and not whole:
        resolved = _with_compare(resolved, compare)
    return resolved


def _bounds(token: str, day: date) -> tuple[date, date, str]:
    """区间词 -> (start, end 左闭右开, 人类可读标签)。"""
    if token == "this_week":
        start = day - timedelta(days=day.weekday())
        return start, day + timedelta(days=1), f"{start.isoformat()} ~ {(day + timedelta(days=1)).isoformat()}(本周至今)"
    if token == "last_week":
        end = day - timedelta(days=day.weekday())
        start = end - timedelta(days=7)
        return start, end, f"{start.isoformat()} ~ {end.isoformat()}(上一整周)"
    if token == "this_month":
        start = _month_start(day)
        return start, day + timedelta(days=1), f"{start.isoformat()} ~ {(day + timedelta(days=1)).isoformat()}(本月至今)"
    if token == "last_month":
        end = _month_start(day)
        start = _add_months(end, -1)
        return start, end, f"{start.isoformat()} ~ {end.isoformat()}(上一整月)"
    if token == "this_quarter":
        start = _quarter_start(day)
        return start, day + timedelta(days=1), f"{start.isoformat()} ~ {(day + timedelta(days=1)).isoformat()}(本季度至今)"
    if token == "last_quarter":
        end = _quarter_start(day)
        start = _add_months(end, -3)
        return start, end, f"{start.isoformat()} ~ {end.isoformat()}(上一整季)"
    if token == "this_year":
        start = day.replace(month=1, day=1)
        return start, day + timedelta(days=1), f"{start.isoformat()} ~ {(day + timedelta(days=1)).isoformat()}(今年至今)"
    if token == "last_year":
        start = day.replace(year=day.year - 1, month=1, day=1)
        end = day.replace(month=1, day=1)
        return start, end, f"{start.isoformat()} ~ {end.isoformat()}(上一整年)"
    for suffix, days in (("last_7d", 7), ("last_30d", 30), ("last_90d", 90), ("last_365d", 365)):
        if token == suffix:
            start = day - timedelta(days=days)
            return start, day + timedelta(days=1), f"{start.isoformat()} ~ {(day + timedelta(days=1)).isoformat()}(近 {days} 天)"
    if token == "all_time":
        return day, day, "全部历史(不限时间)"
    raise TimeError(f"内部错误: 未实现的区间词 {token!r}")


# 时词 -> 平移单位: 粒度为 none 时靠它定对比期。
# 为什么不用"区间长度平移": 本月至今是 10 天, 按 10 天回退得 9/21~10/1 —— 那是
# “上一段等长区间”而不是“上月同期”, 环比会算出一个没人想看的数。M TD 的环比
# 惯例是上月同日段(1/1~1/15 对 2/1~2/15), 所以这里按区间词自己的周期平移。
_TOKEN_SHIFT: dict[str, str] = {
    "this_week": "week", "last_week": "week",
    "this_month": "month", "last_month": "month",
    "this_quarter": "quarter", "last_quarter": "quarter",
    "this_year": "year", "last_year": "year",
}


def _with_compare(r: ResolvedRange, compare: str) -> ResolvedRange:
    """算对比期: yoy 平移一年; mom 优粒度、粒度不明时按区间词自己的周期平移。"""
    if r.start is None or r.end is None:
        return r
    step = r.gran if r.gran != "none" else _TOKEN_SHIFT.get(r.token, "span")
    if compare == "yoy":
        prev_start, prev_end = _add_years(r.start, -1), _add_years(r.end, -1)
        note = "同比(去年同期)"
    elif compare == "mom":
        if step == "month":
            prev_start, prev_end = _add_months(r.start, -1), _add_months(r.end, -1)
            note = "环比(上一月同期)"
        elif step == "quarter":
            prev_start, prev_end = _add_months(r.start, -3), _add_months(r.end, -3)
            note = "环比(上一季度同期)"
        elif step == "year":
            prev_start, prev_end = _add_years(r.start, -1), _add_years(r.end, -1)
            note = "环比(去年同期)"
        elif step == "week":
            prev_start, prev_end = r.start - timedelta(days=7), r.end - timedelta(days=7)
            note = "环比(上一周同期)"
        elif step == "day":
            prev_start, prev_end = r.start - timedelta(days=1), r.end - timedelta(days=1)
            note = "环比(前一天)"
        else:  # 自定义区间等拿不到周期的场合: 按整段长度回退
            span = r.end - r.start
            prev_start, prev_end = r.start - span, r.start
            note = f"环比(上一个等长区间 {span.days} 天)"
    else:
        return r
    return ResolvedRange(
        token=r.token, start=r.start, end=r.end, gran=r.gran,
        label=f"{r.label} · {note}",
        prev_start=prev_start, prev_end=prev_end, compare=compare, whole=r.whole,
        note=note,
    )


def time_choices() -> list[dict[str, str]]:
    """澄清面板的时间段候选(选项式, 不给开放式反问)。"""
    return [{"token": token, "label": label} for token, label in TIME_CHOICES]
