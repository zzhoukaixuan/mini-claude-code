"""Task 工具适配模块：把模型参数转换为 Service 调用，并返回稳定 JSON 工具结果。"""

from __future__ import annotations

import json

from ..config import MAIN_AGENT_TASK_OWNER
from .models import Task, task_to_record
from .service import (
    claim_task,
    complete_task,
    create_task,
    get_task,
    list_tasks,
    update_task,
)


def _render_tool_payload(payload: dict[str, object]) -> str:
    """将 Task 工具结果序列化为模型可稳定解析的紧凑 JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _render_tool_error(action: str, error: Exception) -> str:
    """把业务异常转换为普通工具结果，避免破坏 tool_result 回填。"""
    return _render_tool_payload(
        {
            "ok": False,
            "action": action,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    )


def _task_summary(task: Task) -> dict[str, object]:
    """生成不包含长描述和结果正文的 Task 列表摘要。"""
    return {
        "id": task.id,
        "subject": task.subject,
        "status": task.status,
        "owner": task.owner,
        "blockedBy": list(task.blocked_by),
    }


def run_task_create(
    subject: str,
    description: str = "",
    blocked_by: list[str] | None = None,
) -> str:
    """创建持久 Task，并返回完整记录或结构化错误。"""
    try:
        task = create_task(subject, description, blocked_by)
        return _render_tool_payload(
            {"ok": True, "action": "task_create", "task": task_to_record(task)}
        )
    except Exception as error:
        return _render_tool_error("task_create", error)


def run_task_list() -> str:
    """列出持久 Task 的轻量摘要。"""
    try:
        tasks = list_tasks()
        return _render_tool_payload(
            {
                "ok": True,
                "action": "task_list",
                "tasks": [_task_summary(task) for task in tasks],
            }
        )
    except Exception as error:
        return _render_tool_error("task_list", error)


def run_task_get(task_id: str) -> str:
    """读取一条持久 Task 的完整记录。"""
    try:
        task = get_task(task_id)
        return _render_tool_payload(
            {"ok": True, "action": "task_get", "task": task_to_record(task)}
        )
    except Exception as error:
        return _render_tool_error("task_get", error)


def run_task_update(
    task_id: str,
    subject: str | None = None,
    description: str | None = None,
    blocked_by: list[str] | None = None,
) -> str:
    """更新 pending Task 的描述字段或依赖。"""
    try:
        task = update_task(
            task_id,
            subject=subject,
            description=description,
            blocked_by=blocked_by,
        )
        return _render_tool_payload(
            {"ok": True, "action": "task_update", "task": task_to_record(task)}
        )
    except Exception as error:
        return _render_tool_error("task_update", error)


def run_task_claim(task_id: str) -> str:
    """以 Harness 注入的主 Agent 身份认领一条可开始的 Task。"""
    try:
        task = claim_task(task_id, MAIN_AGENT_TASK_OWNER)
        return _render_tool_payload(
            {"ok": True, "action": "task_claim", "task": task_to_record(task)}
        )
    except Exception as error:
        return _render_tool_error("task_claim", error)


def run_task_complete(task_id: str, result: str) -> str:
    """以 Harness 注入的主 Agent 身份完成 Task，并报告刚解锁的下游。"""
    try:
        task, unblocked_task_ids = complete_task(
            task_id,
            MAIN_AGENT_TASK_OWNER,
            result,
        )
        return _render_tool_payload(
            {
                "ok": True,
                "action": "task_complete",
                "task": task_to_record(task),
                "unblocked_task_ids": list(unblocked_task_ids),
            }
        )
    except Exception as error:
        return _render_tool_error("task_complete", error)
