"""波动事实的双时态(bi-temporal)时间口径: 画像 / 记忆条目 / 图谱关系共用的单一事实源。

要解决的问题: 用户先说"我现在体重 70kg", 后来又提"2015 年秋我 64kg", 按"谁最后
被听到谁赢"的覆盖式合并会把当前态改成一个十年前的值。体重/身高/部门/职位这类
属性本质上不是"身份", 而是**随时间变化的观测序列**, 业界的三条主流路线给的是同
一个答案:

- 健康数据建模(HL7 FHIR ``Observation`` / Apple HealthKit / OpenEHR): 每次测量是
  一条带 ``effectiveDateTime`` 的观测, 观测之间只追加不覆盖, "当前值"按生效时间
  取最新一条**派生**出来;
- 时序知识图谱(Zep / Graphiti): 每条事实带 ``valid_at``/``invalid_at``(现实轴)与
  ``created_at``/``expired_at``(录入轴), 新事实与旧事实矛盾时不删旧边, 只给旧边打
  失效时间; 用户没给时间时 ``valid_at`` 回落到该条输入的发生时间;
- OpenAI 个性化记忆 cookbook: 每条记忆带更新日期, 合并规则明写"两条冲突时按日期
  取最新(prefer the most recent by date)"。

落到代码就是本模块的两轴口径:

- ``valid_at``(现实轴): 这个值在现实中从何时起成立。用户明说的时间("2015 年秋")
  落在这里; 没说时间就回落到本轮对话日期(说"现在"即当下生效)。
- ``recorded_at``(录入轴): 系统何时听到这句话, 只在生效时间打平时决胜。

**当前值由 ``max(生效时间)`` 派生, 而不是由写入顺序决定** —— 生效时间早于当前态的
陈述只能进历史, 不许改写当前值。画像合并(``profile_store``)、记忆条目查重
(``vector_store``)、关系失效(``graph_store``)三处都从这里取同一套判定, 避免同一个
时间语义在三个桶里各写一遍而漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# 早于此年的生效时间一律当作模型猜错(用户真实历史体重落在 1900 之后的概率是全部)。
MIN_VALID_AT_YEAR = 1900
# 晚于"今天 + 1 天"的时间视为猜错或计划值: 计划值不是"已成立的事实"。
FUTURE_TOLERANCE = timedelta(days=1)

# 中文季节粗映射到该季节中位月份: "2015 年秋" -> 2015-09, 够排序也够展示。
_SEASON_MONTH = {"春": 3, "夏": 6, "秋": 9, "冬": 12}

# 值文本里可能夹带的日期写法: 中文"YYYY年[M月][季]"或 ISO"YYYY[-MM[-DD]]"。
_CN_DATE = re.compile(r"(?P<y>\d{4})\s*年\s*(?P<mm>\d{1,2})?\s*(?P<season>[春夏秋冬])?")
_ISO_DATE = re.compile(r"(?P<y>\d{4})[-/](?P<mm>\d{1,2})(?:[-/](?P<dd>\d{1,2}))?")
# 括号内整体是时间修饰时才剥离, 避免把 "173cm(赤脚)" 这类说明文字一起吃掉。
_PARENTHESIZED = re.compile(r"[（(]([^（）()]*)[）)]")

_CST_DATE = timezone(timedelta(hours=8))


def _build(y: int, mm: int, dd: int) -> datetime | None:
    """年月日 -> UTC datetime; 非法组合返回 None。"""
    if y < MIN_VALID_AT_YEAR or not 1 <= mm <= 12 or not 1 <= dd <= 31:
        return None
    try:
        return datetime(y, mm, dd, tzinfo=timezone.utc)
    except ValueError:
        return None


def _from_digits(text: str) -> datetime | None:
    """解析一段日期文本(ISO / 斜杠 / 中文年月季), 只到年或月时按区间起点补齐。"""
    raw = (text or "").strip()
    if not raw:
        return None
    iso = _ISO_DATE.search(raw)
    if iso:
        return _build(int(iso.group("y")), int(iso.group("mm")), int(iso.group("dd") or 1))
    cn = _CN_DATE.search(raw)
    if cn:
        month = int(cn.group("mm") or 0) or _SEASON_MONTH.get(cn.group("season") or "", 1)
        return _build(int(cn.group("y")), month, 1)
    # 裸年份: 退到当年 1 月, 只用于排序不用于展示。
    if re.fullmatch(r"\d{4}", raw):
        return _build(int(raw), 1, 1)
    return None


def parse_event_time(raw: object) -> datetime | None:
    """把 LLM/用户给的生效时间解析成带时区的 datetime; 解析不出或明显荒谬返回 None。

    与 ``extraction._parse_date``(情节 ``occurred_at``)口径不同的地方在于**下界**:
    那条给情节用, 有 ``episodic_window_days`` 时间窗过滤, 错年份会把整条记忆误杀,
    所以保守地只收近三年; 这条给画像与关系用, 时间只参与"谁当当前值"的排序, 猜错
    最多让一条值沉进历史而不会让记忆整条消失, 所以放开到 1900 年(十年前的真实体重
    是合法历史, 不是模型幻觉)。
    """
    stamp = _from_digits(str(raw or ""))
    if stamp is None:
        return None
    now = datetime.now(timezone.utc)
    if stamp > now + FUTURE_TOLERANCE:
        return None
    return stamp


def parse_embedded_date(text: str) -> tuple[str, datetime | None]:
    """从"值"文本里抽出内嵌时间并剥离, 返回 ``(干净值, 生效时间或 None)``。

    存在的理由是老数据自愈: 画像里已经写成了 ``64kg（2015年秋）`` 这种把时间塞进
    value 的形式, 升级后必须能自己把时间挪到 ``valid_at`` 轴上 —— 否则这条值看上去
    是"今天刚听到的", 会一直压住真正的新值。
    """
    raw = (text or "").strip()
    if not raw:
        return "", None
    stamp: datetime | None = None

    def _replace_paren(match: re.Match[str]) -> str:
        nonlocal stamp
        inner = match.group(1).strip()
        if stamp is None and re.search(r"\d{4}", inner):
            parsed = _from_digits(inner)
            if parsed is not None:
                stamp = parsed
                return ""
        return match.group(0)

    cleaned = _PARENTHESIZED.sub(_replace_paren, raw)
    if stamp is None:
        # 括号外的裸时间写法: 尾部的 "2015年秋" / "2015-10"。
        for match in reversed(list(_CN_DATE.finditer(cleaned)) + list(_ISO_DATE.finditer(cleaned))):
            parsed = _from_digits(match.group())
            if parsed is not None:
                stamp = parsed
                cleaned = (cleaned[: match.start()] + cleaned[match.end() :])
                break
    # 整段值本来就是个时间修饰("体重: 2015年秋")时留空, 由调用方按无效属性丢弃。
    return re.sub(r"[\s、,，;；]+$", "", cleaned).strip(), stamp


@dataclass(frozen=True)
class TimedValue:
    """一个带两轴时间的属性值: 现实何时成立(``valid_at``) + 系统何时听到(``recorded_at``)。

    ``explicit`` 标记时间是不是用户明说的: 只有明说的才会在画像摘要里渲染成
    "（自 2015-09）" —— 未明说的按"当下生效"处理, 给每个值都挂上记录时间既噪声也大
    地挤占 ``profile_max_chars`` 预算。
    """

    value: str
    valid_at: datetime | None = None
    recorded_at: datetime | None = None
    explicit: bool = False

    @property
    def effective_at(self) -> datetime | None:
        """排序锚点: 生效时间优先, 没说时间就退回听到它的时间。"""
        return self.valid_at or self.recorded_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "valid_at": self.valid_at.isoformat() if self.valid_at else "",
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else "",
            "explicit": self.explicit,
        }

    @classmethod
    def from_dict(cls, raw: Any, *, fallback_time: datetime | None = None) -> "TimedValue | None":
        """JSON 行 -> TimedValue; 非 dict / 空值返回 None(调用方按脏数据跳过)。"""
        if not isinstance(raw, dict):
            return None
        value = " ".join(str(raw.get("value") or "").split())
        if not value:
            return None
        valid_at = as_datetime(raw.get("valid_at"))
        recorded_at = as_datetime(raw.get("recorded_at")) or fallback_time
        return cls(
            value=value,
            valid_at=valid_at,
            recorded_at=recorded_at,
            explicit=bool(raw.get("explicit")) and valid_at is not None,
        )


def as_datetime(raw: Any) -> datetime | None:
    """时间字段(JSON 文本 / datetime / 脏值) -> 带时区 datetime, 解析不出返回 None。"""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_historical(item: TimedValue, *, now: datetime, grace_days: int) -> bool:
    """这条陈述是不是"在讲过去的一个状态"。

    判据: 用户明说了时间(``explicit``), 且那个时间已经在 ``grace_days`` 之前。
    只说"现在/没提时间"的不算历史(它讲的就是当前态); 明说了时间但就在近期内
    (如"从上个月起我改汇报给张总")也不算历史 —— 那是在描述一个**从过去某个点
    开始并持续到当下**的变更, 拿它的起始日期去跟另一句"今天说的没时间的陈述"
    比大小会误判谁更新, 应该按"后听到的赢"处理。
    """
    effective = item.effective_at
    if not item.explicit or effective is None:
        return False
    return effective < now - timedelta(days=max(1, int(grace_days)))


def history_rank(item: TimedValue, *, now: datetime, grace_days: int) -> tuple[int, float]:
    """当前态优先的排序键(供**降序**排序使用): 当前态组给更大组序, 组内按时间倒序。

    - 当前态(非历史)组 = 组序 1: 按 ``recorded_at``(何时听到的) 排 —— 同一槽位被更新时,
      后说的算当前值;
    - 历史组 = 组序 0: 按 ``valid_at``(现实中何时成立) 排 —— 补说的陈年旧事按年代远近排序。

    两条业界口径在这里各管各的适用范围: Graphiti 的"旧事实不因新事实而失效于更早时间"
    管住历史陈述不得改写当前态, OpenAI 记忆 cookbook 的"冲突按日期取最新"管住同一
    个槽位上的多次表态。
    """
    if is_historical(item, now=now, grace_days=grace_days):
        stamp = item.effective_at
        return (0, stamp.timestamp() if stamp else float("-inf"))
    stamp = item.recorded_at or item.effective_at
    return (1, stamp.timestamp() if stamp else float("-inf"))


def utcnow() -> datetime:
    """当前 UTC 时间(带时区): 落库时间戳统一从这里取, 便于离线测试注入。"""
    return datetime.now(timezone.utc)


def format_month(stamp: datetime | None) -> str:
    """生效时间 -> 摘要用的粗粒度文本(``2026-09``); 无时间返回空串。"""
    if stamp is None:
        return ""
    return stamp.strftime("%Y-%m")


def format_day(stamp: datetime | None) -> str:
    """时间 -> ``YYYY-MM-DD`` 文本; 只给到年月的按月初展示, 空值返回空串。"""
    if stamp is None:
        return ""
    return stamp.strftime("%Y-%m-%d")


def today_cst() -> datetime:
    """本轮对话日期(东八区零点): 提取侧未标注时间的属性按它当生效时间。"""
    now = datetime.now(_CST_DATE)
    return datetime(now.year, now.month, now.day, tzinfo=_CST_DATE)
