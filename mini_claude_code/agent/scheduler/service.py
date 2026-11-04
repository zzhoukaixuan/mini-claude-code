"""Cron 调度服务：事务化组合持久注册表、纯 Tick 计算与内存触发队列。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock

from ..config import (
    CRON_ID_PATTERN,
    CRON_STORE_PATH,
    MAX_CRON_ID_GENERATION_ATTEMPTS,
    MAX_CRON_JOBS,
    CronTimezone,
)
from .engine import plan_cron_tick
from .events import CronTrigger
from .expression import parse_cron_expression
from .models import CronJob
from .queue import CronTriggerQueue
from .store import (
    cron_store_lock,
    generate_cron_id,
    load_durable_cron_jobs,
    resolve_cron_store_path,
    save_durable_cron_jobs,
)


def _validate_trigger_tuple(
    triggers: tuple[CronTrigger, ...],
    *,
    field_name: str,
) -> None:
    """校验 Tick 报告中的不可变触发序列。"""
    if not isinstance(triggers, tuple):
        raise ValueError(f"CronTickReport {field_name} 必须是 CronTrigger 元组")
    if any(not isinstance(trigger, CronTrigger) for trigger in triggers):
        raise ValueError(
            f"CronTickReport {field_name} 只能包含 CronTrigger"
        )
    trigger_ids = tuple(trigger.cron_job_id for trigger in triggers)
    if len(set(trigger_ids)) != len(trigger_ids):
        raise ValueError(
            f"CronTickReport {field_name} 不能包含重复 CronJob ID"
        )
    if trigger_ids != tuple(sorted(trigger_ids)):
        raise ValueError(
            f"CronTickReport {field_name} 必须按 CronJob ID 排序"
        )


@dataclass(frozen=True)
class CronTickReport:
    """一次调度事务的可审计结果，区分到期、入队与合并事件。"""

    due: tuple[CronTrigger, ...]  # 本轮首次登记为到期的全部触发
    enqueued: tuple[CronTrigger, ...]  # 成功进入内存队列的触发
    coalesced: tuple[CronTrigger, ...]  # 因同一 CronJob 已有待处理事件而合并的触发
    durable_changed: bool  # 本轮是否原子替换了 durable 注册表

    def __post_init__(self) -> None:
        """保证入队与合并结果恰好组成全部到期事件。"""
        _validate_trigger_tuple(self.due, field_name="due")
        _validate_trigger_tuple(self.enqueued, field_name="enqueued")
        _validate_trigger_tuple(self.coalesced, field_name="coalesced")
        if type(self.durable_changed) is not bool:
            raise ValueError("CronTickReport durable_changed 必须是布尔值")

        due_by_id = {trigger.cron_job_id: trigger for trigger in self.due}
        enqueued_ids = {trigger.cron_job_id for trigger in self.enqueued}
        coalesced_ids = {trigger.cron_job_id for trigger in self.coalesced}
        if enqueued_ids & coalesced_ids:
            raise ValueError("同一个 Cron 触发不能同时标记为入队和合并")
        if enqueued_ids | coalesced_ids != set(due_by_id):
            raise ValueError("入队与合并事件必须恰好覆盖全部到期事件")
        for trigger in self.enqueued + self.coalesced:
            if due_by_id.get(trigger.cron_job_id) != trigger:
                raise ValueError("入队或合并事件必须来自本轮到期事件快照")


def _merge_job_registries(
    durable_jobs: dict[str, CronJob],
    session_jobs: dict[str, CronJob],
) -> dict[str, CronJob]:
    """合并跨进程与当前进程注册表，拒绝来源或 ID 不一致的内部状态。"""
    for job in durable_jobs.values():
        if not job.durable:
            raise RuntimeError(
                f"durable Cron 注册表包含 session-only Job {job.id!r}"
            )
    for job in session_jobs.values():
        if job.durable:
            raise RuntimeError(
                f"session-only Cron 注册表包含 durable Job {job.id!r}"
            )

    duplicate_ids = set(durable_jobs) & set(session_jobs)
    if duplicate_ids:
        formatted_ids = ", ".join(sorted(duplicate_ids))
        raise RuntimeError(
            f"durable 与 session-only Cron 注册表存在重复 ID：{formatted_ids}"
        )

    merged_jobs = dict(durable_jobs)
    merged_jobs.update(session_jobs)
    return dict(sorted(merged_jobs.items()))


def _generate_available_cron_id(existing_ids: set[str]) -> str:
    """生成当前合并注册表中尚未使用的 ID，并为极端 UUID 碰撞设置上限。"""
    for _ in range(MAX_CRON_ID_GENERATION_ATTEMPTS):
        cron_job_id = generate_cron_id()
        if cron_job_id not in existing_ids:
            return cron_job_id
    raise RuntimeError(
        "多次生成 CronJob ID 均发生碰撞；本次计划未创建"
    )


def _require_cron_job_id(cron_job_id: str) -> None:
    """在查询注册表前拒绝格式无效的外部 CronJob ID。"""
    if (
        not isinstance(cron_job_id, str)
        or CRON_ID_PATTERN.fullmatch(cron_job_id) is None
    ):
        raise ValueError("CronJob ID 必须符合 cron_<32 位小写十六进制> 格式")


class CronScheduler:
    """持有 session-only 状态，并按调用方提供的时刻执行单次调度事务。"""

    def __init__(
        self,
        trigger_queue: CronTriggerQueue,
        *,
        store_path: Path = CRON_STORE_PATH,
    ) -> None:
        """组装依赖但不读取文件、不启动线程，也不自动执行 Tick。"""
        if not isinstance(trigger_queue, CronTriggerQueue):
            raise TypeError("CronScheduler trigger_queue 必须是 CronTriggerQueue")
        if not isinstance(store_path, Path):
            raise TypeError("CronScheduler store_path 必须是 pathlib.Path")

        self._trigger_queue = trigger_queue  # Scheduler 与后续 Worker 共享的触发队列
        self._store_path = resolve_cron_store_path(store_path)  # 已通过工作区边界校验的快照路径
        self._session_jobs: dict[str, CronJob] = {}  # 仅在当前 CLI 生命周期内存在的计划
        self._state_lock = Lock()  # 保护 session-only 注册表及单次 Tick 内存事务

    def schedule(
        self,
        cron: str,
        prompt: str,
        *,
        recurring: bool = True,
        durable: bool = True,
        timezone: CronTimezone = "local",
    ) -> CronJob:
        """创建一条计划；durable 快照保存成功后才提交创建结果。"""
        normalized_cron = parse_cron_expression(cron).source
        with self._state_lock:
            with cron_store_lock(self._store_path):
                durable_jobs = load_durable_cron_jobs(self._store_path)
                combined_jobs = _merge_job_registries(
                    durable_jobs,
                    self._session_jobs,
                )
                if len(combined_jobs) >= MAX_CRON_JOBS:
                    raise ValueError(
                        f"CronJob 数量已达到上限 {MAX_CRON_JOBS}"
                    )

                cron_job = CronJob(
                    id=_generate_available_cron_id(set(combined_jobs)),
                    cron=normalized_cron,
                    prompt=prompt,
                    recurring=recurring,
                    durable=durable,
                    timezone=timezone,
                    misfire_policy="skip",
                    created_at=time.time(),
                )
                if cron_job.durable:
                    durable_after = dict(durable_jobs)
                    durable_after[cron_job.id] = cron_job
                    save_durable_cron_jobs(durable_after, self._store_path)
                else:
                    session_after = dict(self._session_jobs)
                    session_after[cron_job.id] = cron_job
                    self._session_jobs = dict(sorted(session_after.items()))
                return cron_job

    def list_jobs(self) -> tuple[CronJob, ...]:
        """重新读取 durable 状态，并返回与 session-only 状态合并后的稳定快照。"""
        with self._state_lock:
            with cron_store_lock(self._store_path):
                durable_jobs = load_durable_cron_jobs(self._store_path)
                combined_jobs = _merge_job_registries(
                    durable_jobs,
                    self._session_jobs,
                )
                return tuple(combined_jobs.values())

    def cancel(self, cron_job_id: str) -> CronJob:
        """删除一条计划定义；已经进入触发队列的事件不会被追溯撤回。"""
        _require_cron_job_id(cron_job_id)
        with self._state_lock:
            with cron_store_lock(self._store_path):
                durable_jobs = load_durable_cron_jobs(self._store_path)
                combined_jobs = _merge_job_registries(
                    durable_jobs,
                    self._session_jobs,
                )
                cron_job = combined_jobs.get(cron_job_id)
                if cron_job is None:
                    raise FileNotFoundError(f"CronJob {cron_job_id!r} 不存在")

                if cron_job.durable:
                    durable_after = dict(durable_jobs)
                    del durable_after[cron_job.id]
                    save_durable_cron_jobs(durable_after, self._store_path)
                else:
                    session_after = dict(self._session_jobs)
                    del session_after[cron_job.id]
                    self._session_jobs = dict(sorted(session_after.items()))
                return cron_job

    def tick(self, moment: datetime) -> CronTickReport:
        """登记当前分钟到期的 Job；持久化成功后才向内存队列发布事件。"""
        with self._state_lock:
            with cron_store_lock(self._store_path):
                durable_before = load_durable_cron_jobs(self._store_path)
                combined_jobs = _merge_job_registries(
                    durable_before,
                    self._session_jobs,
                )
                plan = plan_cron_tick(combined_jobs, moment)
                planned_jobs = {job.id: job for job in plan.jobs}
                durable_after = {
                    job_id: job
                    for job_id, job in planned_jobs.items()
                    if job.durable
                }
                session_after = {
                    job_id: job
                    for job_id, job in planned_jobs.items()
                    if not job.durable
                }
                durable_changed = durable_after != durable_before
                if durable_changed:
                    save_durable_cron_jobs(
                        durable_after,
                        self._store_path,
                    )

                # 只有磁盘事务成功后，才提交本进程的 session-only 新状态。
                self._session_jobs = session_after

            enqueued: list[CronTrigger] = []
            coalesced: list[CronTrigger] = []
            for trigger in plan.triggers:
                if self._trigger_queue.enqueue(trigger):
                    enqueued.append(trigger)
                else:
                    coalesced.append(trigger)

            return CronTickReport(
                due=plan.triggers,
                enqueued=tuple(enqueued),
                coalesced=tuple(coalesced),
                durable_changed=durable_changed,
            )
