"""跨平台文件锁模块：使用操作系统锁保护需要锁内重读的持久状态事务。"""

from __future__ import annotations

import errno
import importlib
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, cast

from .paths import is_path_within_root


def _acquire_nonblocking(lock_file: BinaryIO) -> None:
    """使用当前平台的标准库后端尝试一次非阻塞独占锁。"""
    if os.name == "nt":
        msvcrt = cast(Any, importlib.import_module("msvcrt"))
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return
    if os.name == "posix":
        fcntl = cast(Any, importlib.import_module("fcntl"))
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    raise OSError(f"当前操作系统 {os.name!r} 不支持 Task 文件锁")


def _release_lock(lock_file: BinaryIO) -> None:
    """使用与获取阶段相同的平台后端释放独占锁。"""
    if os.name == "nt":
        msvcrt = cast(Any, importlib.import_module("msvcrt"))
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if os.name == "posix":
        fcntl = cast(Any, importlib.import_module("fcntl"))
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    raise OSError(f"当前操作系统 {os.name!r} 不支持 Task 文件锁")


def _is_lock_contention(error: OSError) -> bool:
    """判断操作系统错误是否表示锁当前由其他执行者持有。"""
    return error.errno in (errno.EACCES, errno.EAGAIN)


@contextmanager
def exclusive_file_lock(
    lock_path: Path,
    *,
    allowed_root: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> Iterator[None]:
    """在有限等待内获取跨平台独占文件锁，并在退出临界区时可靠释放。"""
    if not isinstance(lock_path, Path):
        raise TypeError("锁文件路径必须是 pathlib.Path 类型")
    if not isinstance(allowed_root, Path):
        raise TypeError("锁文件允许根目录必须是 pathlib.Path 类型")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("锁等待超时必须是有限的正数")
    if (
        isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, (int, float))
        or not math.isfinite(poll_interval_seconds)
        or poll_interval_seconds <= 0
    ):
        raise ValueError("锁轮询间隔必须是有限的正数")

    resolved_root = allowed_root.resolve()
    if lock_path.is_symlink():
        raise ValueError(f"锁文件 {lock_path} 不能是符号链接")
    resolved_lock_path = lock_path.resolve()
    if not is_path_within_root(resolved_lock_path, resolved_root):
        raise ValueError(
            f"锁文件 {resolved_lock_path} 不在允许目录 {resolved_root} 内"
        )
    resolved_lock_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = resolved_lock_path.parent.resolve()
    if not is_path_within_root(resolved_parent, resolved_root):
        raise ValueError(
            f"锁文件目录 {resolved_parent} 不在允许目录 {resolved_root} 内"
        )
    resolved_lock_path = resolved_parent / resolved_lock_path.name
    if resolved_lock_path.is_symlink():
        raise ValueError(f"锁文件 {resolved_lock_path} 不能是符号链接")
    if resolved_lock_path.exists() and not resolved_lock_path.is_file():
        raise ValueError(f"锁路径 {resolved_lock_path} 不是普通文件")

    deadline = time.monotonic() + float(timeout_seconds)
    with open(resolved_lock_path, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())

        while True:
            try:
                _acquire_nonblocking(lock_file)
            except OSError as error:
                if not _is_lock_contention(error):
                    raise
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise TimeoutError(
                        f"等待文件锁 {resolved_lock_path} 超过 {timeout_seconds} 秒"
                    ) from error
                time.sleep(min(float(poll_interval_seconds), remaining_seconds))
            else:
                break

        try:
            yield
        finally:
            _release_lock(lock_file)
