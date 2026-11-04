"""团队消息领域模型：定义 Lead 与 Teammate 通信时的稳定数据边界。"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

from ..config import (
    MAX_TASKS,
    TASK_ID_PATTERN,
    MAX_TEAMMATE_ERROR_LENGTH,
    MAX_TEAMMATE_PROMPT_LENGTH,
    MAX_TEAMMATE_RESULT_LENGTH,
    MAX_TEAMMATE_ROLE_LENGTH,
    MAX_TEAM_MEMBER_NAME_LENGTH,
    MAX_TEAM_MESSAGE_CONTENT_LENGTH,
    TEAM_LEAD_NAME,
    TEAM_MEMBER_NAME_PATTERN,
    TEAM_MEMBER_RESERVED_NAMES,
    TEAM_MESSAGE_ID_PATTERN,
    TEAMMATE_INSTANCE_ID_PATTERN,
    TEAM_REQUEST_ID_PATTERN,
    TEAM_REQUEST_MESSAGE_TYPES,
    TEAM_RESPONSE_MESSAGE_TYPES,
    VALID_PROTOCOL_DECISIONS,
    VALID_TEAM_MESSAGE_TYPES,
    VALID_TEAMMATE_STATUSES,
    ProtocolDecision,
    TeamMessageType,
    TeammateStatus,
)


def validate_team_member_name(
    name: object,
    *,
    field_name: str = "团队成员名称",
) -> str:
    """校验逻辑成员名，使其能够无歧义地映射为跨平台邮箱名称。"""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{field_name} 必须是非空字符串")
    if len(name) > MAX_TEAM_MEMBER_NAME_LENGTH:
        raise ValueError(
            f"{field_name} 不能超过 {MAX_TEAM_MEMBER_NAME_LENGTH} 个字符"
        )
    if TEAM_MEMBER_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(
            f"{field_name} 必须使用小写 kebab-case，且以英文字母开头"
        )
    if name in TEAM_MEMBER_RESERVED_NAMES:
        raise ValueError(f"{field_name} 不能使用保留设备名称 {name!r}")
    return name


def _normalize_teammate_timestamp(
    field_name: str,
    value: float | None,
    *,
    optional: bool,
) -> float | None:
    """校验 Teammate 生命周期时间戳并统一转换为浮点数。"""
    if value is None:
        if optional:
            return None
        raise ValueError(f"Teammate {field_name} 不能为空")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        expected = "有限的非负数或 None" if optional else "有限的非负数"
        raise ValueError(f"Teammate {field_name} 必须是{expected}")
    return float(value)


@dataclass(frozen=True)
class TeamMessage:
    """一条不可变的团队通信消息；投递状态由 MessageBus 管理。"""

    message_id: str  # Harness 生成的稳定消息 ID，用于审计和去重
    sender: str  # 发送方成员名称，例如 lead 或 researcher
    recipient: str  # 接收方成员名称，对应一个逻辑收件箱
    content: str  # 发送给接收方的自然语言正文或结构化文本
    created_at: float  # 使用 Unix 时间戳记录的消息创建时间
    message_type: TeamMessageType = "message"  # 消息用途，默认普通协作消息
    request_id: str | None = None  # 同一次协议请求与响应共享的关联 ID
    decision: ProtocolDecision | None = None  # 仅协议响应携带批准或拒绝决定

    def __post_init__(self) -> None:
        """校验消息基础字段与协议字段组合；请求匹配由后续协议注册表负责。"""
        if (
            not isinstance(self.message_id, str)
            or TEAM_MESSAGE_ID_PATTERN.fullmatch(self.message_id) is None
        ):
            raise ValueError("TeamMessage ID 必须符合 msg_<32 位小写十六进制> 格式")

        validate_team_member_name(self.sender, field_name="TeamMessage sender")
        validate_team_member_name(self.recipient, field_name="TeamMessage recipient")

        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("TeamMessage content 必须是非空字符串")
        if len(self.content) > MAX_TEAM_MESSAGE_CONTENT_LENGTH:
            raise ValueError(
                "TeamMessage content 不能超过 "
                f"{MAX_TEAM_MESSAGE_CONTENT_LENGTH} 个字符"
            )

        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(self.created_at)
            or self.created_at < 0
        ):
            raise ValueError("TeamMessage created_at 必须是有限的非负 Unix 时间戳")
        object.__setattr__(self, "created_at", float(self.created_at))

        # Literal 不执行运行时检查；先确认是字符串，避免列表等输入在集合查询时报错。
        if (
            not isinstance(self.message_type, str)
            or self.message_type not in VALID_TEAM_MESSAGE_TYPES
        ):
            raise ValueError(f"TeamMessage message_type 非法：{self.message_type!r}")

        if self.message_type in ("message", "result"):
            if self.request_id is not None or self.decision is not None:
                raise ValueError("普通消息和结果消息不能携带 request_id 或 decision")
            return

        # 请求和响应都有关联 ID，但这里不判断该 ID 是否已登记、回复者是否有权限。
        if (
            not isinstance(self.request_id, str)
            or TEAM_REQUEST_ID_PATTERN.fullmatch(self.request_id) is None
        ):
            raise ValueError("协议消息 request_id 必须符合 req_<32 位小写十六进制> 格式")

        if self.message_type in TEAM_REQUEST_MESSAGE_TYPES:
            if self.decision is not None:
                raise ValueError("协议请求不能提前携带 decision")
        elif self.message_type in TEAM_RESPONSE_MESSAGE_TYPES:
            if (
                not isinstance(self.decision, str)
                or self.decision not in VALID_PROTOCOL_DECISIONS
            ):
                raise ValueError("协议响应 decision 必须是 approved 或 rejected")


@dataclass(frozen=True)
class TeammateSpec:
    """创建 Teammate 时冻结的身份、职责和初始任务。"""

    name: str  # Teammate 的进程内唯一名称，也是其逻辑邮箱名称
    role: str  # Lead 为该 Teammate 指定的职责边界
    prompt: str  # Teammate 首次 Agent 运行接收的完整任务指令
    created_at: float  # 使用 Unix 时间戳记录的 Teammate 创建时间
    task_ids: tuple[str, ...] = ()  # 允许自动认领的 Task 白名单；空元组不授权任务板认领
    instance_id: str = field(default_factory=lambda: f"tm_{uuid.uuid4().hex}")  # Harness 为每个新实例生成，空闲恢复时沿用

    def __post_init__(self) -> None:
        """校验 Teammate 身份、职责、任务大小和创建时间。"""
        validate_team_member_name(self.name, field_name="Teammate name")
        if self.name == TEAM_LEAD_NAME:
            raise ValueError(f"Teammate name 不能使用 Lead 保留名称 {TEAM_LEAD_NAME!r}")
        if (
            not isinstance(self.instance_id, str)
            or TEAMMATE_INSTANCE_ID_PATTERN.fullmatch(self.instance_id) is None
        ):
            raise ValueError("Teammate instance_id 必须符合 tm_<32 位小写十六进制> 格式")

        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("Teammate role 必须是非空字符串")
        if self.role != self.role.strip():
            raise ValueError("Teammate role 不能包含首尾空白")
        if len(self.role) > MAX_TEAMMATE_ROLE_LENGTH:
            raise ValueError(
                f"Teammate role 不能超过 {MAX_TEAMMATE_ROLE_LENGTH} 个字符"
            )

        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("Teammate prompt 必须是非空字符串")
        if len(self.prompt) > MAX_TEAMMATE_PROMPT_LENGTH:
            raise ValueError(
                f"Teammate prompt 不能超过 {MAX_TEAMMATE_PROMPT_LENGTH} 个字符"
            )

        normalized_created_at = _normalize_teammate_timestamp(
            "created_at",
            self.created_at,
            optional=False,
        )
        object.__setattr__(self, "created_at", normalized_created_at)

        if not isinstance(self.task_ids, tuple):
            raise TypeError("Teammate task_ids 必须是不可变元组")
        if len(self.task_ids) > MAX_TASKS:
            raise ValueError(f"Teammate task_ids 不能超过 {MAX_TASKS} 项")
        seen: set[str] = set()
        for task_id in self.task_ids:
            if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
                raise ValueError("Teammate task_ids 必须使用合法 Task ID")
            if task_id in seen:
                raise ValueError(f"Teammate task_ids 包含重复 ID：{task_id}")
            seen.add(task_id)


@dataclass(frozen=True)
class TeammateStoppedResult:
    """Runner 在安全边界停止的结构化返回值，不是任务完成文本。"""


@dataclass(frozen=True)
class TeammateSnapshot:
    """Teammate 注册信息与当前生命周期状态的不可变快照。"""

    spec: TeammateSpec  # 不随状态迁移改变的 Teammate 创建参数
    status: TeammateStatus  # starting → running ↔ idle；completed/failed/stopped 为不可逆终态
    started_at: float | None = None  # Runner 首次开始执行的时间；从 idle 恢复时不重置
    finished_at: float | None = None  # Runner 进入终态的时间；线程完全退出仍需 join 确认
    result: str | None = None  # completed 状态下 Runner 返回的最终结果
    error: str | None = None  # failed 状态下保留的异常类型与诊断文本

    def __post_init__(self) -> None:
        """校验状态组合、时间顺序以及终态结果形状。"""
        if not isinstance(self.spec, TeammateSpec):
            raise ValueError("TeammateSnapshot spec 必须是 TeammateSpec")
        if self.status not in VALID_TEAMMATE_STATUSES:
            raise ValueError(f"未知的 Teammate 状态 {self.status!r}")

        normalized_started_at = _normalize_teammate_timestamp(
            "started_at",
            self.started_at,
            optional=True,
        )
        normalized_finished_at = _normalize_teammate_timestamp(
            "finished_at",
            self.finished_at,
            optional=True,
        )
        object.__setattr__(self, "started_at", normalized_started_at)
        object.__setattr__(self, "finished_at", normalized_finished_at)

        if (
            normalized_started_at is not None
            and normalized_started_at < self.spec.created_at
        ):
            raise ValueError("Teammate started_at 不能早于 created_at")
        if (
            normalized_finished_at is not None
            and normalized_finished_at < self.spec.created_at
        ):
            raise ValueError("Teammate finished_at 不能早于 created_at")
        if (
            normalized_started_at is not None
            and normalized_finished_at is not None
            and normalized_finished_at < normalized_started_at
        ):
            raise ValueError("Teammate finished_at 不能早于 started_at")

        if self.result is not None and not isinstance(self.result, str):
            raise ValueError("Teammate result 必须是字符串或 None")
        if isinstance(self.result, str) and len(self.result) > MAX_TEAMMATE_RESULT_LENGTH:
            raise ValueError(
                f"Teammate result 不能超过 {MAX_TEAMMATE_RESULT_LENGTH} 个字符"
            )
        if self.error is not None:
            if not isinstance(self.error, str) or not self.error.strip():
                raise ValueError("Teammate error 必须是非空字符串或 None")
            if len(self.error) > MAX_TEAMMATE_ERROR_LENGTH:
                raise ValueError(
                    f"Teammate error 不能超过 {MAX_TEAMMATE_ERROR_LENGTH} 个字符"
                )

        if self.status == "starting":
            if any(
                value is not None
                for value in (
                    self.started_at,
                    self.finished_at,
                    self.result,
                    self.error,
                )
            ):
                raise ValueError("starting Teammate 不能包含运行或终态字段")
            return

        # idle 只是暂时没有工作，Runner 尚未结束，不能携带终态结果。
        if self.status in ("running", "idle"):
            if self.started_at is None:
                raise ValueError(f"{self.status} Teammate 必须具有 started_at")
            if any(
                value is not None
                for value in (self.finished_at, self.result, self.error)
            ):
                raise ValueError(f"{self.status} Teammate 不能提前包含终态字段")
            return

        if self.finished_at is None:
            raise ValueError(f"{self.status} Teammate 必须具有 finished_at")
        if self.status == "stopped":
            if self.started_at is None or self.result is not None or self.error is not None:
                raise ValueError("stopped 必须已开始运行，且不能包含完成结果或失败错误")
            return
        if self.status == "completed":
            if self.started_at is None:
                raise ValueError("completed Teammate 必须具有 started_at")
            if not isinstance(self.result, str) or not self.result.strip():
                raise ValueError("completed Teammate 必须具有非空字符串 result")
            if self.error is not None:
                raise ValueError("completed Teammate 的 error 必须为 None")
            return

        if self.result is not None:
            raise ValueError("failed Teammate 的 result 必须为 None")
        if self.error is None:
            raise ValueError("failed Teammate 必须具有 error")


def generate_team_message_id() -> str:
    """生成符合稳定磁盘格式的随机团队消息 ID。"""
    message_id = f"msg_{uuid.uuid4().hex}"
    if TEAM_MESSAGE_ID_PATTERN.fullmatch(message_id) is None:
        raise RuntimeError("Harness 生成了格式无效的 TeamMessage ID")
    return message_id


def team_message_to_record(message: TeamMessage) -> dict[str, object]:
    """统一输出完整八字段记录；普通消息的空协议字段序列化为 JSON null。"""
    if not isinstance(message, TeamMessage):
        raise TypeError("只能序列化 TeamMessage 实例")
    return {
        "messageId": message.message_id,
        "sender": message.sender,
        "recipient": message.recipient,
        "content": message.content,
        "createdAt": message.created_at,
        "messageType": message.message_type,
        "requestId": message.request_id,
        "decision": message.decision,
    }


def team_message_from_record(record: object) -> TeamMessage:
    """接受完整旧五字段或新八字段记录，拒绝残缺格式和未知字段。"""
    if not isinstance(record, dict):
        raise ValueError("TeamMessage 记录必须是 JSON 对象")

    legacy_fields = {
        "messageId",
        "sender",
        "recipient",
        "content",
        "createdAt",
    }
    current_fields = legacy_fields | {"messageType", "requestId", "decision"}
    actual_fields = set(record)
    message_type: TeamMessageType
    request_id: str | None
    decision: ProtocolDecision | None
    if actual_fields == legacy_fields:
        # L15 没有保存消息用途，旧记录只能恢复为普通消息，不能猜测其协议含义。
        message_type = "message"
        request_id = None
        decision = None
    elif actual_fields == current_fields:
        # 不使用逐字段 get 默认值；新格式必须完整，再由领域模型校验值及组合。
        message_type = record["messageType"]
        request_id = record["requestId"]
        decision = record["decision"]
    else:
        unknown_fields = actual_fields - current_fields
        if unknown_fields:
            formatted_fields = ", ".join(
                sorted(repr(field) for field in unknown_fields)
            )
            raise ValueError(f"TeamMessage 记录包含未知字段：{formatted_fields}")
        # 出现任意一个新字段，就必须满足完整新格式；否则按旧格式报告缺失。
        expected_fields = (
            current_fields
            if actual_fields & (current_fields - legacy_fields)
            else legacy_fields
        )
        formatted_fields = ", ".join(sorted(expected_fields - actual_fields))
        raise ValueError(f"TeamMessage 记录缺少字段：{formatted_fields}")

    message_id = record["messageId"]
    sender = record["sender"]
    recipient = record["recipient"]
    content = record["content"]
    created_at = record["createdAt"]

    if not isinstance(message_id, str):
        raise ValueError("TeamMessage 记录的 messageId 必须是字符串")
    if not isinstance(sender, str):
        raise ValueError("TeamMessage 记录的 sender 必须是字符串")
    if not isinstance(recipient, str):
        raise ValueError("TeamMessage 记录的 recipient 必须是字符串")
    if not isinstance(content, str):
        raise ValueError("TeamMessage 记录的 content 必须是字符串")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
    ):
        raise ValueError("TeamMessage 记录的 createdAt 必须是数字")

    return TeamMessage(
        message_id=message_id,
        sender=sender,
        recipient=recipient,
        content=content,
        created_at=float(created_at),
        message_type=message_type,
        request_id=request_id,
        decision=decision,
    )
