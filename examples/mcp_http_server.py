"""独立 HTTP MCP 演示服务：真实协议、固定模拟数据，不读取配置/密钥或操作工作区。

从源码根目录运行 python examples/mcp_http_server.py，监听 127.0.0.1:8765/mcp。
仅用于本地演示，无认证，不应绑定公网地址。
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

DEMO_VERSION = "http-docs/1.0"  # 演示服务版本，不冒充外部知识库版本
DEMO_DOCUMENTS = (  # 固定内存数据，无文件、网络或模型访问
    ("工具发现", "tools/list 返回工具定义，发现本身不会执行工具。"),
    ("会话恢复", "从已保存的会话快照恢复上下文；未知工具结果不应自动重放。"),
    ("权限检查", "外部工具调用仍需通过 Harness 权限检查，服务端只读提示不是授权。"),
)


def search(query: str) -> str:
    """按字面关键词搜索固定模拟文档；不是语义搜索，不读取项目文件。"""
    query = query.strip()
    if not query:
        raise ValueError("query 必须是非空字符串")
    matches = [f"- {title}：{body}" for title, body in DEMO_DOCUMENTS
               if query.casefold() in (title + " " + body).casefold()]
    return "[模拟文档搜索]\n" + ("\n".join(matches) if matches else "没有匹配的模拟条目。")


def get_version() -> str:
    """返回本地演示服务版本；协议通信是真实的，知识数据是模拟的。"""
    return DEMO_VERSION


def create_server() -> FastMCP:
    """构造但不启动服务；导入模块不会监听端口或创建线程/子进程。"""
    server = FastMCP("miniclaude-demo-docs", host="127.0.0.1", port=8765,
                     json_response=True, log_level="WARNING")
    hints = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
    # 明确使用文本输出，匹配当前 MCPToolResult 文本适配边界。
    server.tool(annotations=hints, structured_output=False)(search)
    server.tool(annotations=hints, structured_output=False)(get_version)
    return server


def main() -> None:
    """显式运行本地演示服务；客户端连接和断开不会控制此服务的进程。"""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
