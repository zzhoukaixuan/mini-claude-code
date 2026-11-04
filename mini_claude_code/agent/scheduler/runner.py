"""Cron 后台 Runner：显式管理可停止的调度轮询线程与单事件交付线程。"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from threading import Event, Lock, Thread, current_thread
from typing import Literal, Protocol, runtime_checkable

from ..config import (
    CRON_QUEUE_POLL_INTERVAL_SECONDS,
    CRON_SCHEDULER_POLL_INTERVAL_SECONDS,
)
from .events import CronTrigger
from .queue import CronTriggerQueue
from .service import CronScheduler


CronClock = Callable[[], datetime]  # 为轮询线程提供带时区当前时间的可替换时钟
CronDelivery = Callable[[CronTrigger], None]  # 在 Agent 锁内交付单个触发的回调
CronRunnerStage = Literal["CronScheduler", "CronDelivery"]  # Runner 错误发生阶段
CronRunnerErrorCallback = Callable[[CronRunnerStage, Exception], None]  # Runner 故障观察回调


@runtime_checkable
class AgentTurnLock(Protocol):
    """Queue Processor 所需的最小非阻塞锁接口。"""

    def acquire(self, blocking: bool = True) -> bool:
        """尝试取得 Agent 回合所有权。"""
        ...

    def release(self) -> None:
        """释放 Agent 回合所有权。"""
        ...


def system_local_now() -> datetime:
    """返回带系统本地 UTC 偏移的当前时间；测试可注入 Fake Clock 替代。"""
    return datetime.now().astimezone()


def _validate_poll_interval(interval_seconds: float, *, name: str) -> float:
    """规范化 Runner 轮询间隔，并拒绝布尔值、无穷值与忙循环配置。"""
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or interval_seconds <= 0
    ):
        raise ValueError(f"{name} 必须是有限的正数秒")
    return float(interval_seconds)


def _validate_join_timeout(timeout: float | None) -> float | None:
    """规范化 join 超时；None 表示由显式调用方决定无限等待。"""
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("Cron Runner join timeout 必须是有限的非负数或 None")
    return float(timeout)


def _notify_runner_error(
    callback: CronRunnerErrorCallback,
    stage: CronRunnerStage,
    error: Exception,
) -> None:
    """尽力报告单次后台错误；观察回调自身失败也不能杀死 Runner。"""
    try:
        callback(stage, error)
    except Exception:
        pass


class CronPollingRunner:
    """在独立 daemon 线程中按注入时钟重复执行 CronScheduler.tick。"""

    def __init__(
        self,
        scheduler: CronScheduler,
        *,
        clock: CronClock = system_local_now,
        poll_interval_seconds: float = CRON_SCHEDULER_POLL_INTERVAL_SECONDS,
        error_callback: CronRunnerErrorCallback,
    ) -> None:
        """保存线程依赖但不读取时钟、不执行 Tick，也不自动启动线程。"""
        if not isinstance(scheduler, CronScheduler):
            raise TypeError("CronPollingRunner scheduler 必须是 CronScheduler")
        if not callable(clock):
            raise TypeError("CronPollingRunner clock 必须是可调用对象")
        if not callable(error_callback):
            raise TypeError("CronPollingRunner error_callback 必须是可调用对象")

        self._scheduler = scheduler  # 每次轮询执行纯 Tick 事务的调度服务
        self._clock = clock  # 生产带时区 datetime 的真实或测试时钟
        self._poll_interval_seconds = _validate_poll_interval(
            poll_interval_seconds,
            name="Cron Scheduler 轮询间隔",
        )
        self._error_callback = error_callback  # 单次 Clock/Tick 故障观察出口
        self._stop_event = Event()  # 可立即唤醒 wait 的软停止信号
        self._lifecycle_lock = Lock()  # 保护 Thread 引用和一次性启动约束
        self._thread: Thread | None = None  # start 前为空的 daemon 线程引用

    def _read_clock(self) -> datetime:
        """读取并校验带时区时间，避免宿主机默认时区被隐式猜测。"""
        moment = self._clock()
        if not isinstance(moment, datetime):
            raise TypeError("CronPollingRunner clock 必须返回 datetime")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("CronPollingRunner clock 必须返回带时区的 datetime")
        return moment

    def _run_loop(self) -> None:
        """立即执行首次 Tick，随后以可中断等待维持轮询。"""
        while not self._stop_event.is_set():
            try:
                self._scheduler.tick(self._read_clock())
            except Exception as error:
                _notify_runner_error(
                    self._error_callback,
                    "CronScheduler",
                    error,
                )
            if self._stop_event.wait(self._poll_interval_seconds):
                break

    def start(self) -> None:
        """显式启动一次 daemon 线程；停止后的 Runner 不允许重新启动。"""
        with self._lifecycle_lock:
            if self._thread is not None:
                raise RuntimeError("CronPollingRunner 不能重复启动")
            if self._stop_event.is_set():
                raise RuntimeError("CronPollingRunner 已请求停止，不能再启动")
            thread = Thread(
                target=self._run_loop,
                name="mini-cc-cron-scheduler",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                raise

    def request_stop(self) -> None:
        """幂等地请求软停止；无法中断正在执行的 Tick 或底层文件操作。"""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> bool:
        """等待线程结束，并返回它是否已在给定时间内停止。"""
        normalized_timeout = _validate_join_timeout(timeout)
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is current_thread():
            raise RuntimeError("CronPollingRunner 不能等待自身线程结束")
        thread.join(normalized_timeout)
        return not thread.is_alive()

    def is_running(self) -> bool:
        """线程安全地报告 daemon 线程当前是否仍存活。"""
        with self._lifecycle_lock:
            thread = self._thread
        return thread is not None and thread.is_alive()


class CronQueueProcessor:
    """等待 Agent 空闲后，从触发队列中取出并交付一个事件。"""

    def __init__(
        self,
        trigger_queue: CronTriggerQueue,
        agent_lock: AgentTurnLock,
        delivery: CronDelivery,
        *,
        poll_interval_seconds: float = CRON_QUEUE_POLL_INTERVAL_SECONDS,
        error_callback: CronRunnerErrorCallback,
    ) -> None:
        """保存交付依赖但不读取队列、不取得 Agent 锁，也不启动线程。"""
        if not isinstance(trigger_queue, CronTriggerQueue):
            raise TypeError(
                "CronQueueProcessor trigger_queue 必须是 CronTriggerQueue"
            )
        if not isinstance(agent_lock, AgentTurnLock):
            raise TypeError(
                "CronQueueProcessor agent_lock 必须支持 acquire/release"
            )
        if not callable(delivery):
            raise TypeError("CronQueueProcessor delivery 必须是可调用对象")
        if not callable(error_callback):
            raise TypeError("CronQueueProcessor error_callback 必须是可调用对象")

        self._trigger_queue = trigger_queue  # Scheduler 与 Processor 共享的事件队列
        self._agent_lock = agent_lock  # 串行化用户回合与定时 Agent 回合
        self._delivery = delivery  # 在已取得 Agent 锁时执行的单事件交付回调
        self._poll_interval_seconds = _validate_poll_interval(
            poll_interval_seconds,
            name="Cron Queue Processor 轮询间隔",
        )
        self._error_callback = error_callback  # 单次队列或交付故障观察出口
        self._stop_event = Event()  # 可立即唤醒 wait 的软停止信号
        self._lifecycle_lock = Lock()  # 保护 Thread 引用和一次性启动约束
        self._thread: Thread | None = None  # start 前为空的 daemon 线程引用

    def _deliver_once_if_idle(self) -> None:
        """只有 Agent 空闲时才取出一个触发；delivery 失败不会自动重试。"""
        if not self._trigger_queue.has_pending():
            return
        if not self._agent_lock.acquire(blocking=False):
            return
        try:
            triggers = self._trigger_queue.drain(limit=1)
            if triggers:
                self._delivery(triggers[0])
        finally:
            self._agent_lock.release()

    def _run_loop(self) -> None:
        """持续尝试单事件交付，并隔离每次队列或 delivery 异常。"""
        while not self._stop_event.is_set():
            try:
                self._deliver_once_if_idle()
            except Exception as error:
                _notify_runner_error(
                    self._error_callback,
                    "CronDelivery",
                    error,
                )
            if self._stop_event.wait(self._poll_interval_seconds):
                break

    def start(self) -> None:
        """显式启动一次 daemon 线程；停止后的 Processor 不允许重新启动。"""
        with self._lifecycle_lock:
            if self._thread is not None:
                raise RuntimeError("CronQueueProcessor 不能重复启动")
            if self._stop_event.is_set():
                raise RuntimeError("CronQueueProcessor 已请求停止，不能再启动")
            thread = Thread(
                target=self._run_loop,
                name="mini-cc-cron-queue-processor",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                raise

    def request_stop(self) -> None:
        """幂等地请求软停止；已经进入 delivery 的 Agent 回合不会被强制中断。"""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> bool:
        """等待线程结束，并返回它是否已在给定时间内停止。"""
        normalized_timeout = _validate_join_timeout(timeout)
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is current_thread():
            raise RuntimeError("CronQueueProcessor 不能等待自身线程结束")
        thread.join(normalized_timeout)
        return not thread.is_alive()

    def is_running(self) -> bool:
        """线程安全地报告 daemon 线程当前是否仍存活。"""
        with self._lifecycle_lock:
            thread = self._thread
        return thread is not None and thread.is_alive()
