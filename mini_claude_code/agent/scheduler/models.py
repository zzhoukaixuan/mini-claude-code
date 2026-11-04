"""CronJob 领域模型：定义可审计计划字段及严格 JSON 记录边界。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from ..config import (
    CRON_ID_PATTERN,
    CRON_SLOT_PATTERN,
    MAX_CRON_EXPRESSION_LENGTH,
    MAX_CRON_LAST_FIRED_SLOT_LENGTH,
    MAX_CRON_PROMPT_LENGTH,
    VALID_CRON_MISFIRE_POLICIES,
    VALID_CRON_TIMEZONES,
    CronMisfirePolicy,
    CronTimezone,
)
from .expression import parse_cron_expression


@dataclass(frozen=True)
class CronJob:
    """一条活动的定时计划定义；实际执行结果由独立 Job/Agent 运行时负责。"""

    id: str  # Harness 生成且可安全持久化的 cron_<UUID4 hex> 标识
    cron: str  # 已规范化并通过五段式语法校验的 Cron 表达式
    prompt: str  # 到期后作为新用户请求交给 Agent 的任务正文
    recurring: bool  # True 表示周期计划；False 表示首次触发后删除
    durable: bool  # True 写入磁盘；False 只存在于当前 CLI 进程
    timezone: CronTimezone  # 使用系统本地时间或 UTC 解释 Cron 字段
    misfire_policy: CronMisfirePolicy  # 停机期间错过的时间是否补跑
    created_at: float  # 使用 Unix 时间戳记录的创建时间，供审计使用
    last_fired_slot: str | None = None  # 最近成功登记的带偏移分钟槽，用于触发去重

    def __post_init__(self) -> None:
        """校验字段边界，并保证持久计划始终携带可解析的执行语义。"""
        if not isinstance(self.id, str) or CRON_ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError("CronJob ID 必须符合 cron_<32 位小写十六进制> 格式")

        if not isinstance(self.cron, str) or not self.cron:
            raise ValueError("CronJob cron 必须是非空字符串")
        if len(self.cron) > MAX_CRON_EXPRESSION_LENGTH:
            raise ValueError(
                f"CronJob cron 不能超过 {MAX_CRON_EXPRESSION_LENGTH} 个字符"
            )
        parsed_expression = parse_cron_expression(self.cron)
        if parsed_expression.source != self.cron:
            raise ValueError("CronJob cron 必须使用单个空格保存规范化的五段式表达式")

        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("CronJob prompt 必须是非空字符串")
        if self.prompt != self.prompt.strip():
            raise ValueError("CronJob prompt 不能包含首尾空白")
        if len(self.prompt) > MAX_CRON_PROMPT_LENGTH:
            raise ValueError(
                f"CronJob prompt 不能超过 {MAX_CRON_PROMPT_LENGTH} 个字符"
            )

        if type(self.recurring) is not bool:
            raise ValueError("CronJob recurring 必须是布尔值")
        if type(self.durable) is not bool:
            raise ValueError("CronJob durable 必须是布尔值")
        if self.timezone not in VALID_CRON_TIMEZONES:
            raise ValueError(
                f"CronJob timezone {self.timezone!r} 无效；"
                f"允许值为 {VALID_CRON_TIMEZONES}"
            )
        if self.misfire_policy not in VALID_CRON_MISFIRE_POLICIES:
            raise ValueError(
                f"CronJob misfire_policy {self.misfire_policy!r} 无效；"
                f"允许值为 {VALID_CRON_MISFIRE_POLICIES}"
            )

        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(self.created_at)
            or self.created_at < 0
        ):
            raise ValueError("CronJob created_at 必须是有限的非负 Unix 时间戳")
        object.__setattr__(self, "created_at", float(self.created_at))

        if self.last_fired_slot is not None:
            if not isinstance(self.last_fired_slot, str):
                raise ValueError("CronJob last_fired_slot 必须是字符串或 None")
            if len(self.last_fired_slot) > MAX_CRON_LAST_FIRED_SLOT_LENGTH:
                raise ValueError(
                    "CronJob last_fired_slot 不能超过 "
                    f"{MAX_CRON_LAST_FIRED_SLOT_LENGTH} 个字符"
                )
            if CRON_SLOT_PATTERN.fullmatch(self.last_fired_slot) is None:
                raise ValueError(
                    "CronJob last_fired_slot 必须是带 UTC 偏移且精确到分钟的 ISO 时间"
                )
            try:
                fired_slot = datetime.fromisoformat(self.last_fired_slot)
            except ValueError as error:
                raise ValueError(
                    f"CronJob last_fired_slot 不是合法时间：{error}"
                ) from error
            if fired_slot.utcoffset() is None:
                raise ValueError("CronJob last_fired_slot 必须包含 UTC 偏移")
            if fired_slot.second != 0 or fired_slot.microsecond != 0:
                raise ValueError("CronJob last_fired_slot 只能精确到分钟")
            if fired_slot.isoformat(timespec="minutes") != self.last_fired_slot:
                raise ValueError("CronJob last_fired_slot 必须使用规范化 ISO 分钟格式")
            if not self.recurring:
                raise ValueError("一次性 CronJob 触发后必须删除，不能保留 last_fired_slot")


def cron_job_to_record(job: CronJob) -> dict[str, object]:
    """将 CronJob 转换为字段名稳定的 JSON 兼容记录。"""
    if not isinstance(job, CronJob):
        raise TypeError("只能序列化 CronJob 实例")
    return {
        "id": job.id,
        "cron": job.cron,
        "prompt": job.prompt,
        "recurring": job.recurring,
        "durable": job.durable,
        "timezone": job.timezone,
        "misfirePolicy": job.misfire_policy,
        "createdAt": job.created_at,
        "lastFiredSlot": job.last_fired_slot,
    }


def cron_job_from_record(record: object) -> CronJob:
    """严格解析一条 CronJob JSON 记录，拒绝未知、缺失或错误类型的字段。"""
    if not isinstance(record, dict):
        raise ValueError("CronJob 记录必须是 JSON 对象")

    expected_fields = {
        "id",
        "cron",
        "prompt",
        "recurring",
        "durable",
        "timezone",
        "misfirePolicy",
        "createdAt",
        "lastFiredSlot",
    }
    actual_fields = set(record)
    missing_fields = expected_fields - actual_fields
    unknown_fields = actual_fields - expected_fields
    if missing_fields:
        formatted_fields = ", ".join(sorted(missing_fields))
        raise ValueError(f"CronJob 记录缺少字段：{formatted_fields}")
    if unknown_fields:
        formatted_fields = ", ".join(
            sorted(repr(field) for field in unknown_fields)
        )
        raise ValueError(f"CronJob 记录包含未知字段：{formatted_fields}")

    job_id = record["id"]
    cron = record["cron"]
    prompt = record["prompt"]
    recurring = record["recurring"]
    durable = record["durable"]
    timezone = record["timezone"]
    misfire_policy = record["misfirePolicy"]
    created_at = record["createdAt"]
    last_fired_slot = record["lastFiredSlot"]

    if not isinstance(job_id, str):
        raise ValueError("CronJob 记录的 id 必须是字符串")
    if not isinstance(cron, str):
        raise ValueError("CronJob 记录的 cron 必须是字符串")
    if not isinstance(prompt, str):
        raise ValueError("CronJob 记录的 prompt 必须是字符串")
    if type(recurring) is not bool:
        raise ValueError("CronJob 记录的 recurring 必须是布尔值")
    if type(durable) is not bool:
        raise ValueError("CronJob 记录的 durable 必须是布尔值")
    if not isinstance(timezone, str):
        raise ValueError("CronJob 记录的 timezone 必须是字符串")
    if not isinstance(misfire_policy, str):
        raise ValueError("CronJob 记录的 misfirePolicy 必须是字符串")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
    ):
        raise ValueError("CronJob 记录的 createdAt 必须是数字")
    if last_fired_slot is not None and not isinstance(last_fired_slot, str):
        raise ValueError("CronJob 记录的 lastFiredSlot 必须是字符串或 null")

    return CronJob(
        id=job_id,
        cron=cron,
        prompt=prompt,
        recurring=recurring,
        durable=durable,
        timezone=cast(CronTimezone, timezone),
        misfire_policy=cast(CronMisfirePolicy, misfire_policy),
        created_at=float(created_at),
        last_fired_slot=last_fired_slot,
    )
