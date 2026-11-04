"""Task 领域模型：定义单条持久任务的字段、局部不变量与 JSON 记录边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from ..worktrees.names import validate_worktree_name

from ..config import (
    MAX_TASK_DEPENDENCIES,
    MAX_TASK_DESCRIPTION_LENGTH,
    MAX_TASK_OWNER_LENGTH,
    MAX_TASK_RESULT_LENGTH,
    MAX_TASK_SUBJECT_LENGTH,
    TASK_ID_PATTERN,
    VALID_TASK_STATUSES,
    TaskStatus,
)


@dataclass(frozen=True)
class Task:
    id: str  # 由 Harness 生成且可安全用作文件名的稳定任务 ID
    subject: str  # 适合列表展示的简短任务标题
    description: str = ""  # 恢复任务时所需的完整目标与约束
    status: TaskStatus = "pending"  # pending → in_progress → completed 生命周期状态
    owner: str | None = None  # 当前认领任务的 Agent 标识；未认领时为 None
    blocked_by: tuple[str, ...] = ()  # 必须先完成的直接上游 Task ID 快照
    result: str | None = None  # 完成后的可恢复结果；未完成时为 None
    worktree: str | None = None  # 已绑定的工作树名称；仅为关联，不证明目录存在或已获执行权限

    def __post_init__(self) -> None:
        """校验单条 Task 自身可判断的字段约束与状态不变量。"""
        if self.worktree is not None:
            validate_worktree_name(self.worktree)
        if not isinstance(self.id, str) or TASK_ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError("Task ID 必须符合 task_<32 位小写十六进制> 格式")
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("Task subject 必须是非空字符串")
        if self.subject != self.subject.strip():
            raise ValueError("Task subject 不能包含首尾空白")
        if len(self.subject) > MAX_TASK_SUBJECT_LENGTH:
            raise ValueError(f"Task subject 不能超过 {MAX_TASK_SUBJECT_LENGTH} 个字符")
        if not isinstance(self.description, str):
            raise ValueError("Task description 必须是字符串")
        if len(self.description) > MAX_TASK_DESCRIPTION_LENGTH:
            raise ValueError(f"Task description 不能超过 {MAX_TASK_DESCRIPTION_LENGTH} 个字符")
        if self.status not in VALID_TASK_STATUSES:
            raise ValueError(f"未知的 Task 状态 {self.status!r}")

        if self.owner is not None:
            if not isinstance(self.owner, str) or not self.owner.strip():
                raise ValueError("Task owner 必须是非空字符串或 None")
            if self.owner != self.owner.strip():
                raise ValueError("Task owner 不能包含首尾空白")
            if len(self.owner) > MAX_TASK_OWNER_LENGTH:
                raise ValueError(f"Task owner 不能超过 {MAX_TASK_OWNER_LENGTH} 个字符")

        if not isinstance(self.blocked_by, tuple):
            raise ValueError("Task blocked_by 必须是 Task ID 元组")
        if len(self.blocked_by) > MAX_TASK_DEPENDENCIES:
            raise ValueError(f"Task 直接依赖不能超过 {MAX_TASK_DEPENDENCIES} 个")
        seen_dependencies: set[str] = set()
        for dependency_id in self.blocked_by:
            if not isinstance(dependency_id, str) or TASK_ID_PATTERN.fullmatch(dependency_id) is None:
                raise ValueError("Task blocked_by 中存在格式无效的 Task ID")
            if dependency_id == self.id:
                raise ValueError("Task 不能直接依赖自身")
            if dependency_id in seen_dependencies:
                raise ValueError(f"Task blocked_by 中存在重复依赖 {dependency_id!r}")
            seen_dependencies.add(dependency_id)

        if self.result is not None and not isinstance(self.result, str):
            raise ValueError("Task result 必须是字符串或 None")
        if isinstance(self.result, str) and len(self.result) > MAX_TASK_RESULT_LENGTH:
            raise ValueError(f"Task result 不能超过 {MAX_TASK_RESULT_LENGTH} 个字符")

        if self.status == "pending":
            if self.owner is not None or self.result is not None:
                raise ValueError("pending Task 的 owner 和 result 必须为 None")
        elif self.status == "in_progress":
            if self.owner is None:
                raise ValueError("in_progress Task 必须具有 owner")
            if self.result is not None:
                raise ValueError("in_progress Task 的 result 必须为 None")
        elif self.status == "completed":
            if self.owner is None:
                raise ValueError("completed Task 必须保留 owner")
            if self.result is None:
                raise ValueError("completed Task 必须具有字符串 result")
            if not self.result.strip():
                raise ValueError("completed Task 的 result 不能为空或只包含空白")


def task_to_record(task: Task) -> dict[str, object]:
    """将已校验的 Task 转换成使用稳定磁盘字段名的 JSON 兼容记录。"""
    if not isinstance(task, Task):
        raise TypeError("只能序列化 Task 实例")
    return {
        "id": task.id,
        "subject": task.subject,
        "description": task.description,
        "status": task.status,
        "owner": task.owner,
        "blockedBy": list(task.blocked_by),
        "result": task.result,
        "worktree": task.worktree,
    }


def task_from_record(record: object) -> Task:
    """严格解析记录；仅兼容旧记录缺少 worktree，其他必填与未知字段规则不变。"""
    if not isinstance(record, dict):
        raise ValueError("Task 记录必须是 JSON 对象")

    expected_fields = {
        "id",
        "subject",
        "description",
        "status",
        "owner",
        "blockedBy",
        "result",
    }
    actual_fields = set(record)
    missing_fields = expected_fields - actual_fields
    unknown_fields = actual_fields - expected_fields - {"worktree"}
    if missing_fields:
        formatted_fields = ", ".join(sorted(missing_fields))
        raise ValueError(f"Task 记录缺少字段：{formatted_fields}")
    if unknown_fields:
        formatted_fields = ", ".join(sorted((repr(field) for field in unknown_fields)))
        raise ValueError(f"Task 记录包含未知字段：{formatted_fields}")

    task_id = record["id"]
    subject = record["subject"]
    description = record["description"]
    status = record["status"]
    owner = record["owner"]
    blocked_by = record["blockedBy"]
    result = record["result"]
    worktree = record.get("worktree")  # 旧记录按未绑定处理，不自动回写磁盘

    if not isinstance(task_id, str):
        raise ValueError("Task 记录的 id 必须是字符串")
    if not isinstance(subject, str):
        raise ValueError("Task 记录的 subject 必须是字符串")
    if not isinstance(description, str):
        raise ValueError("Task 记录的 description 必须是字符串")
    if not isinstance(status, str):
        raise ValueError("Task 记录的 status 必须是字符串")
    if owner is not None and not isinstance(owner, str):
        raise ValueError("Task 记录的 owner 必须是字符串或 null")
    if not isinstance(blocked_by, list):
        raise ValueError("Task 记录的 blockedBy 必须是数组")
    if any(not isinstance(dependency_id, str) for dependency_id in blocked_by):
        raise ValueError("Task 记录的 blockedBy 只能包含字符串 Task ID")
    if result is not None and not isinstance(result, str):
        raise ValueError("Task 记录的 result 必须是字符串或 null")
    if worktree is not None and not isinstance(worktree, str):
        raise ValueError("Task 记录的 worktree 必须是字符串或 null")

    blocked_by_ids = cast(tuple[str, ...], tuple(blocked_by))

    return Task(
        id=task_id,
        subject=subject,
        description=description,
        status=cast(TaskStatus, status),
        owner=owner,
        blocked_by=blocked_by_ids,
        result=result,
        worktree=worktree,
    )
