"""预配置 MCP 连接服务：管理工厂创建、注册所有权转移和失败清理，不接受任意启动配置。"""

from typing import Callable, Mapping
from threading import Event

from .client import MCPClient
from .models import MCPToolResult
from .registry import MCPRegistry


class MCPConnectionService:
    """调用方串行访问；工厂必须返回全新 Client，并自行清理创建中途失败的资源。"""

    def __init__(self, registry: MCPRegistry, factories: Mapping[str, Callable[[], MCPClient]]) -> None:
        self._registry = registry  # 成功注册后接管 Client 关闭责任
        self._factories = dict(factories)  # Harness 配置快照，模型只能选择已登记名称
        self._stopping = Event()  # CLI 退出先关闭新连接入口，再等待 Lead 回合结束

    def connect(self, server_name: str) -> MCPToolResult:
        if self._stopping.is_set():
            return MCPToolResult("MCP 服务正在退出，禁止创建新连接。", is_error=True)
        if not isinstance(server_name, str) or server_name not in self._factories:
            return MCPToolResult("未知 MCP 服务；只能连接 Harness 预配置的服务。", is_error=True)
        if server_name in self._registry.list_servers():
            return MCPToolResult(f"MCP 服务 {server_name} 已连接，未重复创建客户端。")
        try:
            client = self._factories[server_name]()
        except Exception as error:
            return MCPToolResult(f"MCP 客户端创建失败：{type(error).__name__}；未注册服务。", is_error=True)
        try:
            if self._stopping.is_set():
                raise RuntimeError("MCP 服务已请求退出")
            names = self._registry.register(server_name, client)
        except Exception as error:
            try:
                client.close()
            except Exception as cleanup_error:
                return MCPToolResult(
                    f"MCP 注册失败：{type(error).__name__}；未发布工具，"
                    f"但客户端清理也失败：{type(cleanup_error).__name__}，需要检查残留资源。", is_error=True)
            return MCPToolResult(f"MCP 注册失败：{type(error).__name__}；新客户端已关闭，已有连接不变。", is_error=True)
        return MCPToolResult(f"MCP 服务 {server_name} 已注册；发现工具：{', '.join(names) or '无'}。"
                             "新增能力以之后模型请求实际收到的工具列表为准。")

    def disconnect(self, server_name: str) -> MCPToolResult:
        if not isinstance(server_name, str) or server_name not in self._factories:
            return MCPToolResult("未知 MCP 服务；只能管理 Harness 预配置的服务。", is_error=True)
        try:
            removed = self._registry.disconnect(server_name)
        except Exception as error:
            return MCPToolResult(f"MCP 工具已移除，但客户端关闭失败：{type(error).__name__}；"
                                 "需要检查资源清理，不会重新开放该连接。", is_error=True)
        return MCPToolResult(f"MCP 服务 {server_name} 已断开。" if removed else f"MCP 服务 {server_name} 未连接，无需断开。")

    def request_stop(self) -> None:
        """可在停止后台回合前调用；这里只关新连接入口，不并发修改注册表。"""
        self._stopping.set()

    def close_all(self) -> tuple[str, ...]:
        """须在 Lead 停止后调用；逐一清理，单个失败不阻断其他连接。"""
        self.request_stop()
        errors = []
        for name in self._registry.list_servers():
            try:
                self._registry.disconnect(name)
            except Exception as error:
                errors.append(f"MCP 服务 {name} 工具已移除，但清理失败：{type(error).__name__}；需检查残留连接或线程。")
        return tuple(errors)
