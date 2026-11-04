"""后台 Job 领域模型：定义一次异步工具执行的字段与生命周期不变量。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import (
    JOB_ID_PATTERN,
    JOB_TOOL_NAME_PATTERN,
    MAX_JOB_COMMAND_LENGTH,
    MAX_JOB_ERROR_LENGTH,
    MAX_JOB_OUTPUT_CHARS,
    MAX_JOB_TIMEOUT_SECONDS,
    MAX_JOB_TOOL_NAME_LENGTH,
    MAX_JOB_TOOL_USE_ID_LENGTH,
    MIN_JOB_TIMEOUT_SECONDS,
    TERMINAL_JOB_STATUSES,
    VALID_JOB_STATUSES,
    JobStatus,
)


def _validate_timestamp(field_name: str, value: float | None) -> None:
    """校验来自同一单调时钟的可选时间戳。"""
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"Background Job {field_name} 必须是有限的非负数或 None")


@dataclass(frozen=True)
class BackgroundJob:
    """一条不可变的后台执行快照；状态迁移通过创建新快照完成。"""

    id: str  # Harness 生成的稳定 Job ID
    tool_name: str  # 发起后台执行的工具名称
    tool_use_id: str  # 原始模型 tool_use ID，仅用于审计来源
    command: str  # 本次后台执行的命令快照
    status: JobStatus  # queued → running → succeeded/failed/cancelled
    created_at: float  # 使用进程内单调时钟记录的创建时间
    timeout_seconds: float  # 本次执行允许占用的最长秒数
    started_at: float | None = None  # Worker 真正开始处理的时间
    finished_at: float | None = None  # Job 进入终态的时间
    exit_code: int | None = None  # 子进程退出码；启动失败或排队取消时可为 None
    output: str | None = None  # 成功输出、失败输出或取消前已收集的部分输出
    error: str | None = None  # 启动失败、超时或取消原因
    output_truncated: bool = False  # output 是否因 Harness 上限被截断

    def __post_init__(self) -> None:
        """校验字段边界、时间顺序以及各生命周期状态允许的数据形状。"""
        if not isinstance(self.id, str) or JOB_ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError("Background Job ID 必须符合 job_<32 位小写十六进制> 格式")

        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("Background Job tool_name 必须是非空字符串")
        if self.tool_name != self.tool_name.strip():
            raise ValueError("Background Job tool_name 不能包含首尾空白")
        if len(self.tool_name) > MAX_JOB_TOOL_NAME_LENGTH:
            raise ValueError(
                f"Background Job tool_name 不能超过 {MAX_JOB_TOOL_NAME_LENGTH} 个字符"
            )
        if JOB_TOOL_NAME_PATTERN.fullmatch(self.tool_name) is None:
            raise ValueError("Background Job tool_name 必须使用小写 snake_case")

        if not isinstance(self.tool_use_id, str) or not self.tool_use_id.strip():
            raise ValueError("Background Job tool_use_id 必须是非空字符串")
        if self.tool_use_id != self.tool_use_id.strip():
            raise ValueError("Background Job tool_use_id 不能包含首尾空白")
        if len(self.tool_use_id) > MAX_JOB_TOOL_USE_ID_LENGTH:
            raise ValueError(
                "Background Job tool_use_id 不能超过 "
                f"{MAX_JOB_TOOL_USE_ID_LENGTH} 个字符"
            )

        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("Background Job command 必须是非空字符串")
        if len(self.command) > MAX_JOB_COMMAND_LENGTH:
            raise ValueError(
                f"Background Job command 不能超过 {MAX_JOB_COMMAND_LENGTH} 个字符"
            )

        if self.status not in VALID_JOB_STATUSES:
            raise ValueError(f"未知的 Background Job 状态 {self.status!r}")

        _validate_timestamp("created_at", self.created_at)
        _validate_timestamp("started_at", self.started_at)
        _validate_timestamp("finished_at", self.finished_at)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not MIN_JOB_TIMEOUT_SECONDS
            <= self.timeout_seconds
            <= MAX_JOB_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "Background Job timeout_seconds 必须位于 "
                f"[{MIN_JOB_TIMEOUT_SECONDS}, {MAX_JOB_TIMEOUT_SECONDS}] 之间"
            )

        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("Background Job started_at 不能早于 created_at")
        if self.finished_at is not None and self.finished_at < self.created_at:
            raise ValueError("Background Job finished_at 不能早于 created_at")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("Background Job finished_at 不能早于 started_at")

        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("Background Job exit_code 必须是整数或 None")
        if self.output is not None and not isinstance(self.output, str):
            raise ValueError("Background Job output 必须是字符串或 None")
        if isinstance(self.output, str) and len(self.output) > MAX_JOB_OUTPUT_CHARS:
            raise ValueError(
                f"Background Job output 不能超过 {MAX_JOB_OUTPUT_CHARS} 个字符"
            )
        if self.error is not None:
            if not isinstance(self.error, str) or not self.error.strip():
                raise ValueError("Background Job error 必须是非空字符串或 None")
            if len(self.error) > MAX_JOB_ERROR_LENGTH:
                raise ValueError(
                    f"Background Job error 不能超过 {MAX_JOB_ERROR_LENGTH} 个字符"
                )
        if type(self.output_truncated) is not bool:
            raise ValueError("Background Job output_truncated 必须是布尔值")
        if self.output_truncated and self.output is None:
            raise ValueError("Background Job 输出被截断时必须保留字符串 output")

        self._validate_lifecycle_state()

    def _validate_lifecycle_state(self) -> None:
        """根据当前状态验证开始、结束与结果字段之间的组合约束。"""
        if self.status == "queued":
            if any(
                value is not None
                for value in (
                    self.started_at,
                    self.finished_at,
                    self.exit_code,
                    self.output,
                    self.error,
                )
            ) or self.output_truncated:
                raise ValueError("queued Background Job 不能包含执行或结果字段")
            return

        if self.status == "running":
            if self.started_at is None:
                raise ValueError("running Background Job 必须具有 started_at")
            if any(
                value is not None
                for value in (
                    self.finished_at,
                    self.exit_code,
                    self.output,
                    self.error,
                )
            ) or self.output_truncated:
                raise ValueError("running Background Job 不能提前包含结束或结果字段")
            return

        if self.status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"Background Job 状态 {self.status!r} 没有生命周期规则")
        if self.finished_at is None:
            raise ValueError(f"{self.status} Background Job 必须具有 finished_at")

        if self.status == "succeeded":
            if self.started_at is None:
                raise ValueError("succeeded Background Job 必须具有 started_at")
            if self.exit_code != 0:
                raise ValueError("succeeded Background Job 的 exit_code 必须为 0")
            if not isinstance(self.output, str):
                raise ValueError("succeeded Background Job 必须具有字符串 output")
            if self.error is not None:
                raise ValueError("succeeded Background Job 的 error 必须为 None")
            return

        if self.status == "failed":
            if self.started_at is None:
                if self.error is None:
                    raise ValueError(
                        "启动前失败的 Background Job 必须具有 error"
                    )
                if (
                    self.exit_code is not None
                    or self.output is not None
                    or self.output_truncated
                ):
                    raise ValueError(
                        "启动前失败的 Background Job 不能包含执行结果"
                    )
                return
            has_nonzero_exit = self.exit_code is not None and self.exit_code != 0
            if not has_nonzero_exit and self.error is None:
                raise ValueError(
                    "failed Background Job 必须具有非零 exit_code 或 error"
                )
            return

        if self.exit_code == 0:
            raise ValueError("cancelled Background Job 不能具有成功退出码 0")
        if self.started_at is None:
            if self.exit_code is not None or self.output is not None or self.output_truncated:
                raise ValueError("排队阶段取消的 Background Job 不能包含执行结果")


def job_to_record(job: BackgroundJob) -> dict[str, object]:
    """将已校验 Job 转换为稳定、JSON 兼容且不复用工具协议的记录。"""
    if not isinstance(job, BackgroundJob):
        raise TypeError("只能序列化 BackgroundJob 实例")
    return {
        "id": job.id,
        "tool_name": job.tool_name,
        "tool_use_id": job.tool_use_id,
        "command": job.command,
        "status": job.status,
        "created_at": job.created_at,
        "timeout_seconds": job.timeout_seconds,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "exit_code": job.exit_code,
        "output": job.output,
        "error": job.error,
        "output_truncated": job.output_truncated,
    }
