"""会话快照模型与磁盘格式解析：校验集中在记录边界，不在内部逐层重复检查。

messages 这里只检查基本 JSON 消息形态；不是 Anthropic 协议校验器，不能直接用于恢复执行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .config import SESSION_ID_PATTERN, SESSION_SCHEMA_VERSION, SESSION_TITLE_MAX_CHARS


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str  # 稳定会话 ID，恢复时保持不变
    workspace: str  # 创建会话时规范化的绝对工作目录，不是 Git 仓库根
    title: str  # 展示用单行标题，不作为文件名或查找主键
    created_at: float  # 创建时间，Unix 秒
    updated_at: float  # 最近一次成功保存时间，Unix 秒
    revision: int  # 每次保存递增，用于拒绝过期快照覆盖
    turn_in_progress: bool  # 已开始但尚未确认收尾的回合；不表示应该自动重放
    messages: list[dict]  # 已转为 JSON 的有效上下文快照；后续由恢复层核验消息配对


def validate_session_id(session_id: str) -> str:
    """拒绝非法 ID 和路径输入；合法 ID 原样返回。"""
    if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError("会话 ID 必须为 session_ 加 32 位小写十六进制字符")
    return session_id


def parse_session_record(record: object) -> SessionSnapshot:
    """集中校验来自磁盘或即将写入的完整记录，返回独立解析出的快照。"""
    fields = {"schema_version", "session_id", "workspace", "title", "created_at",
              "updated_at", "revision", "turn_in_progress", "messages"}
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError("会话记录字段缺失或含未知字段")
    if type(record["schema_version"]) is not int or record["schema_version"] != SESSION_SCHEMA_VERSION:
        raise ValueError("不支持的会话格式版本")
    validate_session_id(record["session_id"])
    workspace = record["workspace"]
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("会话工作目录必须是绝对路径")
    title = record["title"]
    if (not isinstance(title, str) or not 1 <= len(title) <= SESSION_TITLE_MAX_CHARS
            or title != title.strip() or any(ord(char) < 32 or char in "\x7f\u2028\u2029" for char in title)):
        raise ValueError("会话标题必须是长度受限、无控制字符且没有首尾空白的单行文本")
    for field in ("created_at", "updated_at"):
        value = record[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"会话 {field} 必须是非负有限时间戳")
    if record["updated_at"] < record["created_at"]:
        raise ValueError("会话更新时间不能早于创建时间")
    if type(record["revision"]) is not int or record["revision"] < 0:
        raise ValueError("会话 revision 必须是非负整数")
    if type(record["turn_in_progress"]) is not bool:
        raise ValueError("会话 turn_in_progress 必须是布尔值")
    messages = record["messages"]
    if not isinstance(messages, list):
        raise ValueError("会话 messages 必须是列表")
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in ("user", "assistant")
                or not isinstance(message["content"], (str, list))):
            raise ValueError("会话消息必须包含 user/assistant role 及字符串或列表 content")
    return SessionSnapshot(**{key: value for key, value in record.items() if key != "schema_version"})
