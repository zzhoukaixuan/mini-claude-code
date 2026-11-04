"""Harness 预配置的 HTTP 服务工厂；延迟导入 SDK，不将 URL 或凭据交给模型。"""

from .client import MCPClient
from functools import partial
from mini_claude_code.mcp_configuration import MCPServerConfig
from mini_claude_code.mcp_secrets import register_mcp_secrets


def _create_http_client(config: MCPServerConfig) -> MCPClient:
    from .http_client import HTTPMCPClient
    return HTTPMCPClient(config.url, headers=config.headers, allow_local_http=config.allow_local_http)


def build_http_factories(servers: dict[str, MCPServerConfig]) -> dict:
    """固定启动快照，只登记凭据与工厂，不创建连接；每个 partial 绑定独立配置。"""
    factories = {}
    for name, config in servers.items():
        snapshot = MCPServerConfig(name, config.url, config.headers, config.allow_local_http)
        register_mcp_secrets(snapshot.url, snapshot.headers)
        factories[name] = partial(_create_http_client, snapshot)
    return factories
