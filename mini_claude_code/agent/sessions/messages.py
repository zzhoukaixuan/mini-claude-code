"""会话消息边界：SDK 转换、脱敏、工具配对校验及中断尾部修复。

支持文本、工具交互及模型思考块，不调用模型、工具、磁盘或运行配置。
恢复结果是新列表；中断提示只描述未知状态，不证明工具未执行，也不自动续跑任务。
"""

from __future__ import annotations

from typing import cast

from ..context.serialization import redact_sensitive_data, to_json_compatible
from .config import INTERRUPTED_TOOL_RESULT, INTERRUPTED_TURN_NOTICE
from .models import SessionSnapshot


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"会话 {field} 必须是非空字符串")
    return value


def _identifier(value: object, field: str) -> str:
    value = _text(value, field)
    if any(char.isspace() for char in value):
        raise ValueError(f"会话 {field} 不能包含空白字符")
    return value


def _fields(block: dict, allowed: set[str]) -> None:
    # SDK 可带 citations=None 等可选空字段；仅忽略这些无内容字段，不丢弃有值的未知数据。
    if any(key not in allowed and value is not None for key, value in block.items()):
        raise ValueError("会话内容块包含暂不支持的非空字段")


def _normalize_block(block: object, role: str, *, result_body: bool = False) -> dict:
    if not isinstance(block, dict):
        raise ValueError("会话内容块必须是对象")
    kind = block.get("type")
    if kind == "text":
        _fields(block, {"type", "text", "citations"})
        # OpenRouter 的普通文本可带 citations=[]；没有引用时不需要回传该字段。
        # 非空引用尚未实现恢复支持，不静默丢弃。
        if block.get("citations") not in (None, []):
            raise ValueError("当前会话暂不支持非空 citations")
        return {"type": "text", "text": _text(block.get("text"), "text")}
    if result_body:
        raise ValueError("当前仅支持文本形式的工具结果内容块")
    if kind == "thinking" and role == "assistant":
        _fields(block, {"type", "thinking", "signature"})
        # 中转站可能返回空签名；保存原始值，不伪造签名或把思考转成普通文本。
        if not isinstance(block.get("thinking"), str) or not isinstance(block.get("signature"), str):
            raise ValueError("会话 thinking 和 signature 必须是字符串")
        return {"type": "thinking", "thinking": block["thinking"], "signature": block["signature"]}
    if kind == "redacted_thinking" and role == "assistant":
        _fields(block, {"type", "data"})
        return {"type": "redacted_thinking", "data": _text(block.get("data"), "redacted_thinking.data")}
    if kind == "tool_use" and role == "assistant":
        _fields(block, {"type", "id", "name", "input", "caller"})
        # direct 是当前 Harness 执行的普通工具调用；规范快照省略这一冗余来源字段。
        # 不将服务端代码执行等其他来源静默降级为本地调用。
        if block.get("caller") not in (None, {"type": "direct"}):
            raise ValueError("当前会话仅支持普通 direct 工具调用来源")
        if not isinstance(block.get("input"), dict):
            raise ValueError("会话工具 input 必须是对象")
        return {
            "type": "tool_use", "id": _identifier(block.get("id"), "tool_use.id"),
            "name": _identifier(block.get("name"), "tool_use.name"), "input": block["input"],
        }
    if kind == "tool_result" and role == "user":
        _fields(block, {"type", "tool_use_id", "content", "is_error"})
        content = block.get("content")
        if isinstance(content, list):
            content = [_normalize_block(item, role, result_body=True) for item in content]
        elif not isinstance(content, str):
            raise ValueError("会话工具结果必须包含字符串或文本列表 content")
        result = {"type": "tool_result",
                  "tool_use_id": _identifier(block.get("tool_use_id"), "tool_result.tool_use_id"),
                  "content": content}
        if "is_error" in block:
            if type(block["is_error"]) is not bool:
                raise ValueError("tool_result.is_error 必须是布尔值")
            result["is_error"] = block["is_error"]
        return result
    raise ValueError(f"当前会话不支持 {role!r} 中的内容块类型 {kind!r}")


def _normalize_messages(messages: object) -> list[dict]:
    """先深转换为独立 JSON 数据，再规范当前支持的消息块；未知能力显式失败。"""
    converted = to_json_compatible(messages)
    if not isinstance(converted, list):
        raise ValueError("会话消息必须是列表")
    normalized = []
    for message in converted:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError("会话消息必须且只能含 role 和 content")
        role, content = message["role"], message["content"]
        if role not in ("user", "assistant"):
            raise ValueError("会话消息 role 只能是 user 或 assistant")
        if isinstance(content, list):
            content = [_normalize_block(block, role) for block in content]
        else:
            content = _text(content, "content")
        normalized.append({"role": role, "content": content})
    return normalized


def _pending_tail(messages: list[dict]) -> tuple[tuple[str, ...], int | None]:
    """检查整段配对，仅返回允许修复的尾部缺口及部分结果消息位置。

    pending 非空时，下一条必须是对应的 user 工具结果批次；中间缺失、未知或重复结果拒绝。
    不要求普通 user/assistant 消息严格交替，兼容压缩提示和 Harness 文本事件。
    """
    pending: tuple[str, ...] = ()
    seen_ids: set[str] = set()
    if messages and messages[0]["role"] != "user":
        raise ValueError("会话上下文必须以 user 消息开始")
    for index, message in enumerate(messages):
        content = message["content"]
        blocks = content if isinstance(content, list) else []
        results = [block for block in blocks if block["type"] == "tool_result"]
        if pending:
            if message["role"] != "user" or not isinstance(content, list):
                raise ValueError("tool_use 后必须紧接对应的 user 工具结果批次")
            found: set[str] = set()
            text_started = False
            for block in blocks:
                if block["type"] == "text":
                    text_started = True
                    continue
                if text_started:
                    raise ValueError("工具结果必须位于 user 批次的提醒文本之前")
                result_id = block["tool_use_id"]
                if result_id not in pending or result_id in found:
                    raise ValueError("工具结果 ID 未对应当前调用，或重复返回同一结果")
                found.add(result_id)
            missing = tuple(tool_id for tool_id in pending if tool_id not in found)
            if missing:
                # 空列表可表示尚未收集结果；纯文本不能被猜测成工具结果批次。
                if index != len(messages) - 1 or (blocks and not found):
                    raise ValueError("历史中间缺失工具结果，或缺口后已有非结果消息")
                return missing, index
            pending = ()
            continue
        if results:
            raise ValueError("存在没有紧邻 tool_use 的孤立 tool_result")
        if isinstance(content, list) and not content:
            raise ValueError("非工具结果批次的 content 不能是空列表")
        calls = [block["id"] for block in blocks if block["type"] == "tool_use"]
        for tool_id in calls:
            if tool_id in seen_ids:
                raise ValueError("会话中出现重复的 tool_use ID")
            seen_ids.add(tool_id)
        pending = tuple(calls)
    return pending, None


def _check_sensitive_structure(messages: list[dict], secrets: tuple[str, ...]) -> None:
    """不改写协议标识或 JSON 键；含密钥则拒绝保存，避免脱敏引起 ID 碰撞或隐性泄漏。"""
    def check_keys(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if any(secret in key for secret in secrets):
                    raise ValueError("会话 JSON 字段名包含已知敏感值，无法安全保存")
                check_keys(item)
        elif isinstance(value, list):
            for item in value:
                check_keys(item)

    check_keys(messages)
    for message in messages:
        identifiers = [message["role"]]
        if isinstance(message["content"], list):
            for block in message["content"]:
                identifiers.append(block["type"])
                if block["type"] == "tool_use":
                    identifiers.extend((block["id"], block["name"]))
                elif block["type"] == "tool_result":
                    identifiers.append(block["tool_use_id"])
                    if isinstance(block["content"], list):
                        identifiers.extend(item["type"] for item in block["content"])
        if any(secret in value for value in identifiers for secret in secrets):
            raise ValueError("会话协议标识包含已知敏感值，拒绝改写标识或明文保存")


def make_session_messages(messages: list[dict], *, secrets: tuple[str, ...]) -> list[dict]:
    """保存侧生成独立、脱敏的消息快照；允许合法末尾缺口，不补造结果。

    调用方必须传入已知密钥；密钥收集可复用 collect_transcript_secrets。
    元信息的 turn_in_progress 必须与实际回合生命周期一起保存，不能由消息尾部猜测。
    """
    if not isinstance(secrets, tuple) or any(not isinstance(secret, str) or not secret for secret in secrets):
        raise ValueError("secrets 必须是非空字符串组成的元组，可以为空元组")
    secrets = tuple(sorted(set(secrets), key=lambda secret: (-len(secret), secret)))
    normalized = _normalize_messages(messages)
    _pending_tail(normalized)
    _check_sensitive_structure(normalized, secrets)
    # 思考块回传要求内容和签名保持一致；若脱敏会改写它，拒绝保存而非留下失效签名。
    for message in normalized:
        if message["role"] == "assistant" and isinstance(message["content"], list):
            for block in message["content"]:
                if block["type"] in ("thinking", "redacted_thinking"):
                    if redact_sensitive_data(block, secrets) != block:
                        raise ValueError("思考块包含已知敏感值，无法同时满足安全保存与原样恢复")
    redacted = redact_sensitive_data(normalized, secrets)
    # 脱敏函数保持 JSON 容器形状；标识受上述检查保护，不在内部重复做整段类型校验。
    return cast(list[dict], redacted)


def restore_session_messages(snapshot: SessionSnapshot) -> tuple[list[dict], tuple[str, ...]]:
    """恢复侧校验历史，仅修复进行中回合的尾部缺口；不修改快照、不落盘、不触发执行。"""
    if type(snapshot.turn_in_progress) is not bool:
        raise ValueError("会话 turn_in_progress 必须是布尔值")
    messages = _normalize_messages(snapshot.messages)
    missing, result_index = _pending_tail(messages)
    warnings: list[str] = []
    if missing:
        if not snapshot.turn_in_progress:
            raise ValueError("已结束的会话仍缺失工具结果，拒绝推断为可修复中断")
        unknown_results = [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": INTERRUPTED_TOOL_RESULT, "is_error": True}
            for tool_id in missing
        ]
        if result_index is None:
            messages.append({"role": "user", "content": unknown_results})
        else:
            content = messages[result_index]["content"]
            # 保留已保存的真实结果，补齐缺口后再保留原提醒文本。
            messages[result_index]["content"] = (
                [block for block in content if block["type"] == "tool_result"]
                + unknown_results + [block for block in content if block["type"] == "text"]
            )
        warnings.append(f"已为 {len(missing)} 个未确认结果的工具调用补入 Harness 错误结果；未重跑工具。")
    if snapshot.turn_in_progress:
        warnings.append("上次回合未确认正常结束；文件及后台工作状态需要核验，历史审批未恢复。")
        if messages and messages[-1]["role"] == "user":
            content = messages[-1]["content"]
            if isinstance(content, str):
                messages[-1]["content"] = content + "\n\n" + INTERRUPTED_TURN_NOTICE
            else:
                content.append({"type": "text", "text": INTERRUPTED_TURN_NOTICE})
        else:
            messages.append({"role": "user", "content": INTERRUPTED_TURN_NOTICE})
    # 返回前确认修复没有拆散配对；这不等于验证工具实际执行结果或远端 Provider 的全部约束。
    if _pending_tail(messages)[0]:
        raise ValueError("恢复后仍存在未配对的工具调用")
    return messages, tuple(warnings)
