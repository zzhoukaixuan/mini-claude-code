"""团队消息总线接口：隔离 Agent 协作语义与具体邮箱存储方案。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import TeamMessage


@runtime_checkable
class MessageBus(Protocol):
    """Lead 与 Teammate 使用的最小消息传输契约。"""

    def send(self, message: TeamMessage) -> None:
        """持久提交一条消息；正常返回后，该消息必须可被接收方消费。"""
        ...

    def consume(self, recipient: str) -> list[TeamMessage]:
        """按提交顺序原子取走当前批次；校验失败时不得清空收件箱。

        该接口保证并发消费者不会重复取走同一条已提交消息，但不提供
        业务处理确认：调用返回后进程若崩溃，已经取走的消息不会自动重投。
        """
        ...

    def has_pending(self, recipient: str) -> bool:
        """非破坏性地提示邮箱是否可能有消息，不承诺随后消费仍为非空。"""
        ...
