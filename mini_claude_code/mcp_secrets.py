"""进程内 MCP 凭据登记与精确值脱敏；不读取配置、不落盘，不替代通用 DLP。"""

import logging
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

_secrets: tuple[str, ...] = ()  # 保留已加载凭据直到进程退出，断连后仍能保护历史内容


def register_mcp_secrets(url: str, headers: dict[str, str]) -> None:
    global _secrets
    values = {url, *headers.values()}
    values.update(value for _, value in parse_qsl(urlsplit(url).query) if value)
    for name, value in headers.items():
        if name.lower() == "authorization" and " " in value:
            values.add(value.split(" ", 1)[1])
    values.discard("")
    values.update(quote(value, safe="") for value in tuple(values))
    values.update(quote_plus(value) for value in tuple(values))
    _secrets = tuple(sorted(set(_secrets) | values, key=lambda value: (-len(value), value)))


def active_mcp_secrets() -> tuple[str, ...]:
    return _secrets


def redact_mcp_text(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


class _TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_mcp_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            # 不输出第三方 traceback 的 locals/地址等细节；只留异常类型。
            record.msg += f" ({record.exc_info[0].__name__})"
            record.exc_info = None
            record.exc_text = None
        return True


def protect_mcp_transport_logs() -> None:
    """保护已知 SDK/HTTP 日志源，不全局关闭应用日志。自定义日志器不在此保证范围内。"""
    names = {"mcp.client.streamable_http", "mcp.client.session", "mcp.shared.session", "httpx"}
    names.update(name for name in logging.root.manager.loggerDict if name.startswith("httpcore."))
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(item, _TransportLogFilter) for item in logger.filters):
            logger.addFilter(_TransportLogFilter())
