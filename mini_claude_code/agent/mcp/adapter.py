"""MCP 执行边界适配：用 Harness 创建的 Handler 身份分类，保留文本与业务失败标志。"""

from dataclasses import dataclass
from typing import Callable

from .client import MCPClientError
from .models import MCPToolResult


@dataclass(frozen=True)
class MCPToolHandler:
    """只由注册层构造；名称前缀和服务端 annotations 都不能冒充这个本地身份。"""

    invoke: Callable[..., MCPToolResult]  # 固定连接及原始工具名的调用闭包

    def __call__(self, **arguments: object) -> MCPToolResult:
        result = self.invoke(**arguments)
        if not isinstance(result, MCPToolResult):
            raise MCPClientError("MCP 客户端返回值不符合文本结果契约")
        return result


def normalize_tool_output(output: object) -> tuple[str, bool]:
    """兼容原有字符串 Handler；不把 MCP 数据类 repr 当作模型工具结果。"""
    if isinstance(output, MCPToolResult):
        return output.content, output.is_error
    return output if isinstance(output, str) else str(output), False
