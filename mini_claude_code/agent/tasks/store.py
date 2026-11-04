"""Task 存储模块：负责安全路径、严格 JSON 解析、稳定扫描与原子持久化。

底层接口不自行取锁；并发运行时须通过 tasks.service 协调完整读取与修改。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from ..config import (
    MAX_TASK_FILE_BYTES,
    MAX_TASKS,
    TASK_ID_PATTERN,
    TASKS_DIR,
    WORKDIR,
)
from ..storage.atomic import atomic_replace_text
from .models import Task, task_from_record, task_to_record


def resolve_tasks_root(tasks_dir: Path = TASKS_DIR) -> Path:
    """校验 Task 目录类型、符号链接和工作区边界，但不创建目录。"""
    if not isinstance(tasks_dir, Path):
        raise TypeError("Task 目录必须是 pathlib.Path 类型")
    if tasks_dir.is_symlink():
        raise ValueError(f"Task 目录 {tasks_dir} 不能是符号链接")
    tasks_root = tasks_dir.resolve()
    if not tasks_root.is_relative_to(WORKDIR):
        raise ValueError(f"Task 目录 {tasks_root} 不在工作目录 {WORKDIR} 内")
    if tasks_root.exists() and not tasks_root.is_dir():
        raise ValueError(f"Task 路径 {tasks_root} 不是目录")
    return tasks_root


def generate_task_id() -> str:
    """生成由固定前缀和 UUID4 hex 组成的稳定 Task ID。"""
    task_id = f"task_{uuid.uuid4().hex}"
    if TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise RuntimeError("Harness 生成了格式无效的 Task ID")
    return task_id


def serialize_task(task: Task) -> str:
    """将 Task 序列化为确定性的 UTF-8 JSON 文本。"""
    if not isinstance(task, Task):
        raise TypeError("只能序列化 Task 实例")
    try:
        payload = json.dumps(
            task_to_record(task),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ) + "\n"
    except (TypeError, ValueError) as error:
        raise ValueError(f"无法序列化 Task {task.id!r}：{error}") from error
    payload_size = len(payload.encode("utf-8"))
    if payload_size > MAX_TASK_FILE_BYTES:
        raise ValueError(
            f"Task {task.id!r} 序列化后为 {payload_size} 字节，"
            f"超过上限 {MAX_TASK_FILE_BYTES} 字节"
        )
    return payload


def parse_task_json(text: str) -> Task:
    """严格解析一份大小受限的 Task JSON 文本。"""
    if not isinstance(text, str):
        raise TypeError("Task JSON 必须是字符串")
    payload_size = len(text.encode("utf-8"))
    if payload_size > MAX_TASK_FILE_BYTES:
        raise ValueError(
            f"Task JSON 大小为 {payload_size} 字节，超过上限 {MAX_TASK_FILE_BYTES} 字节"
        )
    try:
        record = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Task 文件不是合法 JSON：{error}") from error
    try:
        return task_from_record(record)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Task JSON 记录无效：{error}") from error


def task_file_path(task_id: str, tasks_dir: Path = TASKS_DIR) -> Path:
    """返回经过 Task ID、符号链接和目录边界检查的规范化文件路径。"""
    if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError("Task ID 必须符合 task_<32 位小写十六进制> 格式")
    tasks_root = resolve_tasks_root(tasks_dir)
    unresolved_path = tasks_root / f"{task_id}.json"
    if unresolved_path.is_symlink():
        raise ValueError(f"Task 文件 {unresolved_path.name!r} 不能是符号链接")
    resolved_path = unresolved_path.resolve()
    if not resolved_path.is_relative_to(tasks_root):
        raise ValueError(f"Task 文件路径 {resolved_path} 不在 Task 目录 {tasks_root} 内")
    return resolved_path


def _load_task_file(file_path: Path, expected_id: str) -> Task:
    """读取并解析一条已通过目录扫描或路径构造定位的 Task 文件。"""
    try:
        file_size = file_path.stat().st_size
    except OSError as error:
        raise ValueError(
            f"无法读取 Task 文件状态 {file_path.name!r}：{type(error).__name__}：{error}"
        ) from error
    if file_size > MAX_TASK_FILE_BYTES:
        raise ValueError(
            f"Task 文件 {file_path.name!r} 大小为 {file_size} 字节，"
            f"超过上限 {MAX_TASK_FILE_BYTES} 字节"
        )
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(
            f"无法读取 UTF-8 Task 文件 {file_path.name!r}：{type(error).__name__}：{error}"
        ) from error
    try:
        task = parse_task_json(text)
    except ValueError as error:
        raise ValueError(f"解析 Task 文件 {file_path.name!r} 失败：{error}") from error
    if task.id != expected_id:
        raise ValueError(
            f"Task 文件名中的 ID {expected_id!r} 与记录 ID {task.id!r} 不一致"
        )
    return task


def save_task(
    task: Task,
    tasks_dir: Path = TASKS_DIR,
    *,
    replace_existing: bool,
) -> Path:
    """创建或替换一条 Task；创建与更新语义必须由调用者显式选择。"""
    if not isinstance(task, Task):
        raise TypeError("只能持久化 Task 实例")
    if type(replace_existing) is not bool:
        raise TypeError("replace_existing 必须是布尔值")

    tasks_root = resolve_tasks_root(tasks_dir)
    target_path = task_file_path(task.id, tasks_dir)
    target_exists = target_path.exists()
    if replace_existing:
        if not target_exists:
            raise FileNotFoundError(f"要更新的 Task {task.id!r} 不存在")
    else:
        if target_exists:
            raise FileExistsError(f"要创建的 Task {task.id!r} 已经存在")
        if len(scan_tasks(tasks_dir)) >= MAX_TASKS:
            raise ValueError(f"Task 数量已经达到上限 {MAX_TASKS}，无法继续创建")

    payload = serialize_task(task)
    return atomic_replace_text(
        target_path,
        payload,
        allowed_root=tasks_root,
        replace_existing=replace_existing,
    )


def load_task(task_id: str, tasks_dir: Path = TASKS_DIR) -> Task:
    """按 ID 加载一条 Task，并验证文件名与记录 ID 一致。"""
    file_path = task_file_path(task_id, tasks_dir)
    if not file_path.exists():
        raise FileNotFoundError(f"Task {task_id!r} 不存在")
    if not file_path.is_file():
        raise ValueError(f"Task 路径 {file_path} 不是文件")
    return _load_task_file(file_path, task_id)


def scan_tasks(tasks_dir: Path = TASKS_DIR) -> dict[str, Task]:
    """按文件名稳定扫描并完整校验 Task 注册表；任何损坏都会使扫描失败。"""
    tasks_root = resolve_tasks_root(tasks_dir)
    if not tasks_root.exists():
        return {}

    try:
        directory_entries = sorted(
            tasks_root.iterdir(),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as error:
        raise ValueError(
            f"无法扫描 Task 目录 {tasks_root}：{type(error).__name__}：{error}"
        ) from error

    task_entries: list[tuple[str, Path]] = []
    for entry in directory_entries:
        if not entry.name.startswith("task_") or entry.suffix.casefold() != ".json":
            continue
        task_id = entry.stem
        if TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise ValueError(f"Task 文件名 {entry.name!r} 不符合稳定 ID 格式")
        if entry.is_symlink():
            raise ValueError(f"Task 文件 {entry.name!r} 不能是符号链接")
        resolved_entry = entry.resolve()
        if not resolved_entry.is_relative_to(tasks_root):
            raise ValueError(f"Task 文件 {entry.name!r} 的实际路径不在 Task 目录内")
        if not resolved_entry.is_file():
            raise ValueError(f"Task 条目 {entry.name!r} 不是普通文件")
        task_entries.append((task_id, resolved_entry))

    if len(task_entries) > MAX_TASKS:
        raise ValueError(
            f"Task 文件数量为 {len(task_entries)}，超过上限 {MAX_TASKS}"
        )

    registry: dict[str, Task] = {}
    for task_id, file_path in task_entries:
        task = _load_task_file(file_path, task_id)
        if task_id in registry:
            raise ValueError(f"Task 注册表中存在重复 ID {task_id!r}")
        registry[task_id] = task
    return registry
