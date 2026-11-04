"""轻量启动入口：解析参数、选择历史、取得会话所有权后才加载 CLI 运行时。"""

from __future__ import annotations

import argparse
import atexit
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Callable

from mini_claude_code.configuration import ConfigurationError, get_connection

from .lifecycle import SessionController
from .messages import restore_session_messages
from .store import SessionStore


_RETAINED_OWNERSHIP = ExitStack()  # 后台线程未退出时保留所有权，直到进程真正退出
atexit.register(_RETAINED_OWNERSHIP.close)


class SessionSelectionError(ValueError):
    """可直接展示的选择失败说明，不包含磁盘记录正文或敏感配置。"""


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MiniClaudeCode：新建或恢复当前工作目录的会话")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--continue", dest="continue_latest", action="store_true", help="恢复最近更新的会话")
    group.add_argument("--resume", nargs="?", const="", metavar="SESSION_ID", help="指定会话 ID，或不带 ID 按编号选择")
    parser.add_argument("--env-file", type=Path, help="显式选择完整连接 .env 文件")
    return parser.parse_args(argv)


def select_session(store: SessionStore, options: argparse.Namespace) -> str | None:
    """只选择 ID；列表快照不是执行依据，拿到占用锁后必须重新读取。"""
    if options.resume not in (None, ""):
        return options.resume
    if not options.continue_latest and options.resume is None:
        return store.create().session_id
    sessions = store.list_sessions()
    if not sessions:
        raise SessionSelectionError("当前工作目录没有历史会话；请不带恢复参数启动新会话。")
    if options.continue_latest:
        return sessions[0].session_id
    for index, snapshot in enumerate(sessions, 1):
        updated = datetime.fromtimestamp(snapshot.updated_at).astimezone().isoformat(timespec="seconds")
        state = "中断" if snapshot.turn_in_progress else "已收尾"
        print(f"{index}. {snapshot.title} | {updated} | {len(snapshot.messages)} 条消息 | {state}")
        print(f"   {snapshot.session_id}")
    while True:
        choice = input("选择会话编号（回车或 q 取消）：").strip()
        if not choice or choice.casefold() == "q":
            return None
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(sessions):
            return sessions[int(choice) - 1].session_id
        print("请输入列表内的编号。")


def main(argv: list[str] | None = None, *, prepare_runtime: Callable[[], None] | None = None) -> int:
    """历史浏览不构造客户端；恢复不触发模型，也不重放旧工具。"""
    options = parse_arguments(argv)
    ownership = ExitStack()
    session: SessionController | None = None
    phase = "选择会话"
    try:
        store = SessionStore(Path.cwd())
        session_id = select_session(store, options)
        if session_id is None:
            return 0
        try:
            ownership.enter_context(store.ownership(session_id))
        except TimeoutError:
            print("该会话正在被其他进程占用，未恢复；不要删除锁文件绕过检查。")
            return 1
        phase = "读取并校验会话（包括工作目录及消息协议）"
        snapshot = store.load(session_id)
        history, warnings = restore_session_messages(snapshot)
        # 先验证记录，再加载配置；用户级密钥也必须参与第一次恢复保存的脱敏。
        phase = "加载运行配置"
        if prepare_runtime is not None:
            prepare_runtime()
        else:
            get_connection(env_file=options.env_file)
        if not snapshot.turn_in_progress and (options.continue_latest or options.resume is not None):
            history.append({"role": "user", "content": (
                "[Harness 会话恢复] 已恢复历史上下文，但未恢复后台执行、成员或审批状态。"
                "历史文件信息可能过期，修改前请重新读取；历史审批不代表本次授权。"
                "请等待新的用户指令或 Harness 事件，不自动重跑历史工具。"
            )})
        session = SessionController(store, snapshot)
        # 完整历史也做保存侧校验；修复后的稳定快照必须先落盘，才能启动运行时。
        session.checkpoint(history, "completed")
        for warning in warnings:
            print(warning)
        if options.continue_latest or options.resume is not None:
            print("已恢复聊天上下文；未恢复后台任务、成员、审批或文件读取凭据，修改前需重新读取。")
        phase = "加载或运行 CLI"
        from ..cli import run_cli
        return run_cli(session, history)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消启动或退出。")
        return 0
    except SessionSelectionError as error:
        print(str(error))
        return 1
    except ConfigurationError as error:
        print(f"配置错误：{error}")
        return 2
    except FileNotFoundError:
        print(f"{phase}失败：所需文件或会话不存在；未自动新建替代会话。")
        return 1
    except Exception as error:
        # 不打印 Provider/任意记录中的原始异常正文，避免意外泄露敏感内容。
        print(f"{phase}失败（{type(error).__name__}）；请检查会话记录、工作目录和运行配置。未自动新建替代会话。")
        return 1
    finally:
        if session is not None and not session.release_safe:
            _RETAINED_OWNERSHIP.enter_context(ownership.pop_all())
        ownership.close()
