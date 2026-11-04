"""Worktree 工具适配：封装创建、状态查询、保留与移除，返回真实结果及部分失败诊断。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .paths import discover_repository
from .service import TaskWorktreeOperationError, create_task_worktree, query_worktree_status
from .lifecycle import WorktreeLifecycleError, keep_task_worktree, remove_task_worktree


def _render(payload: dict) -> str:
    """统一输出 JSON；不把磁盘异常误报为成功。"""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def run_worktree_status(name: str, *, workspace: Path, tasks_dir: Path) -> str:
    """查询整个工作树的状态；无阻碍只是快照，不是删除授权。"""
    try:
        status = query_worktree_status(discover_repository(workspace), name, tasks_dir=tasks_dir)
        data = asdict(status)
        data["target"]["root"] = str(status.target.root)
        data["target"]["workspace"] = str(status.target.workspace)
        return _render({"ok": True, "action": "worktree_status", "status": data,
                        "note": "只读快照；移除时仍会重新核验。独有提交按可达性比较，不证明语义上未合并。"})
    except Exception as error:
        return _render({"ok": False, "action": "worktree_status",
                        "error_type": type(error).__name__, "error": str(error)})


def _run_worktree_finish(action: str, task_id: str, *, workspace: Path, tasks_dir: Path) -> str:
    """两个固定收尾入口共用结果格式；action 仅由下方包装函数指定。"""
    try:
        operation = keep_task_worktree if action == "worktree_keep" else remove_task_worktree
        target = operation(discover_repository(workspace), task_id, tasks_dir=tasks_dir)
        payload: dict[str, object] = {"ok": True, "action": action, "task_id": task_id,
                   "worktree": target.name, "branch": target.branch,
                   "root": str(target.root), "workspace": str(target.workspace),
                   "removed": action == "worktree_remove",
                   "note": ("已核验移除目录和登记；分支、Task 结果及历史绑定保留。"
                            if action == "worktree_remove" else "已记录本次保留决定；未自动提交、合并或永久锁定工作树。")}
    except Exception as error:
        payload = {"ok": False, "action": action, "error_type": type(error).__name__,
                   "error": str(error), "note": "未自动回滚或重试，请按实际阶段核验；不要把失败视为没有副作用。"}
        if isinstance(error, WorktreeLifecycleError):
            payload.update({"operation_id": error.operation_id, "stage": error.stage,
                            "removed": error.removed, "worktree": error.target.name,
                            "branch": error.target.branch, "root": str(error.target.root),
                            "audit_error": str(error.audit_error) if error.audit_error else None,
                            "state_note": "removed=true 表示已核验移除但后续审计失败；null 表示结果不确定；false 表示未执行移除。"})
    return _render(payload)


def run_worktree_keep(task_id: str, *, workspace: Path, tasks_dir: Path) -> str:
    """记录保留决定，不删除或改写工作文件。"""
    return _run_worktree_finish("worktree_keep", task_id, workspace=workspace, tasks_dir=tasks_dir)


def run_worktree_remove(task_id: str, *, workspace: Path, tasks_dir: Path) -> str:
    """调用安全移除服务；权限审批由 Lead 的 Hook 负责。"""
    return _run_worktree_finish("worktree_remove", task_id, workspace=workspace, tasks_dir=tasks_dir)


def run_worktree_create(
    task_id: str, name: str, *, workspace: Path, tasks_dir: Path,
) -> str:
    """供已通过权限 Hook 的 Lead 调用；目录依赖由运行时固定，不来自模型。

    创建、绑定、审计不是一个事务；ok=False 不表示所有副作用均未发生。
    """
    try:
        layout = discover_repository(workspace)
        target = create_task_worktree(layout, task_id, name, tasks_dir=tasks_dir)
        payload: dict[str, object] = {
            "ok": True,
            "action": "worktree_create",
            "task_id": task_id,
            "worktree": target.name,
            "branch": target.branch,
            "root": str(target.root),
            "workspace": str(target.workspace),
            "created": True,
            "bound": True,
            "note": "工作树已创建、核验并绑定；任务尚未执行，不代表队友获得写入权限。",
        }
    except Exception as error:
        payload = {
            "ok": False,
            "action": "worktree_create",
            "error_type": type(error).__name__,
            "error": str(error),
            "note": "未自动回滚或重试。失败可能留下目录、分支、绑定或审计记录；请核验后再决定下一步。",
        }
        if isinstance(error, TaskWorktreeOperationError):
            payload.update({
                "operation_id": error.operation_id,
                "stage": error.stage,
                "worktree": error.target.name,
                "branch": error.target.branch,
                "root": str(error.target.root),
                "workspace": str(error.target.workspace),
                "created": error.created,
                "bound": error.bound,
                "state_note": "created/bound 为 null 表示结果不确定；字段描述已确认阶段，不保证后续状态未变化。",
                "audit_error": (
                    f"{type(error.audit_error).__name__}：{error.audit_error}"
                    if error.audit_error is not None else None
                ),
            })
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
