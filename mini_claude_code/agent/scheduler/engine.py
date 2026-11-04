"""Cron 单步调度核心：基于注入时间计算新注册表与待发布触发，不执行任何副作用。"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..config import MAX_CRON_JOBS, VALID_CRON_TIMEZONES, CronTimezone
from .events import CronTrigger
from .expression import cron_matches, parse_cron_expression
from .models import CronJob


def _require_aware_moment(moment: datetime) -> None:
    """拒绝缺少 UTC 偏移的时间，防止宿主机默认时区悄悄改变调度语义。"""
    if not isinstance(moment, datetime):
        raise TypeError("Cron 调度时刻必须是 datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Cron 调度时刻必须包含时区信息")


def _moment_for_cron_timezone(
    moment: datetime,
    cron_timezone: CronTimezone,
) -> datetime:
    """将同一绝对时刻转换为 CronJob 声明的 UTC 或系统本地时间。"""
    if cron_timezone not in VALID_CRON_TIMEZONES:
        raise ValueError(
            f"Cron 时区 {cron_timezone!r} 无效；允许值为 {VALID_CRON_TIMEZONES}"
        )
    if cron_timezone == "UTC":
        return moment.astimezone(timezone.utc)
    return moment.astimezone()


def _cron_slot(moment: datetime) -> tuple[str, datetime]:
    """把目标时区中的时刻规范化为可持久化、可按绝对时间比较的分钟槽。"""
    normalized_moment = moment.replace(second=0, microsecond=0)
    return normalized_moment.isoformat(timespec="minutes"), normalized_moment


def _slot_has_already_fired(
    candidate_slot: datetime,
    last_fired_slot: str | None,
) -> bool:
    """判断候选槽是否早于或等于已登记槽，兼顾时钟回拨与 DST 偏移。"""
    if last_fired_slot is None:
        return False
    previous_slot = datetime.fromisoformat(last_fired_slot)
    if previous_slot.utcoffset() is None:
        raise ValueError("CronJob last_fired_slot 必须包含 UTC 偏移")
    return candidate_slot.astimezone(timezone.utc) <= previous_slot.astimezone(
        timezone.utc
    )


@dataclass(frozen=True)
class CronTickPlan:
    """一次纯调度计算结果；调用方稍后负责持久化 jobs 并发布 triggers。"""

    jobs: tuple[CronJob, ...]  # 本轮登记完成后仍然活动的 CronJob 快照
    triggers: tuple[CronTrigger, ...]  # 本轮新增且等待发布的触发事件

    def __post_init__(self) -> None:
        """保证计划容器不可变、ID 唯一，并维持确定性的 ID 顺序。"""
        if not isinstance(self.jobs, tuple):
            raise ValueError("CronTickPlan jobs 必须是 CronJob 元组")
        if not isinstance(self.triggers, tuple):
            raise ValueError("CronTickPlan triggers 必须是 CronTrigger 元组")
        if any(not isinstance(job, CronJob) for job in self.jobs):
            raise ValueError("CronTickPlan jobs 只能包含 CronJob")
        if any(not isinstance(trigger, CronTrigger) for trigger in self.triggers):
            raise ValueError("CronTickPlan triggers 只能包含 CronTrigger")

        job_ids = tuple(job.id for job in self.jobs)
        trigger_ids = tuple(trigger.cron_job_id for trigger in self.triggers)
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("CronTickPlan jobs 不能包含重复 CronJob ID")
        if len(set(trigger_ids)) != len(trigger_ids):
            raise ValueError("CronTickPlan triggers 不能包含重复 CronJob ID")
        if job_ids != tuple(sorted(job_ids)):
            raise ValueError("CronTickPlan jobs 必须按 CronJob ID 排序")
        if trigger_ids != tuple(sorted(trigger_ids)):
            raise ValueError("CronTickPlan triggers 必须按 CronJob ID 排序")


def _validated_jobs_snapshot(jobs: dict[str, CronJob]) -> dict[str, CronJob]:
    """复制并校验调用方注册表，让单步计算不会修改原始字典。"""
    if not isinstance(jobs, dict):
        raise TypeError("Cron 调度注册表必须是字典")
    if len(jobs) > MAX_CRON_JOBS:
        raise ValueError(
            f"Cron 调度注册表包含 {len(jobs)} 条任务，"
            f"超过上限 {MAX_CRON_JOBS}"
        )

    snapshot: dict[str, CronJob] = {}
    for job_id, job in jobs.items():
        if not isinstance(job_id, str):
            raise ValueError("Cron 调度注册表的键必须是字符串 ID")
        if not isinstance(job, CronJob):
            raise ValueError(f"Cron 调度注册表 {job_id!r} 的值不是 CronJob")
        if job_id != job.id:
            raise ValueError(
                f"Cron 调度注册表键 {job_id!r} 与记录 ID {job.id!r} 不一致"
            )
        snapshot[job_id] = job
    return dict(sorted(snapshot.items()))


def plan_cron_tick(
    jobs: dict[str, CronJob],
    moment: datetime,
) -> CronTickPlan:
    """计算当前分钟到期的 Job；不改输入、不写磁盘，也不直接调用 Agent。"""
    _require_aware_moment(moment)
    try:
        enqueued_at = moment.timestamp()
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError(f"Cron 调度时刻无法转换为 Unix 时间戳：{error}") from error
    if not math.isfinite(enqueued_at) or enqueued_at < 0:
        raise ValueError("Cron 调度时刻必须能转换为有限的非负 Unix 时间戳")

    working_jobs = _validated_jobs_snapshot(jobs)
    triggers: list[CronTrigger] = []
    for job_id, job in tuple(working_jobs.items()):
        local_moment = _moment_for_cron_timezone(moment, job.timezone)
        expression = parse_cron_expression(job.cron)
        if not cron_matches(expression, local_moment):
            continue

        scheduled_slot, comparable_slot = _cron_slot(local_moment)
        if _slot_has_already_fired(comparable_slot, job.last_fired_slot):
            continue

        triggers.append(
            CronTrigger(
                cron_job_id=job.id,
                prompt=job.prompt,
                scheduled_slot=scheduled_slot,
                timezone=job.timezone,
                enqueued_at=enqueued_at,
            )
        )
        if job.recurring:
            working_jobs[job_id] = replace(
                job,
                last_fired_slot=scheduled_slot,
            )
        else:
            del working_jobs[job_id]

    return CronTickPlan(
        jobs=tuple(working_jobs.values()),
        triggers=tuple(triggers),
    )
