"""Worktree 生命周期日志：锁内读取校验、追加事件并原子替换，不承担业务回滚或自动恢复。"""

from __future__ import annotations

import json
import math
import stat
import time
from pathlib import Path

from ..config import (
    TASK_ID_PATTERN, WORKTREE_DIRECTORY_NAME, WORKTREE_EVENT_FILE_NAME,
    WORKTREE_EVENT_LOCK_NAME, WORKTREE_EVENT_MAX_BYTES,
    WORKTREE_EVENT_DETAIL_MAX_CHARS, WORKTREE_EVENT_LOCK_TIMEOUT_SECONDS,
    WORKTREE_EVENT_LOCK_POLL_SECONDS, WORKTREE_OPERATION_ID_PATTERN,
)
from ..storage.atomic import atomic_replace_bytes
from ..storage.locking import exclusive_file_lock
from ..storage.paths import is_path_within_root
from .models import RepositoryLayout
from .names import validate_worktree_name
from .paths import _require_plain_directory_if_present


def _validate_event(record: object) -> None:
    """校验本阶段事件格式；损坏历史不能被新日志覆盖。"""
    if not isinstance(record, dict) or set(record) != {
        "operation_id", "timestamp", "task_id", "worktree", "stage", "outcome", "detail",
    }:
        raise ValueError("Worktree 事件字段不完整或含未知字段")
    if not isinstance(record["operation_id"], str) or WORKTREE_OPERATION_ID_PATTERN.fullmatch(record["operation_id"]) is None:
        raise ValueError("Worktree 操作 ID 非法")
    timestamp = record["timestamp"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("Worktree 事件时间必须是非负有限数")
    if not isinstance(record["task_id"], str) or TASK_ID_PATTERN.fullmatch(record["task_id"]) is None:
        raise ValueError("Worktree 事件 Task ID 非法")
    validate_worktree_name(record["worktree"])
    if record["stage"] not in ("create", "bind", "keep", "remove", "task_completed") or record["outcome"] not in ("started", "succeeded", "failed"):
        raise ValueError("Worktree 事件阶段或结果非法")
    if not isinstance(record["detail"], str) or len(record["detail"]) > WORKTREE_EVENT_DETAIL_MAX_CHARS:
        raise ValueError("Worktree 事件诊断必须是长度受限的字符串")


def _check_file(path: Path) -> None:
    """已有日志和锁必须是普通文件；拒绝悬空链接及 Windows 重解析点。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ValueError(f"事件文件不能是目录、链接或重解析点：{path}")


def append_worktree_event(
    layout: RepositoryLayout, *, operation_id: str, task_id: str,
    name: str, stage: str, outcome: str, detail: str = "",
) -> None:
    """追加一个事件；失败显式抛出，不能据此推断 Git 或 Task 操作未发生。

    每次锁内重写有界日志，复用原子存储；不是无限日志系统。记录成功也不
    代表业务与日志组成事务。此入口会按需创建管理目录，须由已授权流程调用。
    """
    if not isinstance(layout, RepositoryLayout):
        raise TypeError("事件 layout 必须是 RepositoryLayout")
    if not isinstance(detail, str):
        raise TypeError("事件 detail 必须是字符串")
    record = dict(operation_id=operation_id, timestamp=time.time(), task_id=task_id,
                  worktree=name, stage=stage, outcome=outcome,
                  detail=detail[:WORKTREE_EVENT_DETAIL_MAX_CHARS])
    _validate_event(record)
    root = layout.repository_root
    _require_plain_directory_if_present(root)
    resolved_root = root.resolve(strict=True)
    if not (is_path_within_root(root, resolved_root) and is_path_within_root(resolved_root, root)):
        raise ValueError("事件仓库根发生重定向")
    directory = resolved_root / WORKTREE_DIRECTORY_NAME

    def check_directory() -> None:
        _require_plain_directory_if_present(directory)
        resolved = directory.resolve()
        if not (is_path_within_root(resolved, directory) and is_path_within_root(directory, resolved)):
            raise ValueError("事件管理目录发生重定向")

    check_directory()
    directory.mkdir(exist_ok=True)
    check_directory()
    lock_path = directory / WORKTREE_EVENT_LOCK_NAME
    _check_file(lock_path)
    with exclusive_file_lock(
        lock_path, allowed_root=directory,
        timeout_seconds=WORKTREE_EVENT_LOCK_TIMEOUT_SECONDS,
        poll_interval_seconds=WORKTREE_EVENT_LOCK_POLL_SECONDS,
    ):
        check_directory()
        path = directory / WORKTREE_EVENT_FILE_NAME
        _check_file(path)
        try:
            with path.open("rb") as stream:
                previous = stream.read(WORKTREE_EVENT_MAX_BYTES + 1)
        except FileNotFoundError:
            previous = b""
        if len(previous) > WORKTREE_EVENT_MAX_BYTES:
            raise ValueError("Worktree 事件文件已超限，请先人工归档")
        for line in previous.decode("utf-8").splitlines():
            _validate_event(json.loads(line))
        # 保留已有字节，不把半条 JSON 或空行当作可修复数据；无结尾换行的完整记录可接续。
        separator = b"\n" if previous and not previous.endswith(b"\n") else b""
        payload = previous + separator + (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > WORKTREE_EVENT_MAX_BYTES:
            raise ValueError("追加事件将超过文件上限，请先人工归档")
        atomic_replace_bytes(path, payload, allowed_root=directory)
