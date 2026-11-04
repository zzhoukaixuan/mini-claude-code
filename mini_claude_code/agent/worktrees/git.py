"""内部 Git 调用边界：统一子进程环境、超时和失败诊断，不注册为模型工具。"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

from ..config import WORKTREE_GIT_TIMEOUT_SECONDS, WORKTREE_GIT_ERROR_MAX_CHARS


class WorktreeGitError(RuntimeError):
    """Git 未确认成功；写命令失败时调用方必须考虑部分落盘。"""


def run_git(
    args: list[str], *, cwd: Path,
    timeout: float = WORKTREE_GIT_TIMEOUT_SECONDS,
) -> str:
    """执行 Harness 构造的参数并返回完整 stdout；非零退出、超时均抛错。

    不接受 shell 字符串，不重试。完整 stdout 用于解析路径及登记信息，
    不能为了展示截断它；错误诊断单独限长，不保证子进程输出的内存上限。
    """
    if not isinstance(args, list) or not args or any(
        not isinstance(arg, str) or "\0" in arg for arg in args
    ):
        raise TypeError("Git 参数必须是非空字符串列表，不能包含 NUL")
    if not isinstance(cwd, Path) or not cwd.is_absolute():
        raise ValueError("Git cwd 必须是绝对 Path")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Git 超时必须是有限正数")
    git_env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        git_env.pop(key, None)
    git_env["GIT_TERMINAL_PROMPT"] = "0"  # 后台 Git 操作不能等待终端凭据输入
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=git_env, capture_output=True,
            text=True, encoding="utf-8", errors="strict", timeout=timeout,
            shell=False, check=False,
        )
    except FileNotFoundError as error:
        raise WorktreeGitError("无法启动 Git，请确认已安装 Git 且工作目录仍存在") from error
    except subprocess.TimeoutExpired as error:
        raise WorktreeGitError(f"Git 命令超时（{timeout} 秒），执行结果未确认") from error
    except UnicodeError as error:
        raise WorktreeGitError("Git 输出不是有效 UTF-8，未采用不可靠结果") from error
    except OSError as error:
        raise WorktreeGitError(f"Git 进程执行失败：{error}") from error
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or "无诊断信息")[:WORKTREE_GIT_ERROR_MAX_CHARS]
        raise WorktreeGitError(f"Git 命令失败（退出码 {result.returncode}）：{detail}")
    return result.stdout
