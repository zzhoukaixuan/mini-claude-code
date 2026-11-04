"""Cron 触发队列：在线程安全的有界 FIFO 中按 CronJob ID 合并待处理事件。"""

from __future__ import annotations

from collections import deque
from threading import Lock

from ..config import MAX_PENDING_CRON_TRIGGERS
from .events import CronTrigger


class CronTriggerQueue:
    """保存尚未被 Worker 消费的触发；同一 CronJob 同时最多保留一个事件。"""

    def __init__(self, max_pending: int = MAX_PENDING_CRON_TRIGGERS) -> None:
        """创建有界队列；容量针对不同 CronJob 数量，而不是历史触发总数。"""
        if isinstance(max_pending, bool) or not isinstance(max_pending, int):
            raise TypeError("Cron 触发队列容量必须是整数")
        if max_pending <= 0:
            raise ValueError("Cron 触发队列容量必须大于 0")
        if max_pending > MAX_PENDING_CRON_TRIGGERS:
            raise ValueError(
                "Cron 触发队列容量不能超过 "
                f"{MAX_PENDING_CRON_TRIGGERS}"
            )
        self._max_pending = max_pending  # 队列允许同时等待消费的最大事件数
        self._triggers: deque[CronTrigger] = deque()  # 保持登记顺序的 FIFO
        self._pending_job_ids: set[str] = set()  # 用于按 CronJob ID 常数时间去重
        self._lock = Lock()  # 保护队列与去重集合组成的单个内存事务

    def enqueue(self, trigger: CronTrigger) -> bool:
        """登记触发；相同 CronJob 已有待处理事件时合并并返回 False。"""
        if not isinstance(trigger, CronTrigger):
            raise TypeError("Cron 触发队列只能接收 CronTrigger")
        with self._lock:
            if trigger.cron_job_id in self._pending_job_ids:
                return False
            if len(self._triggers) >= self._max_pending:
                raise OverflowError(
                    f"Cron 触发队列已达到容量上限 {self._max_pending}"
                )
            self._triggers.append(trigger)
            self._pending_job_ids.add(trigger.cron_job_id)
            return True

    def drain(self, limit: int | None = None) -> tuple[CronTrigger, ...]:
        """按 FIFO 顺序取出全部或指定数量的事件，并释放对应去重槽。"""
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError("Cron 触发队列 drain limit 必须是整数或 None")
            if limit <= 0:
                raise ValueError("Cron 触发队列 drain limit 必须大于 0")

        drained: list[CronTrigger] = []
        with self._lock:
            drain_count = len(self._triggers) if limit is None else min(
                limit,
                len(self._triggers),
            )
            for _ in range(drain_count):
                trigger = self._triggers.popleft()
                self._pending_job_ids.remove(trigger.cron_job_id)
                drained.append(trigger)
        return tuple(drained)

    def has_pending(self) -> bool:
        """线程安全地报告队列当前是否存在等待 Worker 消费的事件。"""
        with self._lock:
            return bool(self._triggers)
