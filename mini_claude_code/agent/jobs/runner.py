"""后台 Job Runner：在线程中执行已授权 Shell 命令，并控制输出、超时与取消。"""

from __future__ import annotations

import locale
import math
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from ..config import (
    JOB_OUTPUT_READ_CHUNK_BYTES,
    JOB_POLL_INTERVAL_SECONDS,
    JOB_TERMINATE_GRACE_SECONDS,
    MAX_JOB_ERROR_LENGTH,
    MAX_JOB_OUTPUT_BYTES,
    MAX_JOB_OUTPUT_CHARS,
    TERMINAL_JOB_STATUSES,
    WORKDIR,
)
from .models import BackgroundJob
from .registry import JobRegistry


RunnerClock = Callable[[], float]


@dataclass
class _JobControl:
    """保存一个 Worker 的可变运行时对象，不作为模型可见的 Job 数据。"""

    cancel_event: threading.Event = field(default_factory=threading.Event)  # 取消请求信号
    finished_event: threading.Event = field(default_factory=threading.Event)  # Worker 完成信号
    thread: threading.Thread | None = None  # 执行 Job 的 daemon 线程
    process: subprocess.Popen[bytes] | None = None  # 当前受控的 Shell 子进程
    process_lock: threading.Lock = field(default_factory=threading.Lock)  # 保护进程引用


class _BoundedOutputCollector:
    """持续排空进程输出，但只在内存中保留有界的原始字节。"""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_chars: int,
        encoding: str,
    ) -> None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("输出字节上限必须是正整数")
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars <= 0
        ):
            raise ValueError("输出字符上限必须是正整数")
        if not isinstance(encoding, str) or not encoding.strip():
            raise ValueError("输出编码必须是非空字符串")

        self._max_bytes = max_bytes
        self._max_chars = max_chars
        self._encoding = encoding
        self._buffer = bytearray()
        self._truncated = False
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        """保存仍在预算内的部分；超限字节由调用方读取后直接丢弃。"""
        if not isinstance(chunk, bytes):
            raise TypeError("后台输出分块必须是 bytes")
        if not chunk:
            return

        with self._lock:
            remaining = self._max_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self._truncated = True

    def snapshot(self) -> tuple[str, bool]:
        """返回当前稳定文本快照以及是否发生过字节或字符截断。"""
        with self._lock:
            raw_output = bytes(self._buffer)
            output_truncated = self._truncated

        output = raw_output.decode(self._encoding, errors="replace").strip()
        if len(output) > self._max_chars:
            output = output[: self._max_chars]
            output_truncated = True
        return output, output_truncated


class BackgroundJobRunner:
    """派发并监管当前 CLI 进程中的后台 Shell Job。

    Runner 只负责执行已经通过 PreToolUse 与权限策略的命令；它不会自行执行
    权限判断，也不能作为绕过 Harness 的公开 Shell 入口。
    """

    def __init__(
        self,
        registry: JobRegistry,
        *,
        workdir: Path = WORKDIR,
        clock: RunnerClock = time.monotonic,
        poll_interval_seconds: float = JOB_POLL_INTERVAL_SECONDS,
        terminate_grace_seconds: float = JOB_TERMINATE_GRACE_SECONDS,
        output_encoding: str | None = None,
    ) -> None:
        if not isinstance(registry, JobRegistry):
            raise TypeError("BackgroundJobRunner registry 必须是 JobRegistry 实例")
        if not isinstance(workdir, Path):
            raise TypeError("BackgroundJobRunner workdir 必须是 pathlib.Path")
        if not callable(clock):
            raise TypeError("BackgroundJobRunner clock 必须是可调用对象")
        self._validate_positive_number("poll_interval_seconds", poll_interval_seconds)
        self._validate_positive_number("terminate_grace_seconds", terminate_grace_seconds)

        resolved_workdir = workdir.resolve()
        if not resolved_workdir.is_dir():
            raise ValueError(
                f"BackgroundJobRunner 工作目录不存在或不是目录：{resolved_workdir}"
            )

        selected_encoding = (
            locale.getpreferredencoding(False)
            if output_encoding is None
            else output_encoding
        )
        if not isinstance(selected_encoding, str) or not selected_encoding.strip():
            raise ValueError("BackgroundJobRunner output_encoding 必须是非空字符串")
        try:
            "".encode(selected_encoding)
        except LookupError as error:
            raise ValueError(
                f"BackgroundJobRunner output_encoding 不受支持：{selected_encoding!r}"
            ) from error

        self._registry = registry
        self._workdir = resolved_workdir
        self._clock = clock
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._terminate_grace_seconds = float(terminate_grace_seconds)
        self._output_encoding = selected_encoding
        self._controls: dict[str, _JobControl] = {}
        self._controls_lock = threading.Lock()
        self._completion_queue: queue.SimpleQueue[str] = queue.SimpleQueue()  # 待消费终态 ID
        self._published_completion_ids: set[str] = set()  # 已发布过完成事件的 Job ID
        self._completion_lock = threading.Lock()  # 保护发布去重集合与入队顺序

    @staticmethod
    def _validate_positive_number(field_name: str, value: float) -> None:
        """校验 Runner 内部时间配置。"""
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{field_name} 必须是有限的正数")

    def _now(self) -> float:
        """读取用于超时判断的单调时钟。"""
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("BackgroundJobRunner clock 必须返回有限的非负数")
        return float(value)

    @staticmethod
    def _format_error(error: BaseException) -> str:
        """将内部异常转成满足 Job 模型长度约束的稳定错误文本。"""
        detail = str(error).strip()
        message = (
            type(error).__name__
            if not detail
            else f"{type(error).__name__}：{detail}"
        )
        return message[:MAX_JOB_ERROR_LENGTH]

    def _require_control(self, job_id: str) -> _JobControl:
        """读取 Job 对应的运行时控制对象。"""
        with self._controls_lock:
            control = self._controls.get(job_id)
        if control is None:
            raise RuntimeError(f"Background Job {job_id!r} 缺少运行时控制对象")
        return control

    def start_shell_job(
        self,
        *,
        tool_use_id: str,
        command: str,
        timeout_seconds: float,
    ) -> BackgroundJob:
        """创建后台 Shell Job、启动 daemon Worker，并立即返回当前快照。"""
        job = self._registry.create(
            tool_name="shell",
            tool_use_id=tool_use_id,
            command=command,
            timeout_seconds=timeout_seconds,
        )
        control = _JobControl()
        thread = threading.Thread(
            target=self._run_shell_job,
            args=(job.id, control),
            name=f"mini-cc-job-{job.id[4:12]}",
            daemon=True,
        )
        control.thread = thread

        with self._controls_lock:
            self._controls[job.id] = control

        try:
            thread.start()
        except Exception as error:
            failed_job = self._registry.mark_failed(
                job.id,
                exit_code=None,
                output=None,
                error=f"后台 Worker 启动失败：{self._format_error(error)}"[
                    :MAX_JOB_ERROR_LENGTH
                ],
                output_truncated=False,
            )
            control.finished_event.set()
            return failed_job
        return self._registry.get(job.id)

    def request_cancel(self, job_id: str) -> bool:
        """请求取消尚未结束的 Job；最终状态由 Worker 确认进程退出后写入。"""
        job = self._registry.get(job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            return False
        control = self._require_control(job_id)
        control.cancel_event.set()
        return True

    def wait(
        self,
        job_id: str,
        timeout: float | None = None,
    ) -> BackgroundJob:
        """等待 Job 完成；等待超时时返回仍处于活动状态的最新快照。"""
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("等待超时必须是有限的非负数或 None")

        job = self._registry.get(job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        control = self._require_control(job_id)
        control.finished_event.wait(None if timeout is None else float(timeout))
        return self._registry.get(job_id)

    def _publish_completion(self, job_id: str) -> None:
        """将终态 Job ID 最多发布一次，供 Agent 主线程消费。"""
        job = self._registry.get(job_id)
        if job.status not in TERMINAL_JOB_STATUSES:
            raise RuntimeError(
                f"不能发布尚未结束的 Background Job {job_id!r}：{job.status}"
            )
        with self._completion_lock:
            if job_id in self._published_completion_ids:
                return
            self._completion_queue.put(job_id)
            self._published_completion_ids.add(job_id)

    def collect_completed_jobs(self) -> tuple[BackgroundJob, ...]:
        """按完成事件到达顺序，一次性取出尚未消费的终态 Job。"""
        completed_jobs: list[BackgroundJob] = []
        while True:
            try:
                job_id = self._completion_queue.get_nowait()
            except queue.Empty:
                break
            job = self._registry.get(job_id)
            if job.status not in TERMINAL_JOB_STATUSES:
                raise RuntimeError(
                    f"完成队列包含非终态 Background Job {job_id!r}：{job.status}"
                )
            completed_jobs.append(job)
        return tuple(completed_jobs)

    def _spawn_shell_process(self, command: str) -> subprocess.Popen[bytes]:
        """在独立进程组中启动 Shell，便于取消时终止直接子进程。"""
        options: dict[str, object] = {
            "args": command,
            "shell": True,
            "cwd": self._workdir,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": False,
            "bufsize": 0,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        return subprocess.Popen(**options)  # type: ignore[arg-type]

    @staticmethod
    def _drain_output(
        stream: BinaryIO,
        collector: _BoundedOutputCollector,
        reader_errors: list[str],
    ) -> None:
        """持续读取合并后的 stdout/stderr，避免子进程因管道写满而阻塞。"""
        try:
            while True:
                chunk = stream.read(JOB_OUTPUT_READ_CHUNK_BYTES)
                if not chunk:
                    break
                collector.append(chunk)
        except Exception as error:
            reader_errors.append(BackgroundJobRunner._format_error(error))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _finish_output_reader(
        self,
        process: subprocess.Popen[bytes],
        reader_thread: threading.Thread,
        reader_errors: list[str],
    ) -> None:
        """有限等待输出线程结束，必要时关闭本地管道句柄解除阻塞。"""
        reader_thread.join(self._terminate_grace_seconds)
        if reader_thread.is_alive() and process.stdout is not None:
            try:
                process.stdout.close()
            except Exception as error:
                reader_errors.append(self._format_error(error))
            reader_thread.join(self._terminate_grace_seconds)
        if reader_thread.is_alive():
            reader_errors.append("后台输出读取线程未能及时结束")

    def _terminate_process(
        self,
        process: subprocess.Popen[bytes],
    ) -> tuple[int, bool]:
        """终止仍在运行的进程，返回退出码及是否实际发出了终止信号。"""
        current_exit_code = process.poll()
        if current_exit_code is not None:
            return current_exit_code, False

        termination_requested = False
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            termination_requested = True
        except (OSError, ValueError):
            current_exit_code = process.poll()
            if current_exit_code is not None:
                return current_exit_code, False
            try:
                process.terminate()
                termination_requested = True
            except OSError:
                current_exit_code = process.poll()
                if current_exit_code is not None:
                    return current_exit_code, False

        try:
            return (
                process.wait(timeout=self._terminate_grace_seconds),
                termination_requested,
            )
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                termination_requested = True
            except OSError:
                process.kill()
                termination_requested = True

        try:
            return (
                process.wait(timeout=self._terminate_grace_seconds),
                termination_requested,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("后台进程在强制终止后仍未退出") from error

    def _run_shell_job(self, job_id: str, control: _JobControl) -> None:
        """执行一个 Shell Job，并保证 Worker 的所有出口都会设置完成事件。"""
        process: subprocess.Popen[bytes] | None = None
        reader_thread: threading.Thread | None = None
        reader_errors: list[str] = []
        collector = _BoundedOutputCollector(
            max_bytes=MAX_JOB_OUTPUT_BYTES,
            max_chars=MAX_JOB_OUTPUT_CHARS,
            encoding=self._output_encoding,
        )

        try:
            queued_job = self._registry.get(job_id)
            if control.cancel_event.is_set():
                self._registry.mark_cancelled(
                    job_id,
                    reason="Job 在开始执行前收到取消请求",
                )
                return

            self._registry.mark_running(job_id)
            deadline = self._now() + queued_job.timeout_seconds
            if control.cancel_event.is_set():
                self._registry.mark_cancelled(
                    job_id,
                    reason="Job 在启动子进程前收到取消请求",
                )
                return

            process = self._spawn_shell_process(queued_job.command)
            with control.process_lock:
                control.process = process

            if process.stdout is None:
                raise RuntimeError("后台进程没有可读取的输出管道")
            reader_thread = threading.Thread(
                target=self._drain_output,
                args=(process.stdout, collector, reader_errors),
                name=f"mini-cc-output-{job_id[4:12]}",
                daemon=True,
            )
            reader_thread.start()

            terminal_reason: str | None = None
            while process.poll() is None:
                if control.cancel_event.is_set():
                    terminal_reason = "cancelled"
                    break
                if self._now() >= deadline:
                    terminal_reason = "timeout"
                    break
                control.cancel_event.wait(self._poll_interval_seconds)

            if terminal_reason is not None:
                exit_code, termination_requested = self._terminate_process(
                    process
                )
                if not termination_requested:
                    # 取消或超时检查与进程自然退出发生竞态时，以真实退出结果为准。
                    terminal_reason = None
            else:
                exit_code = process.wait()

            self._finish_output_reader(process, reader_thread, reader_errors)
            output, output_truncated = collector.snapshot()
            if terminal_reason == "cancelled":
                self._registry.mark_cancelled(
                    job_id,
                    # 某些命令会捕获终止信号并返回 0；取消语义优先，不保留成功码。
                    exit_code=None if exit_code == 0 else exit_code,
                    output=output,
                    reason="Job 已按用户请求取消",
                    output_truncated=output_truncated,
                )
            elif terminal_reason == "timeout":
                self._registry.mark_failed(
                    job_id,
                    exit_code=exit_code,
                    output=output,
                    error=(
                        f"命令执行超过 {queued_job.timeout_seconds:g} 秒，Job 已终止"
                    ),
                    output_truncated=output_truncated,
                )
            elif reader_errors:
                self._registry.mark_failed(
                    job_id,
                    exit_code=exit_code,
                    output=output,
                    error=("后台输出采集失败：" + "；".join(reader_errors))[
                        :MAX_JOB_ERROR_LENGTH
                    ],
                    output_truncated=output_truncated,
                )
            elif exit_code == 0:
                self._registry.mark_succeeded(
                    job_id,
                    exit_code=exit_code,
                    output=output,
                    output_truncated=output_truncated,
                )
            else:
                self._registry.mark_failed(
                    job_id,
                    exit_code=exit_code,
                    output=output,
                    error=None,
                    output_truncated=output_truncated,
                )
        except Exception as error:
            if process is not None and process.poll() is None:
                try:
                    self._terminate_process(process)
                except Exception:
                    pass
            if process is not None and reader_thread is not None:
                self._finish_output_reader(process, reader_thread, reader_errors)

            latest_job = self._registry.get(job_id)
            if latest_job.status not in TERMINAL_JOB_STATUSES:
                output, output_truncated = collector.snapshot()
                self._registry.mark_failed(
                    job_id,
                    exit_code=None if process is None else process.poll(),
                    output=None if latest_job.status == "queued" else output,
                    error=f"后台 Job 执行异常：{self._format_error(error)}"[
                        :MAX_JOB_ERROR_LENGTH
                    ],
                    output_truncated=(
                        False if latest_job.status == "queued" else output_truncated
                    ),
                )
        finally:
            with control.process_lock:
                control.process = None
            try:
                latest_job = self._registry.get(job_id)
                if latest_job.status in TERMINAL_JOB_STATUSES:
                    self._publish_completion(job_id)
            finally:
                control.finished_event.set()
