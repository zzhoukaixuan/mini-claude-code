"""纯消息转换与脱敏：供 Transcript 和 Session 复用，不导入运行配置或创建模型客户端。"""

from __future__ import annotations

import inspect
import math
import os
from mini_claude_code.configuration import active_secrets
from mini_claude_code.mcp_secrets import active_mcp_secrets
from dataclasses import asdict, is_dataclass

from .serialization_config import (
    MIN_TRANSCRIPT_SECRET_CHARS, SENSITIVE_ENV_NAME_PATTERN, TRANSCRIPT_REDACTION_MARKER,
)


def to_json_compatible(value: object) -> object:
    """递归地将消息值转换为可 JSON 序列化的独立数据。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON 消息不能包含 NaN 或 Infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        converted: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON 对象的键必须是字符串，实际类型为 {type(key).__name__}")
            converted[key] = to_json_compatible(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [to_json_compatible(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dump_signature = inspect.signature(model_dump)
        except (TypeError, ValueError):
            dumped_value = model_dump(mode="json") # 无法检查签名时，优先按照 Pydantic v2 接口调用。
        else:
            parameters = dump_signature.parameters
            accepts_mode = ("mode" in parameters) or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
            if accepts_mode:
                dumped_value = model_dump(mode="json")
            else:
                dumped_value = model_dump()
        if dumped_value is value:
            raise TypeError(f"{type(value).__name__}.model_dump() 不能返回对象自身")
        return to_json_compatible(dumped_value)
    if is_dataclass(value) and not isinstance(value, type):
        return to_json_compatible(asdict(value))
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict) and not callable(value):
        public_attributes = {
            key: item
            for key, item in attributes.items()
            if isinstance(key, str)
            and not key.startswith("_")
        }
        return to_json_compatible(public_attributes)
    raise TypeError(f"不支持将 {type(value).__name__} 转换为 JSON 数据")

def make_transcript_snapshot(messages: list[dict]) -> list[dict]:
    """生成不再依赖 SDK 对象的消息历史快照。"""
    converted = to_json_compatible(messages)
    if not isinstance(converted, list):
        raise TypeError("消息历史转换后必须是列表")
    snapshot: list[dict] = []
    for index, message in enumerate(converted):
        if not isinstance(message, dict):
            raise TypeError(f"第 {index} 条消息转换后必须是字典")
        snapshot.append(message)
    return snapshot

def collect_transcript_secrets() -> tuple[str, ...]:
    """收集需要从 Transcript 中移除的已知环境敏感值。"""
    collected: set[str] = set(active_secrets() + active_mcp_secrets())  # 用户级模型/MCP 凭据不在环境变量中
    for name, value in os.environ.items():
        if SENSITIVE_ENV_NAME_PATTERN.search(name) is None:
            continue
        if not isinstance(value, str):
            continue
        if len(value) < MIN_TRANSCRIPT_SECRET_CHARS:
            continue
        collected.add(value)
    return tuple(sorted(collected, key=lambda secret: (-len(secret), secret)))

def redact_sensitive_data(value: object, secrets: tuple[str, ...]) -> object:
    """递归替换 JSON 数据中的已知敏感值。"""
    for secret in secrets:
        if not isinstance(secret, str) or not secret:
            raise ValueError("敏感值必须是非空字符串")
    def redact(current: object) -> object:
        if isinstance(current, str):
            redacted = current
            for secret in secrets:
                redacted = redacted.replace(secret, TRANSCRIPT_REDACTION_MARKER)
            return redacted
        if current is None or isinstance(current, (int, float, bool)):
            return current
        if isinstance(current, list):
            return [redact(item) for item in current]
        if isinstance(current, dict):
            return {key: redact(value) for key, value in current.items()}
        raise TypeError(f"不支持对 {type(current).__name__} 类型执行 Transcript 脱敏")
    return redact(value)

def make_redacted_transcript_snapshot(messages: list[dict]) -> list[dict]:
    """生成结构完整但已移除已知敏感值的 Transcript 快照。"""
    snapshot = make_transcript_snapshot(messages)
    secrets = collect_transcript_secrets()
    redacted = redact_sensitive_data(snapshot, secrets)
    if not isinstance(redacted, list):
        raise TypeError("脱敏后的消息历史必须是列表")
    result: list[dict] = []
    for index, message in enumerate(redacted):
        if not isinstance(message, dict):
            raise TypeError(f"脱敏后的第 {index} 条消息必须是字典")
        result.append(message)
    return result
