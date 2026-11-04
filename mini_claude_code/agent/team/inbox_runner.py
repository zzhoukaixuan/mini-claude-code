"""Lead 邮箱后台处理器：在 Agent 空闲时自动消费团队消息并串行交付新回合。"""

from __future__ import annotations

import math
from collections.abc import Callable
from threading import Event, Lock, Thread, current_thread
from typing import Literal, Protocol, runtime_checkable

from ..config import TEAM_INBOX_POLL_INTERVAL_SECONDS, TEAM_LEAD_NAME
from .bus import MessageBus
from .models import TeamMessage


TeamInboxDelivery = Callable[[list[TeamMessage]], None]  # 在已取得 Agent 锁时交付一批 Lead 消息
TeamInboxRunnerStage = Literal["TeamInboxPoller", "TeamInboxDelivery"]  # Inbox Runner 的错误阶段
TeamInboxErrorCallback = Callable[[TeamInboxRunnerStage, Exception], None]  # 单次轮询或交付故障观察出口


@runtime_checkable
class TeamAgentTurnLock(Protocol):
    """Lead Inbox Processor 所需的最小非阻塞 Agent 锁接口。"""

    def acquire(self, blocking: bool = True) -> bool:
        """尝试取得完整 Agent 回合的所有权。"""
        ...

    def release(self) -> None:
        """释放完整 Agent 回合的所有权。"""
        ...


def _validate_poll_interval(interval_seconds: float) -> float:
    """拒绝会造成忙循环或无法确定等待时间的轮询间隔。"""
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or interval_seconds <= 0
    ):
        raise ValueError("Lead Inbox 轮询间隔必须是有限的正数秒")
    return float(interval_seconds)


def _validate_join_timeout(timeout: float | None) -> float | None:
    """规范化线程等待上限；None 表示由调用方明确选择无限等待。"""
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("Lead Inbox Runner join timeout 必须是有限的非负数或 None")
    return float(timeout)


def _notify_runner_error(
    callback: TeamInboxErrorCallback,
    stage: TeamInboxRunnerStage,
    error: Exception,
) -> None:
    """尽力报告单次错误；错误观察回调自身失败不能杀死轮询线程。"""
    try:
        callback(stage, error)
    except Exception:
        pass


class LeadInboxProcessor:
    """轮询 Lead 文件邮箱，并只在取得共享 Agent 锁后消费消息。"""

    def __init__(
        self,
        bus: MessageBus,
        agent_lock: TeamAgentTurnLock,
        delivery: TeamInboxDelivery,
        *,
        poll_interval_seconds: float = TEAM_INBOX_POLL_INTERVAL_SECONDS,
        error_callback: TeamInboxErrorCallback,
    ) -> None:
        """保存依赖但不检查邮箱、不获取 Agent 锁，也不自动启动线程。"""
        if not isinstance(bus, MessageBus):
            raise TypeError("LeadInboxProcessor bus 必须实现 MessageBus 协议")
        if not isinstance(agent_lock, TeamAgentTurnLock):
            raise TypeError(
                "LeadInboxProcessor agent_lock 必须支持 acquire/release"
            )
        if not callable(delivery):
            raise TypeError("LeadInboxProcessor delivery 必须是可调用对象")
        if not callable(error_callback):
            raise TypeError("LeadInboxProcessor error_callback 必须是可调用对象")

        self._bus = bus  # 提供非破坏性就绪提示和持锁原子消费的消息总线
        self._agent_lock = agent_lock  # 与用户回合及 Cron 回合共享的串行化锁
        self._delivery = delivery  # 在锁内把非空消息批次交给 Lead Agent
        self._poll_interval_seconds = _validate_poll_interval(
            poll_interval_seconds
        )  # 两次邮箱就绪检查之间的可中断等待
        self._error_callback = error_callback  # 单次后台错误的统一观察出口
        self._stop_event = Event()  # 可立即唤醒等待的幂等软停止信号
        self._lifecycle_lock = Lock()  # 保护线程引用以及一次性启动约束
        self._thread: Thread | None = None  # start 前为空的 daemon 线程引用

    def _deliver_once_if_idle(self) -> None:
        """若 Agent 空闲则消费并交付一个完整批次；未取得锁时绝不消费。"""
        if self._stop_event.is_set():
            return
        if not self._agent_lock.acquire(blocking=False):
            return
        try:
            messages = self._bus.consume(TEAM_LEAD_NAME)
            if not isinstance(messages, list):
                raise TypeError("MessageBus.consume 必须返回 TeamMessage 列表")
            if messages:
                self._delivery(messages)
        finally:
            self._agent_lock.release()

    def _poll_once(self) -> None:
        """执行一次就绪检查，并将轮询与交付错误分别报告后隔离。"""
        try:
            has_pending = self._bus.has_pending(TEAM_LEAD_NAME)
        except Exception as error:
            _notify_runner_error(
                self._error_callback,
                "TeamInboxPoller",
                error,
            )
            return
        if type(has_pending) is not bool:
            _notify_runner_error(
                self._error_callback,
                "TeamInboxPoller",
                TypeError("MessageBus.has_pending 必须返回 bool"),
            )
            return
        if not has_pending:
            return

        try:
            self._deliver_once_if_idle()
        except Exception as error:
            _notify_runner_error(
                self._error_callback,
                "TeamInboxDelivery",
                error,
            )

    def _run_loop(self) -> None:
        """立即检查一次邮箱，随后以可中断等待持续轮询。"""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as error:
                # 防守未知实现错误，避免单次异常静默杀死后台线程。
                _notify_runner_error(
                    self._error_callback,
                    "TeamInboxPoller",
                    error,
                )
            if self._stop_event.wait(self._poll_interval_seconds):
                break

    def start(self) -> None:
        """显式启动一次 daemon 线程；停止后的 Processor 不允许重新启动。"""
        with self._lifecycle_lock:
            if self._thread is not None:
                raise RuntimeError("LeadInboxProcessor 不能重复启动")
            if self._stop_event.is_set():
                raise RuntimeError("LeadInboxProcessor 已请求停止，不能再启动")
            thread = Thread(
                target=self._run_loop,
                name="mini-cc-team-inbox-processor",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                raise

    def request_stop(self) -> None:
        """幂等请求软停止；已经开始的 Agent 回合不会被强制中断。"""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> bool:
        """等待线程结束，并返回它是否已在给定时限内停止。"""
        normalized_timeout = _validate_join_timeout(timeout)
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is current_thread():
            raise RuntimeError("LeadInboxProcessor 不能等待自身线程结束")
        thread.join(normalized_timeout)
        return not thread.is_alive()

    def is_running(self) -> bool:
        """线程安全地报告后台 Processor 当前是否仍存活。"""
        with self._lifecycle_lock:
            thread = self._thread
        return thread is not None and thread.is_alive()
