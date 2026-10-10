"""层 2(只读对偶): 模型交上来的问数 DSL —— 强类型 JSON, 不是 SQL。

与 :mod:`app.db.datadsl`(写意图 DSL)是同一种设计: 把模型的自由度收尽, 让"能不能算出
来越权的东西"这个问题在语法层就不成立。

1. **没有标识符自由**: DSL 里出现的是 ``metric_id`` / ``dim_id``, 都必须命中语义层
   登记表; 列名永远由服务端从登记表取出, 不来自模型输出。
2. **没有值拼接自由**: 过滤值最终都是绑定参数, 经典注入在结构上不成立。
3. **没有作用域自由**: ``tenant_id`` / ``dept_id`` 连出现在名单里都没有, 由服务端注入。
4. **没有算术自由**: 环比/同比/占比交给编译器与引擎, 模型不算数 —— 这是 LLM 最容易
   出错的一类(把两个月的数字相减再"约等于")。

时间范围刻意只允许**词表内的相对区间**或显式 ISO 日期: 相对区间由服务端按东八区日历
解释(见 :mod:`app.semantic.timesem`), 于是"上个月"到底是哪 30 天不取决于模型的心情。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.datadsl import FilterOp
from app.db.policy import SCOPE_COLUMNS

# 形状上限: 一次问数不该同时看 10 个指标 8 个维度(那既画不出图也不是人话)。
MAX_METRICS = 4
MAX_DIMS = 4
MAX_FILTERS = 8
MAX_IN_VALUES = 50
MAX_VALUE_CHARS = 200
MAX_RESULT_ROWS = 500
HARD_ROW_CAP = 200  # 与 settings.askdata_max_rows 取小后仍是"给人看的量级"

# 允许的比较运算(与写 DSL 同一套 op 词表, 避免第二套口径)。
_ALLOWED_OPS: frozenset[FilterOp] = frozenset(
    {
        FilterOp.EQ, FilterOp.NE, FilterOp.LT, FilterOp.LTE,
        FilterOp.GT, FilterOp.GTE, FilterOp.IN, FilterOp.IS_NULL,
    }
)

INTENTS = ("取数", "归因", "预测")
COMPARE_TYPES = ("none", "mom", "yoy")
GRANULARITIES = ("day", "week", "month", "quarter", "year", "none")


class DslError(ValueError):
    """DSL 结构/字段/值不合法(拒因必须确定, 不带猜测)。"""


def _check_scalar(value: Any, *, where: str) -> None:
    """值只允许标量且长度受限(理由同 datadsl._check_scalar)。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str):
            if "\x00" in value:
                raise DslError(f"{where} 含空字符, 已拒")
            if len(value) > MAX_VALUE_CHARS:
                raise DslError(f"{where} 超长(>{MAX_VALUE_CHARS} 字符)")
        return
    raise DslError(f"{where} 只允许标量值, 收到 {type(value).__name__}")


def _ident(value: str, *, where: str) -> str:
    """标识符形状校验: 语义对象的 id 会进 SQL 文本的列位/别名位, 必须像标识符。"""
    name = (value or "").strip()
    if not name or not name.replace("_", "").replace("-", "").isalnum() or name[0].isdigit():
        raise DslError(f"{where} 不是合法标识符: {value!r}")
    if name in SCOPE_COLUMNS:
        raise DslError(f"{where} 是服务端注入的作用域列({name}), 不许出现在查询里")
    return name


class SpecMetric(BaseModel):
    """一个要看的指标(只允许引用语义层登记过的 metric_id)。"""

    id: str
    # 覆盖语义层的默认聚合(如把 sum 改成 avg); 只接受固定词表, 不接受表达式。
    agg: str | None = None
    label: str = ""

    @field_validator("id")
    @classmethod
    def _id_ident(cls, v: str) -> str:
        return _ident(v, where="metrics[].id")

    @field_validator("agg")
    @classmethod
    def _agg_known(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        agg = str(v).strip().lower()
        if agg not in ("sum", "count", "avg", "max", "min", "count_distinct"):
            raise DslError(f"agg 只允许 sum/count/avg/max/min/count_distinct, 收到 {v!r}")
        return agg


class SpecTime(BaseModel):
    """时间范围: 相对区间词 或 显式 ISO 日期(start/end), 二者都缺 = 让澄清门来问。"""

    range: str = ""
    granularity: str = ""
    start: str = ""
    end: str = ""

    @field_validator("range")
    @classmethod
    def _range_shape(cls, v: str) -> str:
        """区间词: 先走词表, 再走同义说法归一("上个月" -> last_month)。

        不在这里归一就得在编译器里到处判两种写法; 归一失败不猜近似区间, 直接拒
        (拒因会变成一次重写或一次澄清)。
        """
        from app.semantic import timesem

        text = (v or "").strip().lower().replace(" ", "")
        if not text:
            return ""
        try:
            return timesem.normalize_range(text)
        except timesem.TimeError as exc:
            raise DslError(
                f"时间区间词不认识: {v!r}; 可用: {sorted(timesem.RANGE_TOKENS)}; "
                "或改用显式 start/end(YYYY-MM-DD)"
            ) from exc

    @field_validator("granularity")
    @classmethod
    def _gran_known(cls, v: str) -> str:
        text = (v or "").strip().lower()
        if not text:
            return ""
        if text not in GRANULARITIES:
            raise DslError(f"granularity 只允许 {list(GRANULARITIES)}, 收到 {v!r}")
        return text

    @field_validator("start", "end")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            return ""
        # 只验形状, 不验"是不是真实存在的日子": 交给 timesem 解析, 拒因更准确。
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            raise DslError(f"日期必须是 YYYY-MM-DD, 收到 {v!r}")
        return text


class SpecFilter(BaseModel):
    """一个业务过滤条件(不含作用域列)。"""

    dim: str
    op: FilterOp = FilterOp.EQ
    value: Any = None

    @field_validator("dim")
    @classmethod
    def _dim_ident(cls, v: str) -> str:
        return _ident(v, where="filters[].dim")

    @field_validator("op", mode="before")
    @classmethod
    def _op_allowed(cls, v: Any) -> Any:
        op = FilterOp(str(v or "eq"))
        if op not in _ALLOWED_OPS:
            raise DslError(f"不支持的比较符: {v!r}")
        return op

    @model_validator(mode="after")
    def _value_shape(self) -> "SpecFilter":
        if self.op is FilterOp.IS_NULL:
            return self
        if self.op is FilterOp.IN:
            if not isinstance(self.value, (list, tuple)) or not self.value:
                raise DslError("op=in 需要非空的值列表")
            if len(self.value) > MAX_IN_VALUES:
                raise DslError(f"op=in 的值最多 {MAX_IN_VALUES} 个")
            for i, item in enumerate(self.value):
                _check_scalar(item, where=f"filters[{self.dim}][{i}]")
            return self
        _check_scalar(self.value, where=f"filters[{self.dim}]")
        if self.value is None:
            raise DslError(f"filters[{self.dim}] 的值不能为空(要判空请用 op=is_null)")
        return self


class SpecCompare(BaseModel):
    """同环比: 计算交给编译器, 模型只声明"要对比"。"""

    type: str = "none"

    @field_validator("type")
    @classmethod
    def _type_known(cls, v: str) -> str:
        text = (v or "none").strip().lower()
        if text not in COMPARE_TYPES:
            raise DslError(f"compare.type 只允许 {list(COMPARE_TYPES)}, 收到 {v!r}")
        return text


class QuerySpec(BaseModel):
    """一次问数的完整意图(状态机里流转的就是它)。"""

    metrics: list[SpecMetric] = Field(min_length=1, max_length=MAX_METRICS)
    dims: list[str] = Field(default_factory=list)
    time: SpecTime = Field(default_factory=SpecTime)
    filters: list[SpecFilter] = Field(default_factory=list, max_length=MAX_FILTERS)
    compare: SpecCompare = Field(default_factory=SpecCompare)
    order_by: str = ""
    limit: int | None = None
    intent: str = "取数"

    @field_validator("dims")
    @classmethod
    def _dims_ident(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_DIMS:
            raise DslError(f"dims 最多 {MAX_DIMS} 个")
        seen: list[str] = []
        for item in v:
            dim = _ident(item, where="dims[]")
            if dim not in seen:
                seen.append(dim)
        return seen

    @field_validator("order_by")
    @classmethod
    def _order_ident(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            return ""
        direction = ""
        if " " in text:
            text, _, direction = text.partition(" ")
            direction = direction.strip().lower()
            if direction not in ("asc", "desc"):
                raise DslError("order_by 只允许尾随 asc/desc")
        name = _ident(text, where="order_by")
        return f"{name} {direction}".strip() if direction else name

    @field_validator("intent")
    @classmethod
    def _intent_known(cls, v: str) -> str:
        text = (v or "取数").strip()
        if text not in INTENTS:
            raise DslError(f"intent 只允许 {list(INTENTS)}, 收到 {v!r}")
        return text

    @model_validator(mode="after")
    def _cap_rows(self) -> "QuerySpec":
        if self.limit is None:
            return self
        if self.limit <= 0:
            raise DslError("limit 必须是正整数")
        self.limit = min(int(self.limit), MAX_RESULT_ROWS, HARD_ROW_CAP)
        return self

    @property
    def metric_ids(self) -> list[str]:
        return [m.id for m in self.metrics]

    def to_payload(self) -> dict[str, Any]:
        """转成可落库/可进澄清载荷的普通 dict(枚举取 value, 空字段剔除)。"""
        raw = self.model_dump(mode="json", exclude_none=True)
        return _prune(raw)


def _prune(value: Any) -> Any:
    """递归剥掉空串/空列表: DSL 回显给人看时不该带一堆空字段。"""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            cleaned = _prune(item)
            if cleaned in ("", [], {}, None):
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        return [_prune(item) for item in value]
    return value


def parse_dsl(payload: dict | str) -> QuerySpec:
    """把模型给的 JSON(字符串或 dict)转成强类型 DSL。

    与 :func:`app.db.datadsl.parse_plan` 同一口径: 刻意不用 pydantic 的宽松标量转换,
    值语义(``"1"`` vs ``1``)要由维度类型与数据库决定, DSL 层只保证"是个标量"。
    """
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            # 模型偶尔会把 JSON 包进 fenced block; 这里容一次, 但不再往下递归。
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DslError(f"DSL 不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DslError(f"DSL 必须是 JSON 对象, 收到 {type(payload).__name__}")
    try:
        return QuerySpec.model_validate(payload)
    except DslError as exc:
        raise
    except Exception as exc:  # noqa: BLE001 - pydantic 校验错要压成一句可回给模型的拒因
        raise DslError(_short_validation_error(exc)) from exc


def _short_validation_error(exc: Exception) -> str:
    """把 pydantic 的 ValidationError 压成一行可读拒因(要回给模型自我修正)。"""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = []
        for e in errors():
            loc = ".".join(str(item) for item in e.get("loc") or ())
            msg = str(e.get("msg", "")).removeprefix("Value error, ").strip()
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(p for p in parts if p) or str(exc)
    return str(exc)


def render_schema_hint(catalog: Any = None) -> str:
    """把 DSL 形状(可选: 当前可用指标/维度)渲染成 prompt 片段。

    单一事实源: 这份文本由模型字段自动展开, 不会出现"提示词里写着 limit 但校验器不认"。
    """
    hint = (
        "DSL 形状(只允许这些字段, 多余字段会被拒):\n"
        "{\n"
        '  "metrics": [{"id": "<metric_id>", "agg": "sum|count|avg|max|min|count_distinct?"}],\n'
        '  "dims": ["<dim_id>", ...],            // 最多 4 个下钻维度\n'
        '  "time": {"range": "<区间词>", "granularity": "day|week|month|quarter|year|none", '
        '"start": "YYYY-MM-DD?", "end": "YYYY-MM-DD?"},\n'
        '  "filters": [{"dim": "<dim_id>", "op": "eq|ne|lt|lte|gt|gte|in|is_null", "value": ...}],\n'
        '  "compare": {"type": "none|mom|yoy"},  // mom=环比, yoy=同比\n'
        '  "order_by": "<输出列 asc|desc?>",\n'
        '  "limit": 20,\n'
        '  "intent": "取数|归因|预测"\n'
        "}\n"
        f"区间词可用: {_range_hint()}\n"
        "硬性约束: 不要写 SQL、不要自己算同环比、不要出现 tenant_id/dept_id、"
        "不确定的维度宁可留空(系统会向用户澄清)也不要猜。"
    )
    if catalog is not None and getattr(catalog, "metrics", None):
        hint += "\n\n当前可用指标(id | 名称 | 单位 | 口径):\n" + catalog.render_metrics()
        hint += "\n\n当前可用维度(id | 名称 | 示例取值):\n" + catalog.render_dimensions()
    return hint


def _range_hint() -> str:
    from app.semantic import timesem

    return ", ".join(sorted(timesem.RANGE_TOKENS))
