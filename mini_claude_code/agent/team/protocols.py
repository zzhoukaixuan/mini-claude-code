"""团队协议请求与注册表：校验不可变快照，在锁内匹配响应并提交状态迁移。"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Lock

from ..config import (
    MAX_PROTOCOL_PAYLOAD_LENGTH,
    MAX_PROTOCOL_RECORDS,
    TEAM_LEAD_NAME,
    TEAM_REQUEST_ID_PATTERN,
    TEAM_RESPONSE_MESSAGE_TYPES,
    VALID_PROTOCOL_STATUSES,
    VALID_PROTOCOL_TYPES,
    ProtocolDecision,
    ProtocolStatus,
    ProtocolType,
    TeamMessageType,
)
from .bus import MessageBus
from .models import TeamMessage, generate_team_message_id, validate_team_member_name


def generate_protocol_request_id() -> str:
    """由 Harness 生成协议关联 ID；同一次请求及其响应复用此 ID。"""
    request_id = f"req_{uuid.uuid4().hex}"
    if TEAM_REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise RuntimeError("Harness 生成了格式无效的协议请求 ID")
    return request_id


def _normalize_protocol_timestamp(value: object, *, field_name: str) -> float:
    """将必填时间戳规范为 float，拒绝布尔值、非数值、非有限值和负数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"ProtocolRequest {field_name} 必须是有限的非负时间戳")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise ValueError(f"ProtocolRequest {field_name} 超出可表示范围") from error
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"ProtocolRequest {field_name} 必须是有限的非负时间戳")
    return normalized


@dataclass(frozen=True)
class ProtocolRequest:
    """一次请求的不可变快照；状态迁移和响应身份匹配由后续注册表负责。"""

    request_id: str  # 请求与响应共享的关联 ID，与每封邮件的 message_id 区分
    protocol_type: ProtocolType  # shutdown 关闭协议或 plan_approval 计划审批协议
    sender: str  # 请求发起方：关闭请求为 Lead，计划审批请求为 Teammate
    target: str  # 有权回应请求的成员，响应时应成为消息发送方
    payload: str  # 关闭原因或待审批计划，保留原文供后续核验
    created_at: float  # 请求创建时的非负 Unix 时间戳
    status: ProtocolStatus = "pending"  # 等待决定或已批准/拒绝的请求状态
    resolved_at: float | None = None  # 收到有效决定的时间；pending 时必须为空

    def __post_init__(self) -> None:
        """校验单条快照的格式、协议方向、正文边界和状态时间组合。"""
        if (
            not isinstance(self.request_id, str)
            or TEAM_REQUEST_ID_PATTERN.fullmatch(self.request_id) is None
        ):
            raise ValueError("ProtocolRequest request_id 必须符合 req_<32 位小写十六进制> 格式")
        if (
            not isinstance(self.protocol_type, str)
            or self.protocol_type not in VALID_PROTOCOL_TYPES
        ):
            raise ValueError(f"ProtocolRequest protocol_type 非法：{self.protocol_type!r}")
        if not isinstance(self.status, str) or self.status not in VALID_PROTOCOL_STATUSES:
            raise ValueError(f"ProtocolRequest status 非法：{self.status!r}")

        validate_team_member_name(self.sender, field_name="ProtocolRequest sender")
        validate_team_member_name(self.target, field_name="ProtocolRequest target")
        if self.sender == self.target:
            raise ValueError("协议请求的 sender 和 target 不能相同")

        # 名称合法不代表成员已注册；这里只约束协议方向，成员存活性由运行时检查。
        if self.protocol_type == "shutdown":
            if self.sender != TEAM_LEAD_NAME or self.target == TEAM_LEAD_NAME:
                raise ValueError("关闭请求必须由 Lead 发给 Teammate")
        elif self.protocol_type == "plan_approval":
            if self.sender == TEAM_LEAD_NAME or self.target != TEAM_LEAD_NAME:
                raise ValueError("计划审批请求必须由 Teammate 发给 Lead")

        if not isinstance(self.payload, str) or not self.payload.strip():
            raise ValueError("ProtocolRequest payload 必须是非空字符串")
        if len(self.payload) > MAX_PROTOCOL_PAYLOAD_LENGTH:
            raise ValueError(
                f"ProtocolRequest payload 不能超过 {MAX_PROTOCOL_PAYLOAD_LENGTH} 个字符"
            )

        created_at = _normalize_protocol_timestamp(self.created_at, field_name="created_at")
        object.__setattr__(self, "created_at", created_at)
        if self.status == "pending":
            if self.resolved_at is not None:
                raise ValueError("pending 请求不能提前具有 resolved_at")
            return

        # 批准/拒绝是请求终态；确认关闭不等于线程已退出，后者由成员生命周期记录。
        resolved_at = _normalize_protocol_timestamp(self.resolved_at, field_name="resolved_at")
        if resolved_at < created_at:
            raise ValueError("ProtocolRequest resolved_at 不能早于 created_at")
        object.__setattr__(self, "resolved_at", resolved_at)


class ProtocolRequestNotFoundError(LookupError):
    """请求 ID 不在当前进程的协议注册表中。"""


class DuplicateProtocolRequestError(ValueError):
    """请求 ID 已被登记，不能覆盖已有状态。"""


class InvalidProtocolResponseError(ValueError):
    """响应类型、通信双方或请求当前状态不允许此次迁移。"""


class ProtocolRegistry:
    """保存进程内请求快照；文件邮箱持久化不代表该注册表可以跨进程恢复。"""

    def __init__(self, *, max_records: int = MAX_PROTOCOL_RECORDS) -> None:
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
            raise ValueError("ProtocolRegistry max_records 必须是正整数")
        self._max_records = max_records  # 已结束请求仍占用容量，保留决定且防止 ID 被重用
        self._requests: dict[str, ProtocolRequest] = {}  # 按关联 ID 保存最新的不可变请求快照
        self._lock = Lock()  # 保护查找、状态校验和提交整个事务，避免两个响应同时生效

    def _require_request_locked(self, request_id: str) -> ProtocolRequest:
        """取得请求快照；调用方必须持锁，本方法不重复获取非重入锁。"""
        if (
            not isinstance(request_id, str)
            or TEAM_REQUEST_ID_PATTERN.fullmatch(request_id) is None
        ):
            raise ValueError("协议请求 ID 必须符合 req_<32 位小写十六进制> 格式")
        request = self._requests.get(request_id)
        if request is None:
            raise ProtocolRequestNotFoundError(f"协议请求 {request_id!r} 未登记")
        return request

    def _register_locked(self, request: ProtocolRequest) -> ProtocolRequest:
        """检查并登记请求；调用方必须持锁，本方法不重复获取锁。"""
        if not isinstance(request, ProtocolRequest):
            raise TypeError("ProtocolRegistry 只能登记 ProtocolRequest")
        if request.status != "pending":
            raise ValueError("只能登记 pending 协议请求，终态必须由有效响应产生")
        if request.request_id in self._requests:
            raise DuplicateProtocolRequestError(
                f"协议请求 {request.request_id!r} 已登记，不能重复注册"
            )
        if len(self._requests) >= self._max_records:
            raise ValueError(f"协议请求记录已经达到上限 {self._max_records}")
        self._requests[request.request_id] = request
        return request

    def register(self, request: ProtocolRequest) -> ProtocolRequest:
        """登记 pending 请求；重复 ID、终态快照或容量不足均不会修改注册表。"""
        with self._lock:
            return self._register_locked(request)

    def register_and_send(
        self,
        request: ProtocolRequest,
        *,
        bus: MessageBus,
    ) -> ProtocolRequest:
        """持锁登记并同步投递请求；发送抛出普通异常时撤销本次登记。

        这是进程内协调，不是磁盘与内存之间的持久事务。回滚依赖发送异常
        表示未提交的前提；提交后报错（包括文件解锁失败）或进程中断仍可能
        留下已投递邮件，不能据此宣称 exactly-once，也不在这里自动重发。
        bus.send 不得在同一线程回调本注册表，或等待需要本注册表锁的线程。
        """
        if not isinstance(request, ProtocolRequest):
            raise TypeError("ProtocolRegistry 只能登记 ProtocolRequest")
        if not isinstance(bus, MessageBus):
            raise TypeError("协议请求 bus 必须实现 MessageBus 协议")

        # 在改变注册表前构造合法邮件；信封自己的 ID 与协议关联 ID 分别生成/传递。
        message_type: TeamMessageType = (
            "shutdown_request"
            if request.protocol_type == "shutdown"
            else "plan_approval_request"
        )
        message = TeamMessage(
            message_id=generate_team_message_id(),
            sender=request.sender,
            recipient=request.target,
            content=request.payload,
            created_at=request.created_at,
            message_type=message_type,
            request_id=request.request_id,
            decision=None,
        )

        with self._lock:
            # 登记失败不进入发送及回滚分支，尤其不能删除重复 ID 对应的原记录。
            self._register_locked(request)
            try:
                # 发送期间持锁，其他线程的 resolve 会等待本次操作完成。
                bus.send(message)
            except Exception:
                del self._requests[request.request_id]
                raise  # 保留原始异常及调用栈，让上层判断如何报告失败。
            return request

    def get(self, request_id: str) -> ProtocolRequest:
        """返回不可变请求快照；未知 ID 明确报错。"""
        with self._lock:
            return self._require_request_locked(request_id)

    def list_all(self) -> list[ProtocolRequest]:
        """返回按创建时间和请求 ID 稳定排序的独立列表。"""
        with self._lock:
            return sorted(
                self._requests.values(),
                key=lambda request: (request.created_at, request.request_id),
            )

    def resolve(
        self,
        response: TeamMessage,
        *,
        resolved_at: float,
    ) -> ProtocolRequest:
        """在锁内匹配响应并提交终态；重复响应也报错，不覆盖首次决定。"""
        if not isinstance(response, TeamMessage):
            raise TypeError("协议响应必须是 TeamMessage")
        if response.message_type not in TEAM_RESPONSE_MESSAGE_TYPES:
            raise InvalidProtocolResponseError("只有协议响应消息可以结束请求")
        # TeamMessage 已校验字段组合；此处显式收窄可空字段，便于类型检查器识别。
        if response.request_id is None or response.decision is None:
            raise InvalidProtocolResponseError("协议响应缺少 request_id 或 decision")

        with self._lock:
            request = self._require_request_locked(response.request_id)
            expected_type = f"{request.protocol_type}_response"
            if response.message_type != expected_type:
                raise InvalidProtocolResponseError(
                    f"请求 {request.request_id!r} 需要 {expected_type}，"
                    f"不能由 {response.message_type} 响应"
                )
            if response.sender != request.target or response.recipient != request.sender:
                raise InvalidProtocolResponseError(
                    f"请求 {request.request_id!r} 的响应必须由 "
                    f"{request.target!r} 发给 {request.sender!r}"
                )
            if request.status != "pending":
                raise InvalidProtocolResponseError(
                    f"请求 {request.request_id!r} 已处于 {request.status!r}，不能再次决定"
                )

            # replace 会重新执行 __post_init__；时间无效时抛错，字典仍保留旧快照。
            updated = replace(request, status=response.decision, resolved_at=resolved_at)
            self._requests[request.request_id] = updated
            return updated


def send_protocol_response(
    request_id: str,
    decision: ProtocolDecision,
    reason: str,
    *,
    responder: str,
    registry: ProtocolRegistry,
    bus: MessageBus,
    clock: Callable[[], float] = time.time,
) -> TeamMessage:
    """由请求指定的接收方发送批准/拒绝响应，不提前修改注册表状态。

    responder 必须由 Harness 绑定，不能作为模型可填写的工具参数。
    返回只表示 bus.send 正常完成，不代表响应已被消费或队友线程已退出。
    此处检查的是请求快照，检查与发送不是整体事务；并发发送或消费可能
    产生重复/过期响应，最终由接收端 resolve 在锁内裁决，不覆盖首次决定。
    发送异常原样上抛，不自动重试；提交后报错时不能断言邮件尚未投递。
    """
    if not isinstance(registry, ProtocolRegistry):
        raise TypeError("协议回复 registry 必须是 ProtocolRegistry")
    if not isinstance(bus, MessageBus):
        raise TypeError("协议回复 bus 必须实现 MessageBus 协议")
    if not callable(clock):
        raise TypeError("协议回复 clock 必须可调用")
    validate_team_member_name(responder, field_name="协议回复 responder")

    request = registry.get(request_id)
    if request.status != "pending":
        raise InvalidProtocolResponseError(
            f"请求 {request.request_id!r} 已处于 {request.status!r}，不能再次回复"
        )
    if responder != request.target:
        raise InvalidProtocolResponseError(
            f"请求 {request.request_id!r} 只能由 {request.target!r} 回复，"
            f"不能由 {responder!r} 回复"
        )

    created_at = _normalize_protocol_timestamp(clock(), field_name="响应 created_at")
    if created_at < request.created_at:
        raise ValueError("协议响应 created_at 不能早于请求 created_at")
    message_type: TeamMessageType = (
        "shutdown_response"
        if request.protocol_type == "shutdown"
        else "plan_approval_response"
    )
    # TeamMessage 统一校验 decision、正文及字段组合；不重复实现其规则。
    message = TeamMessage(
        message_id=generate_team_message_id(),
        sender=responder,
        recipient=request.sender,
        content=reason,
        created_at=created_at,
        message_type=message_type,
        request_id=request.request_id,
        decision=decision,
    )
    bus.send(message)
    # 不调用 resolve：由请求发起方消费响应后再提交终态。
    return message


def route_protocol_response(
    message: TeamMessage,
    *,
    registry: ProtocolRegistry,
    resolved_at: float,
) -> ProtocolRequest | None:
    """普通消息和请求返回 None；协议响应匹配成功后返回终态请求快照。

    本函数只分流已消费的消息，不读取邮箱。None 表示本路由不处理此类消息，
    不代表丢弃消息或批准请求；调用方仍需交付普通消息或处理协议请求。
    匹配失败的异常原样上抛，由批次路由层记录并隔离，不能降级为普通消息。
    """
    if not isinstance(message, TeamMessage):
        raise TypeError("协议路由 message 必须是 TeamMessage")
    if not isinstance(registry, ProtocolRegistry):
        raise TypeError("协议路由 registry 必须是 ProtocolRegistry")
    if message.message_type not in TEAM_RESPONSE_MESSAGE_TYPES:
        return None

    # 注册表统一校验关联 ID、响应类型、双方身份、状态和完成时间，路由不重复实现。
    return registry.resolve(message, resolved_at=resolved_at)
