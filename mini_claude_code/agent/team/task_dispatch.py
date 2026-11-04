"""团队 Task 分派：按成员白名单发现任务，以固定身份认领、完成和释放任务。

本模块不启动线程、不调用模型；由 Runner 的空闲循环与工作收尾显式调用。
候选只是读取时的快照，所有权必须以 claim_task 在锁内成功返回为准。
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from ..config import TASKS_DIR
from ..tasks.graph import blocking_dependency_ids
from ..tasks.models import Task
from ..tasks.service import (
    TaskClaimConflictError,
    TaskClaimStoppedError,
    claim_task,
    complete_task,
    list_tasks,
    release_task,
)
from .models import TeammateSpec


def find_ready_tasks(
    spec: TeammateSpec, *, tasks_dir: Path = TASKS_DIR,
) -> tuple[Task, ...]:
    """按授权 ID 顺序返回就绪候选；不加占用、不改变磁盘任务。

    依赖可以在白名单外，但仍须已完成。空白名单不扫描任务板；白名单中
    不存在的任务显式报错，避免将配置错误误报为暂时无工作。
    """
    if not isinstance(spec, TeammateSpec):
        raise TypeError("任务发现 spec 必须是 TeammateSpec")
    if not spec.task_ids:
        return ()
    registry = {task.id: task for task in list_tasks(tasks_dir)}
    missing_ids = [task_id for task_id in spec.task_ids if task_id not in registry]
    if missing_ids:
        raise FileNotFoundError("成员授权的 Task 不存在：" + ", ".join(missing_ids))
    return tuple(
        registry[task_id]
        for task_id in spec.task_ids
        if registry[task_id].status == "pending"
        and registry[task_id].owner is None
        and not blocking_dependency_ids(registry[task_id], registry)
    )


def teammate_task_owner(spec: TeammateSpec) -> str:
    """由冻结的实例 ID 生成 owner；同名新成员不能冒用旧实例的任务所有权。

    不拼接最长可达 64 字符的显示名，保持 owner 小于现有 100 字符上限。
    同一实例的所有任务共用此标识；它不是逐次认领令牌或租约，也不是认证密钥。
    """
    if not isinstance(spec, TeammateSpec):
        raise TypeError("Task owner 的 spec 必须是 TeammateSpec")
    return f"teammate:{spec.instance_id}"


def try_claim_ready_task(
    spec: TeammateSpec, *, tasks_dir: Path = TASKS_DIR,
    should_stop: Callable[[], bool] | None = None,
) -> Task | None:
    """尝试认领一个授权候选；只跳过明确的状态/依赖竞争，不吞存储异常。

    成功返回锁内写入后的 Task；None 表示无可认领候选或 Harness 已停止。
    该接口会写磁盘，后续调用方须先满足生命周期、控制消息和预算边界。
    """
    owner = teammate_task_owner(spec)
    if should_stop is not None and not callable(should_stop):
        raise TypeError("should_stop 必须是可调用对象或 None")
    if should_stop is not None and should_stop():
        return None
    for candidate in find_ready_tasks(spec, tasks_dir=tasks_dir):
        if should_stop is not None and should_stop():
            return None
        try:
            return claim_task(candidate.id, owner, tasks_dir=tasks_dir, should_stop=should_stop)
        except TaskClaimStoppedError:
            return None
        except TaskClaimConflictError:
            # 扫描之后可能被其他成员抢先认领，或前置依赖被更新；重新检查由服务负责。
            continue
    return None


def complete_teammate_task(
    spec: TeammateSpec,
    task_id: str,
    result: str,
    *,
    tasks_dir: Path = TASKS_DIR,
) -> tuple[Task, tuple[str, ...]]:
    """以成员固定身份完成授权 Task，返回完成快照和刚被解锁的下游 ID。

    白名单只表示授权范围，不证明所有权；服务会在锁内重新验证状态和 owner。
    此处不投递邮箱；后续通知失败不得把已完成 Task 重新释放。
    """
    owner = teammate_task_owner(spec)
    if not isinstance(task_id, str) or task_id not in spec.task_ids:
        raise ValueError(f"Task {task_id!r} 不在成员 {spec.name!r} 的授权范围内")
    return complete_task(task_id, owner, result, tasks_dir=tasks_dir)


def release_teammate_task(
    spec: TeammateSpec,
    task_id: str,
    *,
    tasks_dir: Path = TASKS_DIR,
) -> Task:
    """以成员固定身份释放授权 Task；失败向上传播，不能假装清理成功。

    仅供 Harness 在未完成工作的异常或停止路径显式调用，不暴露为模型工具。
    实例身份隔离不等于租约：强杀进程后的陈旧认领仍需人工确认并恢复。
    """
    owner = teammate_task_owner(spec)
    if not isinstance(task_id, str) or task_id not in spec.task_ids:
        raise ValueError(f"Task {task_id!r} 不在成员 {spec.name!r} 的授权范围内")
    return release_task(task_id, owner, tasks_dir=tasks_dir)
