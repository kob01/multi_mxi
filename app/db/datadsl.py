"""层 2: 模型交上来的写意图 DSL(强类型 JSON, 不是 SQL)。

这是整个改造里"最有效的架构改动": 模型不再写 INSERT/UPDATE/DELETE 文本, 只填一个
结构化的意图。于是它失去三样自由度 ——

1. **失去值拼接的自由**: 每个 ``value`` 最终都是绑定参数, 经典 SQL 注入在语法层不成立;
2. **失去标识符自由**: ``entity`` 查实体白名单, ``field`` 查该实体的可过滤/可写字段白名单,
   名单外一律拒, 列名不可能来自模型嘴里;
3. **失去作用域自由**: ``tenant_id``/``dept_id`` 连出现在名单里都没有, 由服务端注入。

顺带失去的还有"写出第二条语句/DDL/危险函数"这类面 —— 那些面根本没有对应的字段。

:func:`has_write_intent` 是层 5-C 的"计划偏移检测": 用**服务端词表**核对用户原句是否
真的要改数据。它是概率性降噪, 不是防线; 防线是白名单 + AST + 审批梯度。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.policy import SCOPE_COLUMNS, PolicyError, spec_for

# 形状上限: 一条受控写不该有 30 个条件; 大数只会让"绕过审批的巨型 IN"成为可能。
MAX_FILTERS = 8
MAX_SET_COLUMNS = 12
MAX_IN_VALUES = 50
MAX_VALUE_CHARS = 500
MAX_REASON_CHARS = 200


class DslError(ValueError):
    """DSL 结构/字段/值不合法(拒因必须是确定的, 不带猜测)。"""


class FilterOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    IS_NULL = "is_null"


# 谓词形状模板(列名来自白名单, 值一律走占位符)。
_OP_TEMPLATES: dict[FilterOp, str] = {
    FilterOp.EQ: "{field} = {ph}",
    FilterOp.NE: "{field} <> {ph}",
    FilterOp.LT: "{field} < {ph}",
    FilterOp.LTE: "{field} <= {ph}",
    FilterOp.GT: "{field} > {ph}",
    FilterOp.GTE: "{field} >= {ph}",
    # IN 展开成多个占位符而不是把值拼进文本: 值数量可变, 但每个值仍是绑定参数。
    FilterOp.IN: "{field} IN ({ph})",
    FilterOp.IS_NULL: "{field} IS NULL",
}

# 写意图触发词(服务端词表, 不是模型判断)。刻意保守: 漏判的代价是"多问一轮",
# 误判的代价是"改了不该改的数据", 所以宁可漏。
_WRITE_INTENT_TERMS = (
    "删除", "删掉", "清理", "停用", "作废", "撤销", "取消", "恢复",
    "更新", "改成", "改为", "调整为", "标记为", "置为", "设为", "修正",
    "退回", "驳回", "归档",
    "delete", "update", "set ", "mark as",
)


def has_write_intent(text: str) -> bool:
    """用户原句里是否出现写意图触发词。"""
    lowered = (text or "").lower()
    return any(term in lowered for term in _WRITE_INTENT_TERMS)


def _check_scalar(value: Any, *, where: str) -> None:
    """值只允许标量, 且长度受限。

    注意这里**不**检查"像不像 SQL": 值永远走绑定参数, 里面出现 ``;`` 或 ``--`` 都不会
    改变语句结构; 真要拒的只是嵌套结构(它意味着模型在试图表达别的东西)。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str):
            if "\x00" in value:
                raise DslError(f"{where} 含空字符, 已拒")
            if len(value) > MAX_VALUE_CHARS:
                raise DslError(f"{where} 超长(>{MAX_VALUE_CHARS} 字符)")
        return
    raise DslError(f"{where} 只允许标量值(字符串/数字/布尔/None), 收到 {type(value).__name__}")


class Filter(BaseModel):
    """一个业务条件(不含任何作用域列)。"""

    field: str
    op: FilterOp = FilterOp.EQ
    value: Any = None

    @field_validator("field")
    @classmethod
    def _field_ident(cls, v: str) -> str:
        name = (v or "").strip()
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            raise DslError(f"非法字段名: {v!r}")
        if name in SCOPE_COLUMNS:
            raise DslError(
                f"{name} 是服务端注入的作用域列, 不许出现在过滤条件里"
            )
        return name

    @model_validator(mode="after")
    def _value_shape(self) -> "Filter":
        if self.op is FilterOp.IS_NULL:
            return self
        if self.op is FilterOp.IN:
            if not isinstance(self.value, (list, tuple)) or not self.value:
                raise DslError("op=in 需要非空的值列表")
            if len(self.value) > MAX_IN_VALUES:
                raise DslError(f"op=in 的值最多 {MAX_IN_VALUES} 个")
            for i, item in enumerate(self.value):
                _check_scalar(item, where=f"filters[{self.field}][{i}]")
            return self
        _check_scalar(self.value, where=f"filters[{self.field}]")
        if self.value is None:
            raise DslError(f"filters[{self.field}] 的值不能为空(要判空请用 op=is_null)")
        return self


class DataOpPlan(BaseModel):
    """一次写意图: 对某实体按条件批量变更(或软删除)。"""

    action: str
    entity: str
    filters: list[Filter] = Field(min_length=1, max_length=MAX_FILTERS)
    sets: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""

    @field_validator("action")
    @classmethod
    def _action_known(cls, v: str) -> str:
        action = (v or "").strip().lower()
        if action not in ("update", "delete"):
            # insert 刻意不在这里: 建单是"办理"域的职责(HR/财务/采购 MCP 的结构化工具),
            # 分析智能体不产生新单据。将来要放, 得先给实体登记 insertable 与必填列。
            raise DslError(f"action 只允许 update/delete(新增单据走各业务域工具), 收到 {v!r}")
        return action

    @field_validator("reason")
    @classmethod
    def _reason_len(cls, v: str) -> str:
        if len(v or "") > MAX_REASON_CHARS:
            return (v or "")[:MAX_REASON_CHARS]
        return v or ""

    @model_validator(mode="after")
    def _against_whitelist(self) -> "DataOpPlan":
        spec = spec_for(self.entity)  # 名单外/高危表 -> PolicyError
        for f in self.filters:
            if f.field not in spec.filter_fields:
                raise DslError(
                    f"字段 {f.field} 不在 {spec.entity} 的可过滤白名单内"
                    f"(可用: {sorted(spec.filter_fields)})"
                )
        if self.action == "update":
            if not self.sets:
                raise DslError("update 必须给出 sets(要停用请用 action=delete)")
            if len(self.sets) > MAX_SET_COLUMNS:
                raise DslError(f"sets 最多 {MAX_SET_COLUMNS} 列")
            for key, value in self.sets.items():
                if key in SCOPE_COLUMNS:
                    raise DslError(f"{key} 由服务端维护, 不许写")
                if key not in spec.writable_fields:
                    raise DslError(
                        f"字段 {key} 不在 {spec.entity} 的可写字段白名单内"
                        f"(可写: {sorted(spec.writable_fields)})"
                    )
                _check_scalar(value, where=f"sets[{key}]")
        elif self.action == "delete":
            if not spec.soft_delete:
                raise DslError(f"{spec.entity} 不允许删除")
            if self.sets:
                raise DslError("delete 不接受 sets(停用请用 action=update 改 status)")
        return self


def parse_plan(payload: dict | str) -> DataOpPlan:
    """把模型给的 JSON(字符串或已解析的 dict)转成强类型计划。

    这里刻意不用 pydantic 的宽松转换: ``value="1"` 与 ``value=1`` 对不同列语义差别很大,
    交给模板与数据库定夺, DSL 层只保证"是个标量"。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DslError(f"写计划不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DslError(f"写计划必须是 JSON 对象, 收到 {type(payload).__name__}")
    try:
        return DataOpPlan.model_validate(payload)
    except PolicyError as exc:
        # 白名单拒绝单独透出(它不是"结构错", 而是"越界")。
        raise DslError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - pydantic 的校验错要转成统一拒因
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


def predicate_sql(filt: Filter, placeholder: str) -> tuple[str, list[str], list[Any]]:
    """把一个条件渲染成 ``(SQL 片段, 占位符名列表, 绑定值列表)``。

    返回而不是就地拼接: 调用方要按顺序给占位符编号, 也让 dry-run 复用同一份形状。
    """
    template = _OP_TEMPLATES[filt.op]
    if filt.op is FilterOp.IS_NULL:
        return template.format(field=filt.field, ph=""), [], []
    if filt.op is FilterOp.IN:
        values = list(filt.value)
        names = [f"{placeholder}_{i}" for i in range(len(values))]
        ph = ", ".join(f":{name}" for name in names)
        return (
            template.format(field=filt.field, ph=ph),
            names,
            values,
        )
    return (
        template.format(field=filt.field, ph=f":{placeholder}"),
        [placeholder],
        [filt.value],
    )
