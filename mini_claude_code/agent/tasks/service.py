"""Task 服务模块：组合图校验与存储层，以列表级文件锁保护跨进程读改写事务。"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from ..config import (
    TASK_ID_PATTERN,
    TASK_LOCK_POLL_INTERVAL_SECONDS,
    TASK_LOCK_TIMEOUT_SECONDS,
    TASKS_DIR,
)
from ..storage.locking import exclusive_file_lock
from ..worktrees.names import validate_worktree_name
from .graph import blocking_dependency_ids, validate_task_graph
from .models import Task
from .store import generate_task_id, resolve_tasks_root, save_task, scan_tasks


class TaskClaimConflictError(ValueError):
    """任务已非 pending 或依赖尚未完成；可跳过候选，不代表存储故障。"""


class TaskClaimStoppedError(RuntimeError):
    """Harness 已要求停止，本次认领尚未写入磁盘。"""


def _validated_registry(tasks_dir: Path) -> dict[str, Task]:
    """从磁盘重新扫描 Task，并在业务操作前验证完整依赖图。"""
    registry = scan_tasks(tasks_dir)
    validate_task_graph(registry)
    _validate_worktree_bindings(registry)
    return registry


def _validate_worktree_bindings(registry: dict[str, Task]) -> None:
    """同一任务板不允许共享绑定；保守忽略大小写，避免 Windows 同路径别名。"""
    bindings: dict[str, str] = {}
    for task in registry.values():
        if task.worktree is None:
            continue
        key = task.worktree.casefold()
        previous = bindings.get(key)
        if previous is not None:
            raise ValueError(f"Worktree {task.worktree!r} 被 Task {previous!r} 和 {task.id!r} 重复绑定")
        bindings[key] = task.id


@contextmanager
def _task_mutation_lock(tasks_dir: Path) -> Iterator[None]:
    """使用同一列表级锁协调读写，避免 Windows 读取句柄阻止原子替换。"""
    tasks_root = resolve_tasks_root(tasks_dir)
    lock_path = tasks_root / ".lock"
    with exclusive_file_lock(
        lock_path,
        allowed_root=tasks_root,
        timeout_seconds=TASK_LOCK_TIMEOUT_SECONDS,
        poll_interval_seconds=TASK_LOCK_POLL_INTERVAL_SECONDS,
    ):
        yield


def _require_task(registry: dict[str, Task], task_id: str) -> Task:
    """校验 Task ID 并从已验证注册表中取得对应任务。"""
    if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError("Task ID 必须符合 task_<32 位小写十六进制> 格式")
    task = registry.get(task_id)
    if task is None:
        raise FileNotFoundError(f"Task {task_id!r} 不存在")
    return task


@contextmanager
def guard_task_worktree(task_id: str, tasks_dir: Path = TASKS_DIR) -> Iterator[Task]:
    """在同一任务修改锁内交付绑定快照，供工作树收尾使用。

    调用方可执行有超时限制的 Git 操作，但不能在此范围内调用再次取锁的
    Task 服务读写接口。锁顺序为 Task → Worktree 事件锁，不反向嵌套。
    """
    with _task_mutation_lock(tasks_dir):
        task = _require_task(_validated_registry(tasks_dir), task_id)
        if task.worktree is None:
            raise ValueError("Task 尚未绑定 Worktree")
        yield task


def _normalize_blocked_by(
    blocked_by: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """将公开接口接受的依赖序列转换为冻结快照。"""
    if not isinstance(blocked_by, (list, tuple)):
        raise TypeError("blocked_by 必须是 Task ID 列表或元组")
    if any(not isinstance(dependency_id, str) for dependency_id in blocked_by):
        raise ValueError("blocked_by 只能包含字符串 Task ID")
    return tuple(blocked_by)


def create_task(
    subject: str,
    description: str = "",
    blocked_by: list[str] | tuple[str, ...] | None = None,
    tasks_dir: Path = TASKS_DIR,
) -> Task:
    """创建一条 pending Task，并拒绝缺失依赖或无效图。"""
    with _task_mutation_lock(tasks_dir):
        registry = _validated_registry(tasks_dir)
        dependencies = () if blocked_by is None else _normalize_blocked_by(blocked_by)
        task = Task(
            id=generate_task_id(),
            subject=subject,
            description=description,
            blocked_by=dependencies,
        )
        candidate_registry = dict(registry)
        candidate_registry[task.id] = task
        validate_task_graph(candidate_registry)
        save_task(task, tasks_dir, replace_existing=False)
        return task


def bind_task_to_worktree(
    task_id: str, worktree_name: str, tasks_dir: Path = TASKS_DIR,
) -> Task:
    """锁内绑定 pending Task，仅保存关联，不认领、不授予权限、不验证 Git。

    上层 Worktree 服务须先核验真实目录与仓库归属。本接口只保证同一任务板
    的关联唯一，不能协调不同任务板；绑定失败不能据此自动删除工作树。
    """
    worktree_name = validate_worktree_name(worktree_name)
    with _task_mutation_lock(tasks_dir):
        registry = _validated_registry(tasks_dir)
        task = _require_task(registry, task_id)
        if task.status != "pending":
            raise ValueError("只有 pending Task 可以绑定 Worktree")
        if task.worktree == worktree_name:
            return task  # 相同 pending 绑定幂等，不重复写盘
        if task.worktree is not None:
            raise ValueError(f"Task 已绑定 {task.worktree!r}，不能替换为 {worktree_name!r}")
        candidate = replace(task, worktree=worktree_name)
        candidate_registry = dict(registry)
        candidate_registry[task.id] = candidate
        _validate_worktree_bindings(candidate_registry)
        validate_task_graph(candidate_registry)
        save_task(candidate, tasks_dir, replace_existing=True)
        return candidate


def get_task(task_id: str, tasks_dir: Path = TASKS_DIR) -> Task:
    """从完整且合法的 Task 图中取得一条任务。"""
    with _task_mutation_lock(tasks_dir):
        registry = _validated_registry(tasks_dir)
        return _require_task(registry, task_id)


def list_tasks(tasks_dir: Path = TASKS_DIR) -> tuple[Task, ...]:
    """按 Store 的稳定顺序返回完整且合法的 Task 快照。"""
    with _task_mutation_lock(tasks_dir):
        return tuple(_validated_registry(tasks_dir).values())


def update_task(
    task_id: str,
    *,
    subject: str | None = None,
    description: str | None = None,
    blocked_by: list[str] | tuple[str, ...] | None = None,
    tasks_dir: Path = TASKS_DIR,
) -> Task:
    """仅更新 pending Task 的描述字段或依赖，并验证候选 DAG。"""
    if subject is None and description is None and blocked_by is None:
        raise ValueError("update_task 至少需要提供一个待修改字段")

    with _task_mutation_lock(tasks_dir):
        registry = _validated_registry(tasks_dir)
        task = _require_task(registry, task_id)
        if task.status != "pending":
            raise ValueError(
                f"Task {task.id!r} 当前状态为 {task.status!r}，只有 pending Task 可以更新"
            )

        candidate = replace(
            task,
            subject=task.subject if subject is None else subject,
            description=task.description if description is None else description,
            blocked_by=(
                task.blocked_by
                if blocked_by is None
                else _normalize_blocked_by(blocked_by)
            ),
        )
        if candidate == task:
            raise ValueError(f"Task {task.id!r} 的更新没有产生任何变化")

        candidate_registry = dict(registry)
        candidate_registry[task.id] = candidate
        validate_task_graph(candidate_registry)
        save_task(candidate, tasks_dir, replace_existing=True)
        return candidate


def claim_task(
    task_id: str,
    owner: str,
    tasks_dir: Path = TASKS_DIR,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> Task:
    """在文件锁内重新校验依赖和状态，将 pending Task 原子认领为 in_progress。"""
    if should_stop is not None and not callable(should_stop):
        raise TypeError("claim_task should_stop 必须是可调用对象或 None")
    with _task_mutation_lock(tasks_dir):
        if should_stop is not None and should_stop():
            raise TaskClaimStoppedError("等待 Task 锁期间 Harness 请求停止，未执行认领")
        registry = _validated_registry(tasks_dir)
        task = _require_task(registry, task_id)
        if task.status != "pending":
            raise TaskClaimConflictError(
                f"Task {task.id!r} 当前状态为 {task.status!r}，不能再次认领"
            )
        blocking_ids = blocking_dependency_ids(task, registry)
        if blocking_ids:
            raise TaskClaimConflictError(
                f"Task {task.id!r} 仍被以下任务阻塞：{', '.join(blocking_ids)}"
            )

        claimed_task = replace(task, status="in_progress", owner=owner)
        candidate_registry = dict(registry)
        candidate_registry[task.id] = claimed_task
        validate_task_graph(candidate_registry)
        if should_stop is not None and should_stop():
            raise TaskClaimStoppedError("Harness 请求停止，未写入认领结果")
        save_task(claimed_task, tasks_dir, replace_existing=True)
        return claimed_task


def release_task(
    task_id: str,
    owner: str,
    tasks_dir: Path = TASKS_DIR,
) -> Task:
    """由当前 owner 在锁内释放未完成任务，保留描述和依赖，恢复为 pending。

    释放只撤销任务所有权，不撤销工具副作用，也不是租约或崩溃恢复。
    状态不符、身份不符及存储失败均显式抛出；重复释放不会伪装成成功。
    """
    with _task_mutation_lock(tasks_dir):
        registry = _validated_registry(tasks_dir)
        task = _require_task(registry, task_id)
        if task.status != "in_progress":
            raise ValueError(
                f"Task {task.id!r} 当前状态为 {task.status!r}，只有 in_progress Task 可以释放"
            )
        if not isinstance(owner, str) or not owner.strip() or owner != owner.strip():
            raise ValueError("释放 Task 的 owner 必须是没有首尾空白的非空字符串")
        if task.owner != owner:
            raise ValueError(
                f"Task {task.id!r} 由 {task.owner!r} 认领，owner {owner!r} 无权释放"
            )

        released_task = replace(task, status="pending", owner=None, result=None)
        candidate_registry = dict(registry)
        candidate_registry[task.id] = released_task
        validate_task_graph(candidate_registry)
        save_task(released_task, tasks_dir, replace_existing=True)
        return released_task


def complete_task(
    task_id: str,
    owner: str,
    result: str,
    tasks_dir: Path = TASKS_DIR,
) -> tuple[Task, tuple[str, ...]]:
    """由当前 owner 完成 in_progress Task，并返回刚刚被解锁的下游 ID。"""
    with _task_mutation_lock(tasks_dir):
        registry = _validated_registry(tasks_dir)
        task = _require_task(registry, task_id)
        if task.status != "in_progress":
            raise ValueError(
                f"Task {task.id!r} 当前状态为 {task.status!r}，只有 in_progress Task 可以完成"
            )
        if not isinstance(owner, str) or not owner.strip() or owner != owner.strip():
            raise ValueError("完成 Task 的 owner 必须是没有首尾空白的非空字符串")
        if task.owner != owner:
            raise ValueError(
                f"Task {task.id!r} 由 {task.owner!r} 认领，owner {owner!r} 无权完成"
            )

        completed_task = replace(task, status="completed", result=result)
        candidate_registry = dict(registry)
        candidate_registry[task.id] = completed_task
        validate_task_graph(candidate_registry)

        newly_unblocked_ids = tuple(
            sorted(
                candidate.id
                for candidate in candidate_registry.values()
                if candidate.status == "pending"
                and task.id in candidate.blocked_by
                and not blocking_dependency_ids(candidate, candidate_registry)
            )
        )
        save_task(completed_task, tasks_dir, replace_existing=True)
        return completed_task, newly_unblocked_ids
