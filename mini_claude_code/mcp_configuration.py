"""用户级 HTTP MCP 配置模型、校验与存储；不加载模型运行时或 MCP SDK，不连接服务。"""

from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit

from .configuration import ConfigurationError
from .config_store import load_private_config, save_private_config


SERVER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,19}\Z")  # 限制命名空间长度，预留工具名空间
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")  # HTTP token
RESERVED_HEADERS = frozenset({"host", "content-length", "connection", "accept", "content-type",
                              "mcp-session-id", "mcp-protocol-version", "last-event-id",
                              "transfer-encoding", "upgrade"})  # 由传输层维护


def validate_http_endpoint(url: str, *, allow_local_http: bool = False) -> str:
    """默认 HTTPS；仅显式放行字面回环 HTTP，不将该规则冒充网络沙箱。"""
    if not isinstance(url, str) or not url or "\\" in url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("MCP URL 必须是无空白的完整地址")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" or (allow_local_http and parsed.scheme == "http"
                                            and parsed.hostname in {"127.0.0.1", "::1"})
        if not valid or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.port == 0:
            raise ValueError
    except ValueError:
        raise ValueError("MCP URL 必须使用 HTTPS、无内嵌账号/片段；本地演示可显式允许回环 HTTP") from None
    return url


def validate_http_headers(headers: dict[str, str]) -> dict[str, str]:
    """复制认证 header；拒绝换行、重复大小写名称与协议字段覆盖。"""
    if not isinstance(headers, dict):
        raise ValueError("MCP headers 必须是对象")
    result = {}
    seen = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not HEADER_NAME.fullmatch(name) or not isinstance(value, str):
            raise ValueError("MCP header 名称或值无效")
        if name.lower() in RESERVED_HEADERS or name.lower() in seen:
            raise ValueError("MCP header 重复或覆盖传输协议字段")
        if any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("MCP header 值必须是可打印 ASCII 文本")
        seen.add(name.lower())
        result[name] = value
    return result


@dataclass(frozen=True)
class MCPServerConfig:
    name: str  # 用户指定的服务名，不接受模型提供连接地址
    url: str = field(repr=False)  # 查询参数或路径可能含凭据，不出现在 repr
    headers: dict[str, str] = field(default_factory=dict, repr=False)  # 独立于模型 API Key
    allow_local_http: bool = False  # 仅供显式开启的本地回环演示

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not SERVER_NAME.fullmatch(self.name):
            raise ConfigurationError("MCP 名称须以小写字母开头，仅含小写字母、数字、下划线、连字符，最多 20 字符。")
        if type(self.allow_local_http) is not bool:
            raise ConfigurationError("MCP allow_local_http 必须是布尔值。")
        try:
            validate_http_endpoint(self.url, allow_local_http=self.allow_local_http)
            object.__setattr__(self, "headers", validate_http_headers(self.headers))
        except ValueError:
            raise ConfigurationError("MCP 地址或认证 headers 无效；未保存配置。") from None

    def public_record(self) -> dict:
        """CLI 展示专用：隐藏整个路径、查询参数和所有 header 值。"""
        parsed = urlsplit(self.url)
        return {"name": self.name, "transport": "streamable-http",
                "endpoint": f"{parsed.scheme}://{parsed.netloc}/<hidden>",
                "headers": {name: "<redacted>" for name in self.headers},
                "allow_local_http": self.allow_local_http}


def load_mcp_servers(config_dir: Path | None = None) -> dict[str, MCPServerConfig]:
    """文件缺失视为空清单；非法结构直接失败，不静默丢弃某个服务。"""
    record = load_private_config("mcp.json", config_dir)
    if record is None:
        return {}
    if set(record) != {"schema_version", "servers"} or type(record["schema_version"]) is not int or record["schema_version"] != 1 or not isinstance(record["servers"], dict):
        raise ConfigurationError("MCP 配置版本或字段无效。")
    servers = {}
    for name, entry in record["servers"].items():
        if not isinstance(entry, dict) or set(entry) != {"transport", "url", "headers", "allow_local_http"} or entry["transport"] != "streamable-http":
            raise ConfigurationError("MCP 服务字段无效；仅支持 streamable-http。")
        servers[name] = MCPServerConfig(name, entry["url"], entry["headers"], entry["allow_local_http"])
    return servers


def save_mcp_servers(servers: dict[str, MCPServerConfig], config_dir: Path | None = None) -> Path:
    """保存完整快照；后续 CLI 的读改写须持有同一文件锁，不可用原子替换代替事务锁。"""
    entries = {}
    for name, server in servers.items():
        if name != server.name:
            raise ConfigurationError("MCP 清单名称与服务名称不一致。")
        # 再校验持久化边界，避免嵌套可变 headers 在构造后被修改。
        validated = MCPServerConfig(name, server.url, server.headers, server.allow_local_http)
        entries[name] = {"transport": "streamable-http", "url": validated.url,
                         "headers": validated.headers, "allow_local_http": validated.allow_local_http}
    return save_private_config("mcp.json", {"schema_version": 1, "servers": entries}, config_dir)
