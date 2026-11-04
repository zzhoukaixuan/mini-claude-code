"""MiniClaudeCode 产品 CLI：解析参数、分发命令并规范进程退出码。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from collections.abc import Sequence

from . import __version__
from . import backend


EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """构建不加载 Agent 运行时和模型配置的命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="miniclaude",
        description="MiniClaudeCode：面向 Harness Engineering 的 Coding Agent CLI",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    ask_parser = subparsers.add_parser(
        "ask",
        help="执行一次同步任务，输出最终回答后退出",
        description=(
            "在全新会话中执行一次同步 Coding Agent 任务；"
            "进程内后台与定时能力请使用 chat。"
        ),
    )
    ask_parser.add_argument(
        "query",
        nargs="+",
        metavar="QUERY",
        help="要交给 Agent 的任务；包含空格时可以使用引号",
    )
    ask_parser.add_argument(
        "--permission-mode",
        choices=backend.VALID_PERMISSION_MODES,
        default="ask",
        help="工具权限模式（默认：ask）",
    )
    ask_parser.add_argument(
        "--memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否启用跨会话 Memory（默认：启用）",
    )

    chat_parser = subparsers.add_parser(
        "chat",
        help="进入支持会话恢复、团队协作与 Worktree 隔离的多轮会话",
        description=(
            "启动支持 /btw、Memory、Task、Background Job、Cron、团队审批与 Worktree 隔离的"
            "交互式 Coding Agent。"
        ),
    )
    recovery = chat_parser.add_mutually_exclusive_group()
    recovery.add_argument("--continue", dest="continue_latest", action="store_true", help="恢复当前目录最近的会话")
    recovery.add_argument("--resume", nargs="?", const="", metavar="SESSION_ID", help="按编号选择或指定会话 ID")
    for command_parser in (ask_parser, chat_parser):
        command_parser.add_argument("--env-file", type=Path, help="显式选择完整连接 .env；不会借用用户级密钥")
    subparsers.add_parser("configure", help="交互配置用户级 API 地址、密钥和默认模型（不调用模型）")
    doctor = subparsers.add_parser("doctor", help="只读检查安装、依赖与配置，不联网")
    doctor.add_argument("--json", action="store_true", help="输出机器可读诊断，不含凭据")
    from .mcp_cli import register_mcp_commands
    register_mcp_commands(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行 CLI，并返回适合作为进程状态的退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_OK

    try:
        if args.command == "doctor":
            from .doctor import run_doctor
            return run_doctor(as_json=args.json)
        if args.command == "configure":
            from .configure import configure
            return configure()
        if args.command == "mcp":
            from .mcp_cli import run_mcp_command
            return run_mcp_command(args)
        connection_options = {"env_file": args.env_file} if args.env_file is not None else {}
        if args.command == "ask":
            answer = backend.run_once(
                " ".join(args.query),
                permission_mode=args.permission_mode,
                memory_enabled=args.memory,
                **connection_options,
            )
            if answer:
                print(answer)
            return EXIT_OK
        if args.command == "chat":
            if args.continue_latest:
                return backend.run_chat(["--continue"], **connection_options)
            if args.resume is not None:
                return backend.run_chat(["--resume", *([args.resume] if args.resume else [])], **connection_options)
            return backend.run_chat(**connection_options)
    except backend.ProductConfigurationError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except KeyboardInterrupt:
        print("\nMiniClaudeCode 已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except EOFError:
        print("输入结束，配置未继续保存。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as error:
        print(
            f"运行失败：{type(error).__name__}：{error}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR

    parser.error(f"未知命令：{args.command}")
    return EXIT_USAGE_ERROR
