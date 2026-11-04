"""工作树收尾服务：显式保留或在任务锁内安全移除，保留分支与 Task 历史，不自动重试。"""

from pathlib import Path
from typing import cast
from uuid import uuid4

from ..config import WORKTREE_REMOVE_TIMEOUT_SECONDS
from ..tasks.service import guard_task_worktree
from .events import append_worktree_event
from .git import run_git
from .models import RepositoryLayout, WorktreeTarget
from .service import _same_path, inspect_worktree, _query_worktree_status


class WorktreeLifecycleError(RuntimeError):
    """携带真实移除阶段，区分未执行、结果不确定及已移除但审计失败。"""

    def __init__(
        self, operation_id: str, action: str, stage: str, target: WorktreeTarget,
        removed: bool | None, error: Exception, audit_error: Exception | None = None,
    ) -> None:
        self.operation_id = operation_id  # 同一收尾操作的事件关联 ID
        self.action = action  # keep 或 remove
        self.stage = stage  # 失败阶段，不用于推断磁盘结果
        self.target = target  # 供人工核验的目标目录与分支
        self.removed = removed  # False 未执行移除，None 结果不确定，True 已核验移除
        self.audit_error = audit_error  # 记录失败事件时遇到的第二个错误
        state = "已核验移除" if removed is True else "移除结果不确定" if removed is None else "未执行移除"
        extra = f"；失败审计也未保存：{type(audit_error).__name__}：{audit_error}" if audit_error else ""
        super().__init__(
            f"Worktree {action} 操作 {operation_id} 在 {stage} 失败："
            f"{type(error).__name__}：{error}；{state}{extra}。"
            f"目标 {str(target.root)!r}，分支 {target.branch!r}；未自动回滚或重试。"
        )


def keep_task_worktree(
    layout: RepositoryLayout, task_id: str, *, tasks_dir: Path,
) -> WorktreeTarget:
    """核对绑定并记录本次保留决定；不锁定 Git、不提交，也不永久禁止后续移除。"""
    with guard_task_worktree(task_id, tasks_dir) as task:
        target = inspect_worktree(layout, cast(str, task.worktree))
        operation_id = "wt_" + uuid4().hex
        try:
            append_worktree_event(
                layout, operation_id=operation_id, task_id=task.id, name=target.name,
                stage="keep", outcome="succeeded", detail="显式保留目录与分支；不提交、不合并、不修改任务状态",
            )
        except Exception as error:
            # 保留本身无需磁盘变更；日志失败不能声称保留决定已记录。
            raise WorktreeLifecycleError(operation_id, "keep", "保留审计", target, False, error) from error
        return target


def _verify_removed(layout: RepositoryLayout, target: WorktreeTarget) -> None:
    """核验目录及登记均消失；用 lstat 识别悬空链接，不把权限错误当成不存在。"""
    try:
        target.root.lstat()
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("Git 返回后目标目录仍存在，不能确认移除完成")
    output = run_git(["worktree", "list", "--porcelain", "-z"], cwd=layout.repository_root)
    for field in output.split("\0"):
        if field.startswith("worktree ") and _same_path(Path(field[len("worktree "):]), target.root):
            raise RuntimeError("Git 返回后工作树登记仍存在，不能确认移除完成")


def remove_task_worktree(
    layout: RepositoryLayout, task_id: str, *, tasks_dir: Path,
) -> WorktreeTarget:
    """只移除 completed 任务的干净工作树；保留分支、任务结果及历史绑定。

    Task 锁覆盖检查、Git 移除及审计，阻止同任务板的协作修改；不协调外部
    编辑器或手工 Git。禁用 force，不自动重试，失败后保留已知阶段供人工核验。
    """
    with guard_task_worktree(task_id, tasks_dir) as task:
        if task.status != "completed":
            raise ValueError("仅允许移除 completed Task 的工作树，未完成任务请保留")
        # guard 已校验完整任务板和绑定唯一性；复用锁内 Task，避免嵌套取锁。
        status = _query_worktree_status(layout, cast(str, task.worktree), tasks=(task,))
        if status.removal_blockers:
            raise ValueError("拒绝移除 Worktree：" + "；".join(status.removal_blockers))
        target = status.target
        operation_id = "wt_" + uuid4().hex
        removed: bool | None = False
        stage = "开始审计"

        def record(outcome: str, detail: str = "") -> None:
            append_worktree_event(
                layout, operation_id=operation_id, task_id=task.id, name=target.name,
                stage="remove", outcome=outcome, detail=detail,
            )

        try:
            record("started")  # 审计不可写时，不进入删除
            stage = "Git 移除"
            removed = None  # 从发起命令起，异常/超时都不能断言没有副作用
            run_git(
                ["worktree", "remove", "--", str(target.root)],
                cwd=layout.repository_root, timeout=WORKTREE_REMOVE_TIMEOUT_SECONDS,
            )
            stage = "移除后核验"
            _verify_removed(layout, target)
            removed = True
            stage = "成功审计"
            record("succeeded", "已核验目录与登记移除；保留 Git 分支、Task 结果和历史绑定")
        except Exception as error:
            audit_error = None
            # 日志本身失败不递归记录；业务失败则尽力留下失败事件。
            if stage in ("Git 移除", "移除后核验"):
                try:
                    record("failed", f"{stage}：{type(error).__name__}：{error}；移除结果需人工核验")
                except Exception as logging_error:
                    audit_error = logging_error
            raise WorktreeLifecycleError(
                operation_id, "remove", stage, target, removed, error, audit_error,
            ) from error
        return target
