"""Provider 边界模块：兼容提取模型响应的文本、内容块类型和 Token 用量。"""

from __future__ import annotations


def content_has_tool_use(content: object) -> bool:
    """判断响应内容中是否存在工具调用块，兼容字典和 SDK 对象。"""
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
        else:
            block_type = getattr(block, "type", None)
        if block_type == "tool_use":
            return True
    return False


def extract_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            block_text = block.get("text")
        else:
            block_type = getattr(block, "type", None)
            block_text = getattr(block, "text", None)
        if block_type == "text" and isinstance(block_text, str):
            texts.append(block_text)
    return "\n".join(texts)


def extract_token_usage(response: object) -> tuple[int, int]:
    """从子 Agent 模型响应中提取 token 使用情况。"""
    if isinstance(response, dict):
        usage = response.get("usage")
    else:
        usage = getattr(response, "usage", None)
    if usage is None:
        raise ValueError("模型响应中缺少 usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
    else:
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        raise ValueError("usage 字段中必须包含 input_tokens 字段，且必须是非负整数")
    if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0:
        raise ValueError("usage 字段中必须包含 output_tokens 字段，且必须是非负整数")
    return input_tokens, output_tokens
