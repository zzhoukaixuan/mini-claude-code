"""Task 图模块：校验持久任务注册表的一致性、依赖完整性与 DAG 不变量。"""

from __future__ import annotations

import heapq

from .models import Task


def validate_task_graph(registry: dict[str, Task]) -> None:
    """使用稳定的 Kahn 拓扑排序校验 Task 注册表及其依赖图。"""
    if not isinstance(registry, dict):
        raise TypeError("Task 注册表必须是字典")

    for task_id, task in registry.items():
        if not isinstance(task_id, str):
            raise ValueError("Task 注册表的键必须是字符串 ID")
        if not isinstance(task, Task):
            raise ValueError(f"Task 注册表条目 {task_id!r} 不是 Task 实例")
        if task_id != task.id:
            raise ValueError(
                f"Task 注册表键 {task_id!r} 与记录 ID {task.id!r} 不一致"
            )

    indegree: dict[str, int] = {task_id: 0 for task_id in registry}  # 入度表
    downstream: dict[str, list[str]] = {task_id: [] for task_id in registry}  # 下游依赖表
    for task_id in sorted(registry):
        task = registry[task_id]
        indegree[task_id] = len(task.blocked_by)
        for dependency_id in task.blocked_by:
            if dependency_id not in registry:
                raise ValueError(
                    f"Task {task_id!r} 依赖不存在的 Task {dependency_id!r}"
                )
            downstream[dependency_id].append(task_id)

    ready = [task_id for task_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    consumed_count = 0
    while ready:
        task_id = heapq.heappop(ready)
        consumed_count += 1
        for downstream_id in sorted(downstream[task_id]):
            indegree[downstream_id] -= 1
            if indegree[downstream_id] == 0:
                heapq.heappush(ready, downstream_id)

    if consumed_count != len(registry):
        cycle_task_ids = tuple(
            sorted(task_id for task_id, degree in indegree.items() if degree > 0)
        )
        raise ValueError(
            "Task 图存在循环依赖，无法完成拓扑排序；涉及 Task："
            + ", ".join(cycle_task_ids)
        )


def blocking_dependency_ids(
    task: Task,
    registry: dict[str, Task],
) -> tuple[str, ...]:
    """按声明顺序返回尚未 completed 的直接上游 Task ID。"""
    if not isinstance(task, Task):
        raise TypeError("依赖检查对象必须是 Task 实例")
    if not isinstance(registry, dict):
        raise TypeError("Task 注册表必须是字典")
    registered_task = registry.get(task.id)
    if registered_task is None:
        raise ValueError(f"Task {task.id!r} 不在注册表中")
    if registered_task != task:
        raise ValueError(f"Task {task.id!r} 与注册表中的任务快照不一致")

    blocking_ids: list[str] = []
    for dependency_id in task.blocked_by:
        dependency = registry.get(dependency_id)
        if dependency is None:
            raise ValueError(
                f"Task {task.id!r} 依赖不存在的 Task {dependency_id!r}"
            )
        if not isinstance(dependency, Task):
            raise ValueError(f"依赖条目 {dependency_id!r} 不是 Task 实例")
        if dependency.status != "completed":
            blocking_ids.append(dependency_id)
    return tuple(blocking_ids)
