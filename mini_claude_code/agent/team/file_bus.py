"""文件消息总线：以独立收件箱锁和原子 JSONL 快照实现团队消息传输。"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import (
    MAX_TEAM_MAILBOX_BYTES,
    MAX_TEAM_MAILBOX_MESSAGES,
    TEAM_MAILBOX_DIR,
    TEAM_MAILBOX_LOCK_POLL_INTERVAL_SECONDS,
    TEAM_MAILBOX_LOCK_TIMEOUT_SECONDS,
    WORKDIR,
)
from ..storage.atomic import atomic_replace_text
from ..storage.locking import exclusive_file_lock
from ..storage.paths import is_path_within_root
from .models import (
    TeamMessage,
    team_message_from_record,
    team_message_to_record,
    validate_team_member_name,
)


def resolve_mailbox_directory(mailbox_dir: Path = TEAM_MAILBOX_DIR) -> Path:
    """校验邮箱根目录及工作区边界，但不在导入或构造阶段创建目录。"""
    if not isinstance(mailbox_dir, Path):
        raise TypeError("团队邮箱目录必须是 pathlib.Path 类型")
    if mailbox_dir.is_symlink():
        raise ValueError(f"团队邮箱目录 {mailbox_dir} 不能是符号链接")

    resolved_workdir = WORKDIR.resolve()
    resolved_directory = mailbox_dir.resolve()
    if not is_path_within_root(resolved_directory, resolved_workdir):
        raise ValueError(
            f"团队邮箱目录 {resolved_directory} 不在工作目录 {resolved_workdir} 内"
        )
    if resolved_directory.exists() and not resolved_directory.is_dir():
        raise ValueError(f"团队邮箱路径 {resolved_directory} 不是目录")

    resolved_parent = resolved_directory.parent.resolve()
    if not is_path_within_root(resolved_parent, resolved_workdir):
        raise ValueError(
            f"团队邮箱父目录 {resolved_parent} 不在工作目录 {resolved_workdir} 内"
        )
    return resolved_parent / resolved_directory.name


def mailbox_path(mailbox_dir: Path, recipient: str) -> Path:
    """返回接收方 JSONL 邮箱路径，并拒绝符号链接和越界目标。"""
    resolved_directory = resolve_mailbox_directory(mailbox_dir)
    normalized_recipient = validate_team_member_name(
        recipient,
        field_name="邮箱接收方",
    )
    candidate = resolved_directory / f"{normalized_recipient}.jsonl"
    if candidate.is_symlink():
        raise ValueError(f"团队邮箱文件 {candidate} 不能是符号链接")

    resolved_candidate = candidate.resolve()
    if not is_path_within_root(resolved_candidate, resolved_directory):
        raise ValueError(
            f"团队邮箱文件 {resolved_candidate} 不在邮箱目录 {resolved_directory} 内"
        )
    if resolved_candidate.exists() and not resolved_candidate.is_file():
        raise ValueError(f"团队邮箱路径 {resolved_candidate} 不是普通文件")
    return resolved_candidate


def mailbox_lock_path(mailbox_dir: Path, recipient: str) -> Path:
    """返回接收方专属锁文件路径，使不同成员的邮箱可以并行工作。"""
    resolved_directory = resolve_mailbox_directory(mailbox_dir)
    normalized_recipient = validate_team_member_name(
        recipient,
        field_name="邮箱接收方",
    )
    lock_directory = resolved_directory / ".locks"
    if lock_directory.is_symlink():
        raise ValueError(f"团队邮箱锁目录 {lock_directory} 不能是符号链接")

    resolved_lock_directory = lock_directory.resolve()
    if not is_path_within_root(resolved_lock_directory, resolved_directory):
        raise ValueError(
            f"团队邮箱锁目录 {resolved_lock_directory} 不在邮箱目录内"
        )
    if resolved_lock_directory.exists() and not resolved_lock_directory.is_dir():
        raise ValueError(f"团队邮箱锁路径 {resolved_lock_directory} 不是目录")

    candidate = resolved_lock_directory / f"{normalized_recipient}.lock"
    if candidate.is_symlink():
        raise ValueError(f"团队邮箱锁文件 {candidate} 不能是符号链接")
    resolved_candidate = candidate.resolve()
    if not is_path_within_root(resolved_candidate, resolved_lock_directory):
        raise ValueError(
            f"团队邮箱锁文件 {resolved_candidate} 不在邮箱锁目录内"
        )
    if resolved_candidate.exists() and not resolved_candidate.is_file():
        raise ValueError(f"团队邮箱锁路径 {resolved_candidate} 不是普通文件")
    return resolved_candidate


def _validate_mailbox_message_batch(
    messages: list[TeamMessage],
    recipient: str,
) -> str:
    """校验一个邮箱快照的数量、类型、接收方及消息 ID 唯一性。"""
    if not isinstance(messages, list):
        raise TypeError("团队邮箱消息必须使用列表保存")
    normalized_recipient = validate_team_member_name(
        recipient,
        field_name="邮箱接收方",
    )
    if len(messages) > MAX_TEAM_MAILBOX_MESSAGES:
        raise ValueError(
            f"团队邮箱包含 {len(messages)} 条消息，"
            f"超过上限 {MAX_TEAM_MAILBOX_MESSAGES}"
        )

    seen_message_ids: set[str] = set()
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, TeamMessage):
            raise ValueError(f"团队邮箱第 {index} 条内容不是 TeamMessage")
        if message.recipient != normalized_recipient:
            raise ValueError(
                f"团队邮箱第 {index} 条消息的接收方 {message.recipient!r} "
                f"与邮箱 {normalized_recipient!r} 不一致"
            )
        if message.message_id in seen_message_ids:
            raise ValueError(
                f"团队邮箱包含重复消息 ID {message.message_id!r}"
            )
        seen_message_ids.add(message.message_id)
    return normalized_recipient


def serialize_mailbox(
    messages: list[TeamMessage],
    recipient: str,
) -> str:
    """将同一接收方的消息序列化为确定性 UTF-8 JSONL 快照。"""
    _validate_mailbox_message_batch(messages, recipient)
    serialized_lines: list[str] = []
    for message in messages:
        try:
            serialized_line = json.dumps(
                team_message_to_record(message),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"无法序列化团队消息 {message.message_id!r}：{error}"
            ) from error
        serialized_lines.append(serialized_line)

    payload = "\n".join(serialized_lines)
    if serialized_lines:
        payload += "\n"
    try:
        payload_size = len(payload.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"团队邮箱包含无法编码为 UTF-8 的文本：{error}") from error
    if payload_size > MAX_TEAM_MAILBOX_BYTES:
        raise ValueError(
            f"团队邮箱序列化后为 {payload_size} 字节，"
            f"超过上限 {MAX_TEAM_MAILBOX_BYTES} 字节"
        )
    return payload


def parse_mailbox(text: str, recipient: str) -> list[TeamMessage]:
    """完整解析一个 JSONL 邮箱；任意行失败时不返回部分消息。"""
    if not isinstance(text, str):
        raise TypeError("团队邮箱 JSONL 必须是字符串")
    normalized_recipient = validate_team_member_name(
        recipient,
        field_name="邮箱接收方",
    )
    try:
        payload_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"团队邮箱包含无法编码为 UTF-8 的文本：{error}") from error
    if payload_size > MAX_TEAM_MAILBOX_BYTES:
        raise ValueError(
            f"团队邮箱 JSONL 为 {payload_size} 字节，"
            f"超过上限 {MAX_TEAM_MAILBOX_BYTES} 字节"
        )
    if not text:
        return []

    messages: list[TeamMessage] = []
    seen_message_ids: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"团队邮箱第 {line_number} 行不是合法 JSON：{error}"
            ) from error
        try:
            message = team_message_from_record(record)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"团队邮箱第 {line_number} 行不是合法 TeamMessage：{error}"
            ) from error
        if message.recipient != normalized_recipient:
            raise ValueError(
                f"团队邮箱第 {line_number} 行消息的接收方 "
                f"{message.recipient!r} 与邮箱 {normalized_recipient!r} 不一致"
            )
        if message.message_id in seen_message_ids:
            raise ValueError(
                f"团队邮箱第 {line_number} 行包含重复消息 ID "
                f"{message.message_id!r}"
            )
        seen_message_ids.add(message.message_id)
        messages.append(message)
        if len(messages) > MAX_TEAM_MAILBOX_MESSAGES:
            raise ValueError(
                f"团队邮箱消息数量超过上限 {MAX_TEAM_MAILBOX_MESSAGES}"
            )
    return messages


def _read_mailbox_text(path: Path) -> str:
    """读取受锁保护的邮箱，并在解码前后执行资源上限检查。"""
    if path.is_symlink():
        raise ValueError(f"团队邮箱文件 {path} 不能是符号链接")
    if not path.exists():
        return ""
    if not path.is_file():
        raise ValueError(f"团队邮箱路径 {path} 不是普通文件")
    file_size = path.stat().st_size
    if file_size > MAX_TEAM_MAILBOX_BYTES:
        raise ValueError(
            f"团队邮箱文件为 {file_size} 字节，"
            f"超过上限 {MAX_TEAM_MAILBOX_BYTES} 字节"
        )

    raw_payload = path.read_bytes()
    if len(raw_payload) > MAX_TEAM_MAILBOX_BYTES:
        raise ValueError(
            f"团队邮箱读取到 {len(raw_payload)} 字节，"
            f"超过上限 {MAX_TEAM_MAILBOX_BYTES} 字节"
        )
    try:
        return raw_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"团队邮箱不是合法 UTF-8 文件：{error}") from error


class FileMailboxBus:
    """以每个接收方一份原子 JSONL 快照实现 MessageBus 契约。"""

    def __init__(self, mailbox_dir: Path = TEAM_MAILBOX_DIR) -> None:
        self._mailbox_dir = resolve_mailbox_directory(mailbox_dir)

    @property
    def mailbox_dir(self) -> Path:
        """返回已经通过工作区边界校验的邮箱根目录。"""
        return self._mailbox_dir

    def send(self, message: TeamMessage) -> None:
        """在接收方锁内校验旧快照、追加消息并原子提交新快照。"""
        if not isinstance(message, TeamMessage):
            raise TypeError("FileMailboxBus.send 只接受 TeamMessage")
        recipient = validate_team_member_name(
            message.recipient,
            field_name="TeamMessage recipient",
        )
        lock_path = mailbox_lock_path(self._mailbox_dir, recipient)
        with exclusive_file_lock(
            lock_path,
            allowed_root=self._mailbox_dir,
            timeout_seconds=TEAM_MAILBOX_LOCK_TIMEOUT_SECONDS,
            poll_interval_seconds=TEAM_MAILBOX_LOCK_POLL_INTERVAL_SECONDS,
        ):
            target_path = mailbox_path(self._mailbox_dir, recipient)
            current_messages = parse_mailbox(
                _read_mailbox_text(target_path),
                recipient,
            )
            payload = serialize_mailbox(
                [*current_messages, message],
                recipient,
            )
            atomic_replace_text(
                target_path,
                payload,
                allowed_root=self._mailbox_dir,
            )

    def consume(self, recipient: str) -> list[TeamMessage]:
        """在接收方锁内完整解析并原子清空邮箱，然后返回有序消息。"""
        normalized_recipient = validate_team_member_name(
            recipient,
            field_name="邮箱接收方",
        )
        lock_path = mailbox_lock_path(self._mailbox_dir, normalized_recipient)
        with exclusive_file_lock(
            lock_path,
            allowed_root=self._mailbox_dir,
            timeout_seconds=TEAM_MAILBOX_LOCK_TIMEOUT_SECONDS,
            poll_interval_seconds=TEAM_MAILBOX_LOCK_POLL_INTERVAL_SECONDS,
        ):
            target_path = mailbox_path(
                self._mailbox_dir,
                normalized_recipient,
            )
            if not target_path.exists():
                return []
            mailbox_text = _read_mailbox_text(target_path)
            messages = parse_mailbox(mailbox_text, normalized_recipient)
            if not mailbox_text:
                return []
            atomic_replace_text(
                target_path,
                "",
                allowed_root=self._mailbox_dir,
            )
            return messages

    def has_pending(self, recipient: str) -> bool:
        """以文件大小提供非破坏性就绪提示；真实消息仍由持锁 consume 确认。"""
        normalized_recipient = validate_team_member_name(
            recipient,
            field_name="邮箱接收方",
        )
        target_path = mailbox_path(
            self._mailbox_dir,
            normalized_recipient,
        )
        try:
            return target_path.exists() and target_path.stat().st_size > 0
        except FileNotFoundError:
            # exists 与 stat 之间若被外部进程移除，只代表本次提示已经过期。
            return False
