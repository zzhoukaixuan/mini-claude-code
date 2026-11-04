"""进程内 MCP 教学替身：模拟发现和调用，不实现 JSON-RPC、网络或子进程通信。"""

from inspect import signature
from typing import Callable, Mapping, Sequence

from mini_claude_code.agent.mcp.client import MCPClientClosedError, MCPClientError, MCPInvalidArgumentsError, MCPUnknownToolError
from mini_claude_code.agent.mcp.models import MCPToolDefinition, MCPToolResult, copy_json_object


class FakeMCPClient:
    """单调用方同步客户端；仅允许注入可信本地测试函数，不是权限沙箱。"""

    def __init__(self, tools: Sequence[MCPToolDefinition],
                 handlers: Mapping[str, Callable[..., MCPToolResult]]) -> None:
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names) or set(names) != set(handlers):
            raise ValueError("MCP 工具名称不能重复，且定义与 Handler 必须一一对应")
        if any(not callable(handler) for handler in handlers.values()):
            raise ValueError("MCP Handler 必须可调用")
        self._tools = tuple(tool.snapshot() for tool in tools)  # 私有工具目录，不与调用方共享字典
        self._handlers = dict(handlers)  # 原始工具名到本地模拟函数的映射
        self._closed = False  # close 后不可逆；不会自动重连

    def _ensure_open(self) -> None:
        if self._closed:
            raise MCPClientClosedError("MCP 客户端已关闭")

    def list_tools(self) -> tuple[MCPToolDefinition, ...]:
        self._ensure_open()
        return tuple(tool.snapshot() for tool in self._tools)

    def call_tool(self, name: str, arguments: dict) -> MCPToolResult:
        self._ensure_open()
        if not isinstance(name, str) or name not in self._handlers:
            raise MCPUnknownToolError("MCP 工具不存在")
        handler = self._handlers[name]
        try:
            copied = copy_json_object(arguments, "MCP 调用参数")
            signature(handler).bind(**copied)
        except (TypeError, ValueError) as error:
            raise MCPInvalidArgumentsError("MCP 参数不是有效对象，或与工具函数签名不匹配") from error
        # 参数名/数量已检查；字段语义由模拟工具验证，不声称完整验证了 JSON Schema。
        try:
            result = handler(**copied)
        except MCPClientError:
            raise
        except Exception as error:
            # 不输出任意异常正文，避免本地路径、密钥等被错误带给模型。
            return MCPToolResult(f"模拟工具执行失败：{type(error).__name__}", is_error=True)
        if not isinstance(result, MCPToolResult):
            raise MCPClientError("模拟工具返回值不符合 MCPToolResult 契约")
        return result

    def close(self) -> None:
        self._closed = True


def create_fake_docs_client() -> FakeMCPClient:
    """固定且无副作用的演示工具；搜索结果是模拟内容，不查询真实知识库。"""
    def search(query: str) -> MCPToolResult:
        if not isinstance(query, str) or not query.strip():
            return MCPToolResult("query 必须是非空字符串", is_error=True)
        return MCPToolResult(f"[模拟文档搜索] 查询：{query}\n示例条目：工具发现、会话恢复。")

    def get_version() -> MCPToolResult:
        return MCPToolResult("fake-docs/1.0（进程内模拟服务）")

    return FakeMCPClient([
        MCPToolDefinition("search", "搜索模拟文档，仅返回演示结果，不访问真实文件或网络。",
                          {"type": "object", "properties": {"query": {"type": "string"}},
                           "required": ["query"], "additionalProperties": False},
                          {"readOnlyHint": True}),
        MCPToolDefinition("get_version", "查询模拟文档服务的固定版本信息。",
                          {"type": "object", "properties": {}, "additionalProperties": False},
                          {"readOnlyHint": True}),
    ], {"search": search, "get_version": get_version})
