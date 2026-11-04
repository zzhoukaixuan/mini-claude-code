"""MCP 管理 CLI：用户交互、脱敏展示与显式连接诊断，不加载 Agent 或调用 LLM。"""

from contextlib import contextmanager
from getpass import getpass, GetPassWarning
import json
import logging
import warnings

from .configuration import ConfigurationError
from .config_store import mcp_config_transaction
from .mcp_configuration import MCPServerConfig, load_mcp_servers, save_mcp_servers


def register_mcp_commands(subparsers) -> None:
    parser = subparsers.add_parser("mcp", help="管理用户级 Streamable HTTP MCP 服务")
    commands = parser.add_subparsers(dest="mcp_command", required=True)
    add = commands.add_parser("add", help="交互填写服务地址和认证信息，不连接服务")
    add.add_argument("name", help="小写服务名，最多 20 字符")
    add.add_argument("--replace", action="store_true", help="允许覆盖已有配置，仍需确认")
    add.add_argument("--allow-local-http", action="store_true", help="允许本机回环 HTTP 演示服务")
    commands.add_parser("list", help="列出已配置服务（不探测在线状态）")
    for command, help_text in (("show", "显示脱敏配置"), ("test", "初始化连接并发现工具，不执行工具"),
                                ("remove", "删除配置，不撤销远端操作或关闭其他进程的连接")):
        child = commands.add_parser(command, help=help_text)
        child.add_argument("name")


def _confirm(message: str) -> bool:
    return input(message + " [y/N]：").strip().lower() == "y"


def _secret_input(prompt: str) -> str:
    # 非交互环境下 getpass 可能退化为回显输入；这里直接拒绝，防止意外泄密。
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(prompt)
        except GetPassWarning:
            raise ConfigurationError("当前终端不支持安全隐藏输入，请在交互终端运行。") from None


def _show(record) -> None:
    # JSON 转义服务端/用户输入中的控制字符，避免终端控制序列被执行。
    print(json.dumps(record, ensure_ascii=True, indent=2))


@contextmanager
def _quiet_transport_logs():
    """独立 CLI 诊断期间屏蔽第三方传输日志，避免 URL 查询密钥进入异常日志。

    此命令不并发运行 Agent；其他运行时的日志脱敏另行接入。
    """
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        yield
    finally:
        logging.disable(previous)


def _test_server(server: MCPServerConfig) -> int:
    if not _confirm("将连接该服务并发送配置的认证信息，仅发现工具，是否继续？"):
        print("已取消，未连接服务。")
        return 0
    client = None
    succeeded = False
    with _quiet_transport_logs():
        try:
            from .agent.mcp.http_client import HTTPMCPClient
            client = HTTPMCPClient(server.url, headers=server.headers,
                                   allow_local_http=server.allow_local_http)
            tools = client.list_tools()
            _show({"server": server.name, "tools": [tool.name for tool in tools]})
            succeeded = True
        except ImportError:
            print('缺少 MCP 依赖，请安装：python -m pip install -e ".[mcp]"')
        except Exception as error:
            print(f"连接或发现失败：{type(error).__name__}（不回显地址、凭据或原始异常）。")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as error:
                    succeeded = False
                    print(f"连接清理未确认完成：{type(error).__name__}。")
    if succeeded:
        print("连接、发现和关闭完成；未执行工具，不代表所有工具均可用。")
    return 0 if succeeded else 1


def run_mcp_command(args, *, config_dir=None) -> int:
    """交互在锁外；add/remove 的最终存在性检查与落盘在同一个锁内。"""
    try:
        if args.mcp_command == "add":
            # URL 也可能含 key，不接受命令行凭据，避免 shell 历史泄漏。
            url = _secret_input("MCP Streamable HTTP 完整 URL（输入不回显）：").strip()
            headers = {}
            while True:
                name = input("认证 Header 名称（如 Authorization；直接回车结束）：").strip()
                if not name:
                    break
                if name.lower() in {key.lower() for key in headers}:
                    raise ConfigurationError("不能重复输入同名 Header。")
                headers[name] = _secret_input("Header 值（如 Bearer …；输入不回显）：")
            server = MCPServerConfig(args.name, url, headers, args.allow_local_http)
            _show(server.public_record())
            if not _confirm("将凭据以受限权限明文保存到用户配置，是否继续？"):
                print("已取消，未保存。")
                return 0
            with mcp_config_transaction(config_dir):
                servers = load_mcp_servers(config_dir)
                if args.name in servers and not args.replace:
                    raise ConfigurationError("服务已存在；更新请显式指定 --replace。")
                servers[args.name] = server
                save_mcp_servers(servers, config_dir)
            print("已保存；未连接服务。运行中的 Agent 不会热加载此变更。")
            return 0
        servers = load_mcp_servers(config_dir)
        if args.mcp_command == "list":
            _show([server.public_record() for server in servers.values()])
            return 0
        server = servers.get(args.name)
        if server is None:
            raise ConfigurationError("未找到该 MCP 服务。")
        if args.mcp_command == "show":
            _show(server.public_record())
        elif args.mcp_command == "test":
            _show(server.public_record())
            return _test_server(server)
        elif args.mcp_command == "remove":
            if not _confirm("删除该服务配置？不会撤销工具副作用或断开其他进程的连接"):
                return 0
            with mcp_config_transaction(config_dir):
                current = load_mcp_servers(config_dir)
                if current.get(args.name) != server:
                    raise ConfigurationError("配置已被其他进程修改，请重新查看后删除。")
                del current[args.name]
                save_mcp_servers(current, config_dir)
            print("已删除配置。")
        return 0
    except ConfigurationError as error:
        print(f"MCP 配置错误：{error}")
        return 2
    except (OSError, TimeoutError) as error:
        print(f"MCP 配置操作失败：{type(error).__name__}；请检查目录权限或配置锁。")
        return 1
