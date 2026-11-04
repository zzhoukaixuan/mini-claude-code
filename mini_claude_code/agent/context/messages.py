"""消息上下文模块：识别工具消息，并执行大结果落盘、预算裁剪、Snip 与 Micro 压缩。"""

from __future__ import annotations

import hashlib

from ..config import (
    KEEP_INITIAL_MESSAGES,
    KEEP_RECENT_TOOL_RESULT_BATCHES,
    MAX_CONTEXT_MESSAGES,
    MAX_LATEST_TOOL_RESULT_CHARS,
    MICRO_COMPACT_MIN_CHARS,
    MICRO_COMPACT_PLACEHOLDER,
    PERSIST_TOOL_RESULT_MIN_CHARS,
    PERSIST_TOOL_RESULT_PREVIEW_CHARS,
    TOOL_RESULTS_DIR,
    WORKDIR,
)


def get_block_type(block: object) -> str | None:
    """获取字典或 SDK 内容块的类型。"""
    if isinstance(block, dict):
        type_block = block.get("type", None)
    else:
        type_block = getattr(block, "type", None)
    return type_block

def message_has_tool_use(message: dict) -> bool:
    """判断 assistant 消息中是否包含 tool_use。"""
    if message.get("role") == "assistant":
        if isinstance(message.get("content"), list):
            for block in message.get("content"):
                if get_block_type(block) == "tool_use":
                    return True
    return False

def message_has_tool_result(message: dict) -> bool:
    """判断 user 消息中是否包含 tool_result。"""
    if message.get("role") == "user":
        if isinstance(message.get("content"), list):
            for block in message.get("content"):
                if get_block_type(block) == "tool_result":
                    return True
    return False

def collect_tool_results(messages: list[dict]) -> list[tuple[int, int, dict]]:
    """按时间顺序收集所有字典形式的 tool_result 及其位置。"""
    results: list[tuple[int, int, dict]] = []
    for message_index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if get_block_type(block) != "tool_result":
                continue
            results.append((message_index, block_index, block))

    return results

def find_last_assistant_message_index(messages: list[dict]) -> int | None:
    """返回最近一条 assistant 消息的位置；不存在时返回 None。"""
    for message_index in range(len(messages) - 1, -1, -1):
        if messages[message_index].get("role") == "assistant":
            return message_index
    return None

# -------------------- 零 API 的三层预处理器 --------------------

def micro_compact(messages: list[dict]) -> list[dict]:
    """按完整批次压缩已消费旧结果，未消费与近期批次始终保持完整。"""
    last_assistant_index = find_last_assistant_message_index(messages)
    if last_assistant_index is None:
        return messages

    # 一条 tool_result 之后出现过 assistant 响应，才说明它已经进入过模型请求。
    # 最近 assistant 之后的新结果属于当前待回填批次，不能参与压缩。
    consumed_results = [
        result
        for result in collect_tool_results(messages)
        if result[0] < last_assistant_index
    ]
    consumed_batch_indices = list(dict.fromkeys(
        message_index
        for message_index, _, _ in consumed_results
    ))
    older_batch_indices = set(
        consumed_batch_indices[:-KEEP_RECENT_TOOL_RESULT_BATCHES]
    )
    for message_index, _, block in consumed_results:
        if message_index not in older_batch_indices:
            continue
        content = block.get("content")
        if not isinstance(content, str):
            continue
        if len(content) <= MICRO_COMPACT_MIN_CHARS:
            continue
        block["content"] = MICRO_COMPACT_PLACEHOLDER
    return messages

def persist_large_output(tool_use_id: str, output: str) -> str:
    """将单条大工具输出安全保存到工作区，并返回精简引用。"""
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        raise ValueError("工具调用 ID 必须是非空字符串")
    if not isinstance(output, str):
        raise ValueError("工具输出必须是字符串")
    if len(output) <= PERSIST_TOOL_RESULT_MIN_CHARS:
        return output
    resolved_output_dir = TOOL_RESULTS_DIR.resolve()
    if not resolved_output_dir.is_relative_to(WORKDIR):
        raise ValueError("工具调用结果目录不在工作区内")
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    file_name = hashlib.sha256(tool_use_id.encode()).hexdigest() + ".txt"
    target_path = (resolved_output_dir / file_name).resolve()
    if not target_path.is_relative_to(resolved_output_dir):
        raise ValueError("工具调用结果文件不在指定结果目录内")
    try:
        with open(target_path, "x", encoding="utf-8") as f:
            f.write(output)
    except FileExistsError:
        with open(target_path, "r", encoding="utf-8") as f_read:
            existing_output = f_read.read()
            if existing_output != output:
                raise ValueError("工具输出与已存在输出不一致")
    return (
        f"<persisted-output>\n"
        f"完整输出已保存至：{target_path.relative_to(WORKDIR).as_posix()}\n"
        f"原始字符数：{len(output)}\n"
        f"预览：\n"
        f"{output[:PERSIST_TOOL_RESULT_PREVIEW_CHARS]}\n"
        f"</persisted-output>\n"
    )

def tool_result_budget(messages: list[dict]) -> list[dict]:
    """限制最新一批工具结果占用的上下文字符数。"""
    if not messages:
        return messages
    last_message = messages[-1]
    if last_message.get("role") != "user":
        return messages
    if not isinstance(last_message.get("content"), list):
        return messages
    tool_results: list[dict] = []
    for block in last_message.get("content"):
        if not isinstance(block, dict):
            continue
        if get_block_type(block) != "tool_result":
            continue
        if not isinstance(block.get("content"), str):
            continue
        tool_results.append(block)
    total_chars = sum(len(block.get("content")) for block in tool_results)
    if total_chars <= MAX_LATEST_TOOL_RESULT_CHARS:
        return messages
    ranked_results = sorted(tool_results, key=lambda block: len(block.get("content")), reverse=True)
    for block in ranked_results:
        if total_chars <= MAX_LATEST_TOOL_RESULT_CHARS:
            break
        original_content = block["content"]
        tool_use_id = block.get("tool_use_id")
        persisted_content = persist_large_output(tool_use_id, original_content)
        if persisted_content == original_content:
            continue
        block["content"] = persisted_content
        total_chars += len(persisted_content) - len(original_content)
    return messages

def snip_compact(
    messages: list[dict],
    max_messages: int = MAX_CONTEXT_MESSAGES,
    keep_head: int = KEEP_INITIAL_MESSAGES,
) -> list[dict]:
    """裁剪历史中间消息，同时保护工具调用与结果的配对。"""
    if not isinstance(max_messages, int) or isinstance(max_messages, bool) or max_messages < 3:
        raise ValueError("max_messages 必须是大于或等于 3 的整数")
    if not isinstance(keep_head, int) or isinstance(keep_head, bool) or keep_head < 0 or keep_head > max_messages - 2:
        raise ValueError("keep_head 必须是 0 到 max_messages - 2 之间的整数")
    if len(messages) <= max_messages:
        return messages
    head_end = keep_head
    if head_end > 0 and head_end < len(messages) and message_has_tool_use(messages[head_end - 1]) and message_has_tool_result(messages[head_end]):
        head_end += 1
    remaining_slots = max_messages - head_end - 1 # 为裁剪占位消息预留一个位置。
    tail_start = len(messages)
    # 从尾部向前按“原子消息组”选择消息。
    while tail_start > head_end and remaining_slots > 0:
        group_start = tail_start - 1
        # user(tool_result) 与前面的 assistant(tool_use)
        # 必须作为一个整体进入尾部。
        if group_start > head_end and message_has_tool_result(messages[group_start]) and message_has_tool_use(messages[group_start - 1]):
            group_start -= 1
        group_size = tail_start - group_start
        if group_size > remaining_slots:
            break
        tail_start = group_start
        remaining_slots -= group_size
    snipped_count = tail_start - head_end
    if snipped_count <= 0:
        return messages
    placeholder_content = f"[中间 {snipped_count} 条较早消息已从活跃上下文中裁剪。]"
    placeholder = {"role": "user", "content": placeholder_content}
    return messages[:head_end] + [placeholder] + messages[tail_start:]
