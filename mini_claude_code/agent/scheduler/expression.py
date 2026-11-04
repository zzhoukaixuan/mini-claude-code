"""Cron 表达式模块：严格解析五段式计划，并匹配已经转换到目标时区的时间。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ..config import MAX_CRON_EXPRESSION_LENGTH


_CRON_INTEGER_PATTERN = re.compile(r"[0-9]+")  # 只接受 ASCII 十进制非负整数
_CRON_FIELD_SPECS: tuple[tuple[str, int, int], ...] = (  # 五段式 Cron 字段及闭区间
    ("分钟", 0, 59),
    ("小时", 0, 23),
    ("日期", 1, 31),
    ("月份", 1, 12),
    ("星期", 0, 6),
)


def _full_value_set(lower_bound: int, upper_bound: int) -> frozenset[int]:
    """返回某个 Cron 字段完整且不可变的合法值集合。"""
    return frozenset(range(lower_bound, upper_bound + 1))


def _parse_ascii_integer(text: str, *, field_name: str) -> int:
    """解析不带符号的 ASCII 整数，避免 Unicode 数字和隐式类型转换。"""
    if _CRON_INTEGER_PATTERN.fullmatch(text) is None:
        raise ValueError(f"Cron {field_name}包含非法整数：{text!r}")
    return int(text)


def _require_in_bounds(
    value: int,
    *,
    field_name: str,
    lower_bound: int,
    upper_bound: int,
) -> None:
    """保证单个 Cron 字段值位于该字段允许的闭区间。"""
    if not lower_bound <= value <= upper_bound:
        raise ValueError(
            f"Cron {field_name}值 {value} 超出范围 "
            f"[{lower_bound}, {upper_bound}]"
        )


def _parse_cron_component(
    component: str,
    *,
    field_name: str,
    lower_bound: int,
    upper_bound: int,
) -> frozenset[int]:
    """解析列表中的一个通配符、步长、范围或单值组件。"""
    if component == "*":
        return _full_value_set(lower_bound, upper_bound)

    if component.startswith("*/"):
        step_text = component[2:]
        step = _parse_ascii_integer(step_text, field_name=field_name)
        field_size = upper_bound - lower_bound + 1
        if not 1 <= step <= field_size:
            raise ValueError(
                f"Cron {field_name}步长 {step} 必须位于 [1, {field_size}]"
            )
        return frozenset(range(lower_bound, upper_bound + 1, step))

    if "/" in component:
        raise ValueError(
            f"Cron {field_name}暂不支持这种步长语法：{component!r}；"
            "只支持 */N"
        )

    if "-" in component:
        range_parts = component.split("-")
        if len(range_parts) != 2 or not all(range_parts):
            raise ValueError(f"Cron {field_name}范围格式无效：{component!r}")
        start = _parse_ascii_integer(range_parts[0], field_name=field_name)
        end = _parse_ascii_integer(range_parts[1], field_name=field_name)
        _require_in_bounds(
            start,
            field_name=field_name,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )
        _require_in_bounds(
            end,
            field_name=field_name,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )
        if start > end:
            raise ValueError(
                f"Cron {field_name}范围起点不能大于终点：{component!r}"
            )
        return frozenset(range(start, end + 1))

    value = _parse_ascii_integer(component, field_name=field_name)
    _require_in_bounds(
        value,
        field_name=field_name,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
    )
    return frozenset({value})


def _parse_cron_field(
    field: str,
    *,
    field_name: str,
    lower_bound: int,
    upper_bound: int,
) -> frozenset[int]:
    """解析一个字段；逗号列表可以组合单值和范围，但不能组合通配符。"""
    if not field:
        raise ValueError(f"Cron {field_name}字段不能为空")

    components = field.split(",")
    if any(not component for component in components):
        raise ValueError(f"Cron {field_name}列表不能包含空项：{field!r}")
    if len(components) > 1 and any("*" in component for component in components):
        raise ValueError(
            f"Cron {field_name}通配符或步长不能与列表项组合：{field!r}"
        )

    values: set[int] = set()
    for component in components:
        values.update(
            _parse_cron_component(
                component,
                field_name=field_name,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
            )
        )
    if not values:
        raise ValueError(f"Cron {field_name}没有产生任何可匹配值")
    return frozenset(values)


def _validate_value_set(
    values: frozenset[int],
    *,
    field_name: str,
    lower_bound: int,
    upper_bound: int,
) -> None:
    """保护手动构造 CronExpression 时的不可变集合与值域不变量。"""
    if not isinstance(values, frozenset) or not values:
        raise ValueError(f"CronExpression.{field_name} 必须是非空 frozenset")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"CronExpression.{field_name} 只能包含整数"
            )
        if not lower_bound <= value <= upper_bound:
            raise ValueError(
                f"CronExpression.{field_name} 包含越界值 {value}；"
                f"允许范围为 [{lower_bound}, {upper_bound}]"
            )


@dataclass(frozen=True)
class CronExpression:
    """一条已经通过语法和值域校验的五段式 Cron 表达式。"""

    source: str  # 规范化后的原始五段式表达式
    minutes: frozenset[int]  # 允许触发的分钟，范围 0～59
    hours: frozenset[int]  # 允许触发的小时，范围 0～23
    days_of_month: frozenset[int]  # 允许触发的日期，范围 1～31
    months: frozenset[int]  # 允许触发的月份，范围 1～12
    days_of_week: frozenset[int]  # 允许触发的星期，范围 0～6，0 表示星期日
    day_of_month_wildcard: bool  # 日期字段是否为字面量 *
    day_of_week_wildcard: bool  # 星期字段是否为字面量 *

    def __post_init__(self) -> None:
        """保护手动构造入口，确保 Matcher 永远面对完整的合法值集合。"""
        if not isinstance(self.source, str) or len(self.source.split()) != 5:
            raise ValueError("CronExpression.source 必须是五段式非空字符串")
        if len(self.source) > MAX_CRON_EXPRESSION_LENGTH:
            raise ValueError(
                "CronExpression.source 不能超过 "
                f"{MAX_CRON_EXPRESSION_LENGTH} 个字符"
            )
        if self.source != " ".join(self.source.split()):
            raise ValueError("CronExpression.source 必须使用单个空格规范化")

        value_sets = (
            (self.minutes, "minutes", 0, 59),
            (self.hours, "hours", 0, 23),
            (self.days_of_month, "days_of_month", 1, 31),
            (self.months, "months", 1, 12),
            (self.days_of_week, "days_of_week", 0, 6),
        )
        for values, field_name, lower_bound, upper_bound in value_sets:
            _validate_value_set(
                values,
                field_name=field_name,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
            )

        if type(self.day_of_month_wildcard) is not bool:
            raise ValueError("day_of_month_wildcard 必须是布尔值")
        if type(self.day_of_week_wildcard) is not bool:
            raise ValueError("day_of_week_wildcard 必须是布尔值")
        if self.day_of_month_wildcard and self.days_of_month != _full_value_set(1, 31):
            raise ValueError("日期字段为通配符时必须包含全部合法日期")
        if self.day_of_week_wildcard and self.days_of_week != _full_value_set(0, 6):
            raise ValueError("星期字段为通配符时必须包含全部合法星期")


def parse_cron_expression(text: str) -> CronExpression:
    """将五段式 Cron 文本解析为不可变表达式；非法输入直接抛出 ValueError。"""
    if not isinstance(text, str):
        raise TypeError("Cron 表达式必须是字符串")
    if len(text) > MAX_CRON_EXPRESSION_LENGTH:
        raise ValueError(
            f"Cron 表达式不能超过 {MAX_CRON_EXPRESSION_LENGTH} 个字符"
        )
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("Cron 表达式不能为空")
    fields = normalized.split(" ")
    if len(fields) != len(_CRON_FIELD_SPECS):
        raise ValueError(
            "Cron 表达式必须包含五个字段：分钟 小时 日期 月份 星期；"
            f"当前得到 {len(fields)} 个字段"
        )

    parsed_fields = tuple(
        _parse_cron_field(
            field,
            field_name=field_name,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )
        for field, (field_name, lower_bound, upper_bound) in zip(
            fields,
            _CRON_FIELD_SPECS,
            strict=True,
        )
    )
    return CronExpression(
        source=normalized,
        minutes=parsed_fields[0],
        hours=parsed_fields[1],
        days_of_month=parsed_fields[2],
        months=parsed_fields[3],
        days_of_week=parsed_fields[4],
        day_of_month_wildcard=fields[2] == "*",
        day_of_week_wildcard=fields[4] == "*",
    )


def cron_matches(expression: CronExpression, moment: datetime) -> bool:
    """判断目标时区中的一个时间点是否命中已解析的 Cron 表达式。"""
    if not isinstance(expression, CronExpression):
        raise TypeError("cron_matches expression 必须是 CronExpression")
    if not isinstance(moment, datetime):
        raise TypeError("cron_matches moment 必须是 datetime")

    minute_matches = moment.minute in expression.minutes
    hour_matches = moment.hour in expression.hours
    month_matches = moment.month in expression.months
    if not (minute_matches and hour_matches and month_matches):
        return False

    day_of_month_matches = moment.day in expression.days_of_month
    cron_day_of_week = (moment.weekday() + 1) % 7
    day_of_week_matches = cron_day_of_week in expression.days_of_week

    if expression.day_of_month_wildcard and expression.day_of_week_wildcard:
        return True
    if expression.day_of_month_wildcard:
        return day_of_week_matches
    if expression.day_of_week_wildcard:
        return day_of_month_matches
    return day_of_month_matches or day_of_week_matches
