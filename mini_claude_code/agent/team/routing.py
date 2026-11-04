"""批量分流已消费的团队邮件，匹配协议响应并隔离单条错误，不负责读取邮箱。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..config import TEAM_RESPONSE_MESSAGE_TYPES
from .models import TeamMessage, validate_team_member_name
from .protocols import ProtocolRegistry, ProtocolRequest, route_protocol_response


@dataclass(frozen=True)
class ResolvedProtocolResponse:
    """保留成功匹配的原始响应及其产生的请求终态快照。"""

    message: TeamMessage  # 原始响应邮件，供后续通知和审计使用
    request: ProtocolRequest  # 本次响应提交后的不可变请求快照


@dataclass(frozen=True)
class InboxRoutingError:
    """单条邮件的路由失败信息；错误不得降级为普通消息继续交付。"""

    message_index: int  # 邮件在本批次中的位置，从 0 开始
    message_id: str | None  # 合法消息对象的 ID；输入不是消息对象时为空
    error_type: str  # 异常类型名称，便于区分未知请求、身份不匹配等失败
    reason: str  # 异常说明，供调用方记录或展示


@dataclass(frozen=True)
class InboxRoutingResult:
    """批次处理结果；三组不可变集合分别保留各自的输入顺序。"""

    deliverable_messages: tuple[TeamMessage, ...]  # 普通消息、结果及尚待处理的协议请求
    resolved_responses: tuple[ResolvedProtocolResponse, ...]  # 已成功提交状态迁移的响应
    errors: tuple[InboxRoutingError, ...]  # 已隔离的逐条错误，不自动重投


def route_inbox_messages(
    messages: list[TeamMessage],
    *,
    recipient: str,
    registry: ProtocolRegistry,
    clock: Callable[[], float] = time.time,
) -> InboxRoutingResult:
    """逐条分流已消费邮件；单条失败不阻断后续消息。

    顶层参数错误直接抛出。协议响应由注册表匹配并提交，普通消息和请求
    留给调用方处理；请求出现在 deliverable_messages 不代表已核验或批准。
    本函数不调用 consume，也不修改输入列表。批次不是整体事务：后续失败
    不撤销前面已经生效的决定，调用方不能把整批重跑当作无副作用的重试。
    """
    if not isinstance(messages, list):
        raise TypeError("邮箱路由 messages 必须是列表")
    validate_team_member_name(recipient, field_name="邮箱路由 recipient")
    if not isinstance(registry, ProtocolRegistry):
        raise TypeError("邮箱路由 registry 必须是 ProtocolRegistry")
    if not callable(clock):
        raise TypeError("邮箱路由 clock 必须可调用")

    deliverable_messages: list[TeamMessage] = []
    resolved_responses: list[ResolvedProtocolResponse] = []
    errors: list[InboxRoutingError] = []

    for index, message in enumerate(messages):
        try:
            if not isinstance(message, TeamMessage):
                raise TypeError("邮箱中的每条消息必须是 TeamMessage")
            # 必须先确认收件人，不能让错投邮件改变共享注册表中的状态。
            if message.recipient != recipient:
                raise ValueError(
                    f"邮件收件人 {message.recipient!r} 与当前收件人 {recipient!r} 不一致"
                )

            if message.message_type not in TEAM_RESPONSE_MESSAGE_TYPES:
                deliverable_messages.append(message)
                continue

            # 只有响应需要读取完成时间；时间值的合法性由请求模型统一检查。
            request = route_protocol_response(
                message, registry=registry, resolved_at=clock()
            )
            if request is None:
                raise RuntimeError("协议响应未返回终态请求快照")
            resolved_responses.append(
                ResolvedProtocolResponse(message=message, request=request)
            )
        except Exception as error:
            # 不吞掉 KeyboardInterrupt/SystemExit，也不把失败邮件交给模型当作有效响应。
            errors.append(
                InboxRoutingError(
                    message_index=index,
                    message_id=message.message_id if isinstance(message, TeamMessage) else None,
                    error_type=type(error).__name__,
                    reason=str(error),
                )
            )

    return InboxRoutingResult(
        deliverable_messages=tuple(deliverable_messages),
        resolved_responses=tuple(resolved_responses),
        errors=tuple(errors),
    )
