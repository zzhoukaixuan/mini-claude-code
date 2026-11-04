"""MCP 客户端最小接口与错误类型；Fake 与 Streamable HTTP 共享，不依赖 Agent。"""

from typing import Protocol

from .models import MCPToolDefinition, MCPToolResult


class MCPClientError(RuntimeError):
    """连接、调用协议或客户端生命周期错误，不代表工具正常返回业务失败。"""


class MCPClientClosedError(MCPClientError):
    """客户端已经关闭，不允许继续发现或调用。"""


class MCPUnknownToolError(MCPClientError):
    """请求了服务端没有注册的原始工具名。"""


class MCPInvalidArgumentsError(MCPClientError):
    """工具调用参数结构无效。"""


class MCPClient(Protocol):
    """由上层负责持有和关闭；发现不执行工具，调用不自行重试副作用。"""

    def list_tools(self) -> tuple[MCPToolDefinition, ...]:
        """返回独立的工具定义快照，不隐式调用任何工具。"""
        ...

    def call_tool(self, name: str, arguments: dict) -> MCPToolResult:
        """按服务端原名调用一次；业务失败返回 is_error，连接/协议失败抛异常。"""
        ...

    def close(self) -> None:
        """幂等关闭；关闭后不得继续调用或重新打开这个实例。"""
        ...
