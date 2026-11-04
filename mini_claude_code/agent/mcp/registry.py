"""MCP 连接注册与工具池组装：名称防冲突、整体发布、独立快照；尚不接入权限或 Agent。"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from ..tools.registry import collect_enabled_tool_names
from .client import MCPClient, MCPClientClosedError, MCPClientError
from .adapter import MCPToolHandler
from .models import MCPToolDefinition, MCPToolResult

MAX_TOOL_NAME_LENGTH = 64  # 当前模型工具名的项目限制；超长拒绝，不截断制造冲突
DISALLOWED_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")  # 外部名称映射到模型可接受字符


def normalize_mcp_name(name: str) -> str:
    """只做名称映射；是否发生碰撞由注册表统一判断。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("MCP 服务/工具名称必须是非空字符串")
    return DISALLOWED_NAME_CHARS.sub("_", name)


@dataclass
class _Connection:
    server_name: str  # 调用方提供的原始服务名
    normalized_name: str  # 用于模型工具命名的规范化服务名
    client: MCPClient  # 注册成功后由注册表负责关闭
    tools: tuple[MCPToolDefinition, ...]  # 注册时发现的私有快照，不动态修改
    active: bool = True  # 使已取出的旧 Handler 在断开后也立即失效


class MCPRegistry:
    """单所有者同步注册表；上层须串行化注册、调用与断开，不承诺并发取消。

    注册失败时 Client 仍归调用方管理；成功后才转移关闭责任。
    返回的 MCP Handler 暂时返回 MCPToolResult，第三阶段再适配统一工具结果。
    """

    def __init__(self, builtin_tools: list[dict], builtin_handlers: dict[str, Callable]) -> None:
        self._builtin_names = set(collect_enabled_tool_names(builtin_tools, builtin_handlers))
        self._builtin_tools = deepcopy(builtin_tools)  # 独立内置 Schema 基线
        self._builtin_handlers = dict(builtin_handlers)  # 不修改原 Handler 表
        self._connections: dict[str, _Connection] = {}  # 原始服务名到已发布连接
        self._revision = 0  # 注册/移除后递增，供后续缓存失效使用

    @property
    def revision(self) -> int:
        return self._revision

    def list_servers(self) -> tuple[str, ...]:
        """按注册顺序列举服务，不暴露可变连接对象。"""
        return tuple(self._connections)

    @staticmethod
    def _tool_name(server: str, tool: str) -> str:
        name = f"mcp__{server}__{normalize_mcp_name(tool)}"
        if len(name) > MAX_TOOL_NAME_LENGTH:
            raise ValueError("MCP 组装后的工具名称超过长度限制")
        return name

    def register(self, server_name: str, client: MCPClient) -> tuple[str, ...]:
        """先发现和完整校验，最后一次发布；失败不改动已有注册表或 revision。"""
        normalized = normalize_mcp_name(server_name)
        if len(f"mcp__{normalized}__x") > MAX_TOOL_NAME_LENGTH:
            raise ValueError("MCP 服务名称过长")
        if any(connection.normalized_name == normalized for connection in self._connections.values()):
            raise ValueError("MCP 服务已注册或规范化后名称冲突")
        if any(connection.client is client for connection in self._connections.values()):
            raise ValueError("同一 MCP Client 实例不能注册为多个服务")
        tools = tuple(tool.snapshot() for tool in client.list_tools())
        occupied = set(self._builtin_names)
        for connection in self._connections.values():
            occupied.update(self._tool_name(connection.normalized_name, tool.name) for tool in connection.tools)
        names = []
        for tool in tools:
            name = self._tool_name(normalized, tool.name)
            if name in occupied:
                raise ValueError(f"MCP 工具名称冲突：{name}")
            occupied.add(name)
            names.append(name)
        self._connections[server_name] = _Connection(server_name, normalized, client, tools)
        self._revision += 1
        return tuple(names)

    @staticmethod
    def _bind_handler(connection: _Connection, original_name: str) -> Callable[..., MCPToolResult]:
        # 工厂闭包固定依赖；不把 client/name 暴露为模型可覆盖的默认关键字参数。
        def invoke(**arguments: object) -> MCPToolResult:
            if not connection.active:
                raise MCPClientClosedError("该 MCP 连接已移除，请刷新工具池")
            return connection.client.call_tool(original_name, arguments)
        return MCPToolHandler(invoke)

    def assemble_tool_pool(self) -> tuple[list[dict], dict[str, Callable]]:
        """返回独立 Schema 与 Handler 表；annotations 不伪装成权限，也不直接塞入模型 Schema。"""
        tools = deepcopy(self._builtin_tools)
        handlers = dict(self._builtin_handlers)
        for connection in self._connections.values():
            for tool in connection.tools:
                name = self._tool_name(connection.normalized_name, tool.name)
                tools.append({"name": name, "description": tool.description,
                              "input_schema": deepcopy(tool.input_schema)})
                handlers[name] = self._bind_handler(connection, tool.name)
        return tools, handlers

    def disconnect(self, server_name: str) -> bool:
        """先撤销能力，再释放连接；关闭失败也不把可能已失效的 Client 重新挂回工具池。"""
        connection = self._connections.pop(server_name, None)
        if connection is None:
            return False
        connection.active = False
        self._revision += 1
        try:
            connection.client.close()
        except Exception as error:
            raise MCPClientError("MCP 工具已移除，但客户端关闭失败；调用方需要检查资源清理") from error
        return True
