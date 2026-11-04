"""Teammate 生命周期管理器：显式创建 daemon 线程，区分完成、失败与协作式停止。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from threading import Event, Lock, Thread, current_thread

from ..config import ACTIVE_TEAMMATE_STATUSES, MAX_TEAMMATE_ERROR_LENGTH
from .models import TeammateSnapshot, TeammateSpec, TeammateStoppedResult
from .registry import TeammateRegistry


TeammateClock = Callable[[], float]  # 为 Teammate 生命周期提供可替换的 Unix 时间戳时钟
TeammateRunner = Callable[[TeammateSpec], str | TeammateStoppedResult]  # 兼容一次性 Runner；自治 Runner 仅在生命周期结束时返回停止信号


def _validate_join_timeout(timeout: float | None) -> float | None:
    """校验 join 总等待时间；None 表示等待本次快照中的全部线程结束。"""
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("Teammate join timeout 必须是有限的非负数或 None")
    return float(timeout)


def _format_runner_error(error: BaseException) -> str:
    """将后台异常规范化为长度受限且始终非空的审计文本。"""
    error_message = " ".join(str(error).split()) or "无异常信息"
    rendered = f"{type(error).__name__}：{error_message}"
    if len(rendered) <= MAX_TEAMMATE_ERROR_LENGTH:
        return rendered
    marker = "...[错误信息已截断]"
    return rendered[: MAX_TEAMMATE_ERROR_LENGTH - len(marker)] + marker


class TeammateManager:
    """通过注入 Runner 启动 Teammate，并协调线程引用与注册表状态。"""

    def __init__(
        self,
        runner: TeammateRunner,
        *,
        registry: TeammateRegistry | None = None,
        clock: TeammateClock = time.time,
        stop_event: Event | None = None,
    ) -> None:
        if not callable(runner):
            raise TypeError("TeammateManager runner 必须是可调用对象")
        if registry is not None and not isinstance(registry, TeammateRegistry):
            raise TypeError("TeammateManager registry 必须是 TeammateRegistry 或 None")
        if not callable(clock):
            raise TypeError("TeammateManager clock 必须是可调用对象")
        if stop_event is not None and not isinstance(stop_event, Event):
            raise TypeError("TeammateManager stop_event 必须是 Event 或 None")

        self._runner = runner  # 执行单个 Teammate 完整生命周期的可替换入口
        self._registry = registry or TeammateRegistry()  # 保存不可变生命周期快照
        self._clock = clock  # 创建、开始和结束状态共用的 Unix 时间戳来源
        self._threads: dict[str, Thread] = {}  # 当前仍可能存活的 Teammate 线程引用
        self._lifecycle_lock = Lock()  # 保护线程创建、登记和清理之间的进程内竞态
        self._stop_event = stop_event if stop_event is not None else Event()  # 与自治 Runner 共用；停止后不清零、不重启

    def _now(self) -> float:
        """读取并规范化注入时钟，拒绝无法用于生命周期排序的值。"""
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("TeammateManager clock 必须返回有限的非负数")
        return float(value)

    def _run_teammate(self, spec: TeammateSpec) -> None:
        """在线程中调用 Runner，并将任何正常或异常出口固化为终态快照。"""
        try:
            self._registry.mark_running(spec.name, self._now())
            result = self._runner(spec)
            if isinstance(result, TeammateStoppedResult):
                self._registry.mark_stopped(spec.name, self._now())
                return
            if not isinstance(result, str):
                raise TypeError("Teammate runner 必须返回字符串结果或 TeammateStoppedResult")
            self._registry.mark_completed(spec.name, result, self._now())
        except BaseException as error:
            try:
                self._registry.mark_failed(
                    spec.name,
                    _format_runner_error(error),
                    self._now(),
                )
            except BaseException:
                # 注册表或时钟自身已损坏时，线程无法安全伪造一个合法终态。
                pass
        # 保留线程引用直到 join 确认退出，不能在线程退出前移除导致清理误报成功。

    def spawn(
        self,
        name: str,
        role: str,
        prompt: str,
        *,
        task_ids: list[str] | None = None,
    ) -> TeammateSnapshot:
        """原子占用名称并启动一次 daemon 线程，返回最初的 starting 快照。"""
        if task_ids is not None and not isinstance(task_ids, list):
            raise TypeError("spawn_teammate task_ids 必须是列表或 None")
        spec = TeammateSpec(
            name=name,
            role=role,
            prompt=prompt,
            created_at=self._now(),
            task_ids=() if task_ids is None else tuple(task_ids),
        )
        with self._lifecycle_lock:
            if self._stop_event.is_set():
                raise RuntimeError("TeammateManager 已进入停止状态，不能创建新成员")
            snapshot = self._registry.register(spec)
            try:
                thread = Thread(
                    target=self._run_teammate,
                    args=(spec,),
                    name=f"mini-cc-teammate-{spec.name}",
                    daemon=True,
                )
                self._threads[spec.name] = thread
                thread.start()
            except BaseException as error:
                self._threads.pop(spec.name, None)
                self._registry.mark_failed(
                    spec.name,
                    _format_runner_error(error),
                    self._now(),
                )
                raise
            return snapshot

    def get(self, name: str) -> TeammateSnapshot:
        """取得指定 Teammate 的当前不可变状态快照。"""
        return self._registry.get(name)

    def list_all(self) -> list[TeammateSnapshot]:
        """取得按创建顺序排列的全部 Teammate 状态快照。"""
        return self._registry.list_all()

    def is_member_active(self, name: str) -> bool:
        """保守判断已登记成员是否仍活跃；终态已写入但线程未退出时仍返回 True。"""
        with self._lifecycle_lock:
            snapshot = self._registry.get(name)
            thread = self._threads.get(snapshot.spec.name)
            return snapshot.status in ACTIVE_TEAMMATE_STATUSES or (
                thread is not None and thread.is_alive()
            )

    def request_stop_all(self) -> None:
        """与 spawn 共用锁关闭创建入口，并唤醒共享停止信号的 Runner；不等待模型批准。"""
        with self._lifecycle_lock:
            self._stop_event.set()

    def join_all(self, timeout: float | None = None) -> bool:
        """在一个总超时预算内等待调用开始时已经登记的所有线程。"""
        normalized_timeout = _validate_join_timeout(timeout)
        with self._lifecycle_lock:
            threads = list(self._threads.values())
        if any(thread is current_thread() for thread in threads):
            raise RuntimeError("Teammate 线程不能通过 Manager 等待自身结束")

        deadline = (
            None
            if normalized_timeout is None
            else time.monotonic() + normalized_timeout
        )
        for thread in threads:
            remaining_timeout = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining_timeout)
        with self._lifecycle_lock:
            self._threads = {
                name: thread for name, thread in self._threads.items() if thread.is_alive()
            }
            return not self._threads  # 判断真实线程，而不是仅依赖注册表中的终态
