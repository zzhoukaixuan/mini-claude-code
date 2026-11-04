"""Cron 触发事件模块：保存调度器已经登记、等待 Worker 消费的不可变任务快照。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from ..config import (
    CRON_ID_PATTERN,
    CRON_SLOT_PATTERN,
    MAX_CRON_LAST_FIRED_SLOT_LENGTH,
    MAX_CRON_PROMPT_LENGTH,
    VALID_CRON_TIMEZONES,
    CronTimezone,
)


def _validate_scheduled_slot(slot: str) -> None:
    """校验触发槽是带 UTC 偏移且精确到分钟的规范 ISO 时间。"""
    if not isinstance(slot, str):
        raise ValueError("CronTrigger scheduled_slot 必须是字符串")
    if len(slot) > MAX_CRON_LAST_FIRED_SLOT_LENGTH:
        raise ValueError(
            "CronTrigger scheduled_slot 不能超过 "
            f"{MAX_CRON_LAST_FIRED_SLOT_LENGTH} 个字符"
        )
    if CRON_SLOT_PATTERN.fullmatch(slot) is None:
        raise ValueError(
            "CronTrigger scheduled_slot 必须是带 UTC 偏移且精确到分钟的 ISO 时间"
        )
    try:
        parsed_slot = datetime.fromisoformat(slot)
    except ValueError as error:
        raise ValueError(
            f"CronTrigger scheduled_slot 不是合法时间：{error}"
        ) from error
    if parsed_slot.utcoffset() is None:
        raise ValueError("CronTrigger scheduled_slot 必须包含 UTC 偏移")
    if parsed_slot.second != 0 or parsed_slot.microsecond != 0:
        raise ValueError("CronTrigger scheduled_slot 只能精确到分钟")
    if parsed_slot.isoformat(timespec="minutes") != slot:
        raise ValueError("CronTrigger scheduled_slot 必须使用规范化 ISO 分钟格式")


@dataclass(frozen=True)
class CronTrigger:
    """一次已经到期的执行请求；它是 CronJob 的快照，而不是执行成功结果。"""

    cron_job_id: str  # 产生本次触发的 CronJob ID
    prompt: str  # 登记触发时冻结的 Agent 用户请求
    scheduled_slot: str  # 本次命中的带偏移 ISO 分钟槽
    timezone: CronTimezone  # 解释该分钟槽时使用的 CronJob 时区
    enqueued_at: float  # 调度器登记本次触发时的 Unix 时间戳

    def __post_init__(self) -> None:
        """保护事件边界，避免队列接收无法追溯来源或无法执行的触发。"""
        if (
            not isinstance(self.cron_job_id, str)
            or CRON_ID_PATTERN.fullmatch(self.cron_job_id) is None
        ):
            raise ValueError(
                "CronTrigger cron_job_id 必须符合 "
                "cron_<32 位小写十六进制> 格式"
            )

        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("CronTrigger prompt 必须是非空字符串")
        if self.prompt != self.prompt.strip():
            raise ValueError("CronTrigger prompt 不能包含首尾空白")
        if len(self.prompt) > MAX_CRON_PROMPT_LENGTH:
            raise ValueError(
                f"CronTrigger prompt 不能超过 {MAX_CRON_PROMPT_LENGTH} 个字符"
            )

        _validate_scheduled_slot(self.scheduled_slot)

        if self.timezone not in VALID_CRON_TIMEZONES:
            raise ValueError(
                f"CronTrigger timezone {self.timezone!r} 无效；"
                f"允许值为 {VALID_CRON_TIMEZONES}"
            )

        if (
            isinstance(self.enqueued_at, bool)
            or not isinstance(self.enqueued_at, (int, float))
            or not math.isfinite(self.enqueued_at)
            or self.enqueued_at < 0
        ):
            raise ValueError("CronTrigger enqueued_at 必须是有限的非负 Unix 时间戳")
        object.__setattr__(self, "enqueued_at", float(self.enqueued_at))
