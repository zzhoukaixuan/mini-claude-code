"""后台 Job 注册表：在线程锁内创建不可变快照并执行受控状态迁移。"""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from ..config import (
    JOB_ID_PATTERN,
    MAX_BACKGROUND_JOBS,
    MAX_JOB_ID_GENERATION_ATTEMPTS,
)
from .models import BackgroundJob


Clock = Callable[[], float]
JobIdFactory = Callable[[], str]


class JobNotFoundError(LookupError):
    """请求的 Job ID 不在当前进程注册表中。"""


class InvalidJobTransitionError(ValueError):
    """请求的 Job 状态迁移不符合生命周期。"""


def generate_job_id() -> str:
    """生成由固定前缀和 UUID4 hex 组成的 Job ID。"""
    job_id = f"job_{uuid.uuid4().hex}"
    if JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise RuntimeError("Harness 生成了格式无效的 Background Job ID")
    return job_id


class JobRegistry:
    """保存当前 CLI 进程中的 Job，并原子执行每次生命周期迁移。"""

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        id_factory: JobIdFactory = generate_job_id,
        max_jobs: int = MAX_BACKGROUND_JOBS,
    ) -> None:
        if not callable(clock):
            raise TypeError("Job Registry clock 必须是可调用对象")
        if not callable(id_factory):
            raise TypeError("Job Registry id_factory 必须是可调用对象")
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or max_jobs <= 0:
            raise ValueError("Job Registry max_jobs 必须是正整数")
        self._clock = clock
        self._id_factory = id_factory
        self._max_jobs = max_jobs
        self._jobs: dict[str, BackgroundJob] = {}
        self._job_id_by_tool_use_id: dict[str, str] = {}
        self._lock = threading.Lock()

    def _now(self) -> float:
        """读取并规范化注入的单调时钟值。"""
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("Job Registry clock 必须返回有限的非负数")
        return float(value)

    def _allocate_job_id_locked(self) -> str:
        """在持锁状态下生成尚未使用的合法 Job ID。"""
        for _ in range(MAX_JOB_ID_GENERATION_ATTEMPTS):
            candidate = self._id_factory()
            if not isinstance(candidate, str) or JOB_ID_PATTERN.fullmatch(candidate) is None:
                raise ValueError(
                    "Job Registry id_factory 必须返回 "
                    "job_<32 位小写十六进制> 格式的字符串"
                )
            if candidate not in self._jobs:
                return candidate
        raise RuntimeError(
            "Background Job ID 连续碰撞，超过 Harness 允许的生成次数"
        )

    def _require_job_locked(self, job_id: str) -> BackgroundJob:
        """校验 Job ID，并取得持锁状态下的最新快照。"""
        if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise ValueError(
                "Background Job ID 必须符合 job_<32 位小写十六进制> 格式"
            )
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Background Job {job_id!r} 不存在")
        return job

    @staticmethod
    def _require_source_status(
        job: BackgroundJob,
        target_status: str,
        allowed_source_statuses: frozenset[str],
    ) -> None:
        """拒绝终态回退和不在白名单中的状态跳转。"""
        if job.status not in allowed_source_statuses:
            allowed_text = ", ".join(sorted(allowed_source_statuses))
            raise InvalidJobTransitionError(
                f"Background Job {job.id!r} 当前状态为 {job.status!r}，"
                f"不能迁移到 {target_status!r}；允许的来源状态：{allowed_text}"
            )

    def create(
        self,
        tool_name: str,
        tool_use_id: str,
        command: str,
        timeout_seconds: float,
    ) -> BackgroundJob:
        """创建 queued Job，并拒绝同一 tool_use 的重复派发。"""
        with self._lock:
            if (
                isinstance(tool_use_id, str)
                and tool_use_id in self._job_id_by_tool_use_id
            ):
                existing_job_id = self._job_id_by_tool_use_id[tool_use_id]
                raise ValueError(
                    f"tool_use_id {tool_use_id!r} 已经派发为 Background Job "
                    f"{existing_job_id!r}"
                )
            if len(self._jobs) >= self._max_jobs:
                raise ValueError(
                    f"Background Job 数量已经达到上限 {self._max_jobs}"
                )

            job = BackgroundJob(
                id=self._allocate_job_id_locked(),
                tool_name=tool_name,
                tool_use_id=tool_use_id,
                command=command,
                status="queued",
                created_at=self._now(),
                timeout_seconds=timeout_seconds,
            )
            self._jobs[job.id] = job
            self._job_id_by_tool_use_id[job.tool_use_id] = job.id
            return job

    def get(self, job_id: str) -> BackgroundJob:
        """返回指定 Job 的不可变最新快照。"""
        with self._lock:
            return self._require_job_locked(job_id)

    def list_jobs(self) -> tuple[BackgroundJob, ...]:
        """按创建时间和 ID 稳定返回全部不可变 Job 快照。"""
        with self._lock:
            return tuple(
                sorted(
                    self._jobs.values(),
                    key=lambda job: (job.created_at, job.id),
                )
            )

    def mark_running(self, job_id: str) -> BackgroundJob:
        """将 queued Job 标记为 Worker 已经开始处理。"""
        with self._lock:
            job = self._require_job_locked(job_id)
            self._require_source_status(job, "running", frozenset({"queued"}))
            updated = replace(
                job,
                status="running",
                started_at=self._now(),
            )
            self._jobs[job.id] = updated
            return updated

    def mark_succeeded(
        self,
        job_id: str,
        *,
        exit_code: int,
        output: str,
        output_truncated: bool,
    ) -> BackgroundJob:
        """将 running Job 迁移为不可逆的 succeeded。"""
        with self._lock:
            job = self._require_job_locked(job_id)
            self._require_source_status(job, "succeeded", frozenset({"running"}))
            updated = replace(
                job,
                status="succeeded",
                finished_at=self._now(),
                exit_code=exit_code,
                output=output,
                error=None,
                output_truncated=output_truncated,
            )
            self._jobs[job.id] = updated
            return updated

    def mark_failed(
        self,
        job_id: str,
        *,
        exit_code: int | None,
        output: str | None,
        error: str | None,
        output_truncated: bool,
    ) -> BackgroundJob:
        """记录启动失败或 running Job 的非成功终态。"""
        with self._lock:
            job = self._require_job_locked(job_id)
            self._require_source_status(
                job,
                "failed",
                frozenset({"queued", "running"}),
            )
            updated = replace(
                job,
                status="failed",
                finished_at=self._now(),
                exit_code=exit_code,
                output=output,
                error=error,
                output_truncated=output_truncated,
            )
            self._jobs[job.id] = updated
            return updated

    def mark_cancelled(
        self,
        job_id: str,
        *,
        exit_code: int | None = None,
        output: str | None = None,
        reason: str = "Job 已取消",
        output_truncated: bool = False,
    ) -> BackgroundJob:
        """将尚未结束的 queued/running Job 迁移为 cancelled。"""
        with self._lock:
            job = self._require_job_locked(job_id)
            self._require_source_status(
                job,
                "cancelled",
                frozenset({"queued", "running"}),
            )
            updated = replace(
                job,
                status="cancelled",
                finished_at=self._now(),
                exit_code=exit_code,
                output=output,
                error=reason,
                output_truncated=output_truncated,
            )
            self._jobs[job.id] = updated
            return updated
