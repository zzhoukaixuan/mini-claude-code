"""Cron 工具适配模块：调用调度服务并向模型返回稳定、限长的 JSON 结果。"""

from __future__ import annotations

import json
from typing import cast

from ..config import CRON_PROMPT_PREVIEW_CHARS, CronTimezone
from .models import CronJob, cron_job_to_record
from .service import CronScheduler


def _render_tool_payload(payload: dict[str, object]) -> str:
    """将 Cron 工具结果序列化为紧凑且支持中文的 JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _render_tool_error(action: str, error: Exception) -> str:
    """将业务异常转成普通工具结果，维持 tool_use/tool_result 消息配对。"""
    return _render_tool_payload(
        {
            "ok": False,
            "action": action,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    )


def _cron_summary(cron_job: CronJob) -> dict[str, object]:
    """为列表和取消结果生成不会被超长 Prompt 撑大的计划摘要。"""
    prompt_preview = cron_job.prompt[:CRON_PROMPT_PREVIEW_CHARS]
    return {
        "id": cron_job.id,
        "cron": cron_job.cron,
        "promptPreview": prompt_preview,
        "promptLength": len(cron_job.prompt),
        "promptTruncated": len(prompt_preview) < len(cron_job.prompt),
        "recurring": cron_job.recurring,
        "durable": cron_job.durable,
        "timezone": cron_job.timezone,
        "misfirePolicy": cron_job.misfire_policy,
        "createdAt": cron_job.created_at,
        "lastFiredSlot": cron_job.last_fired_slot,
    }


def run_schedule_cron(
    cron: str,
    prompt: str,
    recurring: bool = True,
    durable: bool = True,
    timezone: str = "local",
    *,
    scheduler: CronScheduler,
) -> str:
    """创建 CronJob，并返回完整记录或结构化错误。"""
    try:
        cron_job = scheduler.schedule(
            cron,
            prompt,
            recurring=recurring,
            durable=durable,
            timezone=cast(CronTimezone, timezone),
        )
        return _render_tool_payload(
            {
                "ok": True,
                "action": "schedule_cron",
                "cronJob": cron_job_to_record(cron_job),
                "executionStarted": False,
            }
        )
    except Exception as error:
        return _render_tool_error("schedule_cron", error)


def run_list_crons(*, scheduler: CronScheduler) -> str:
    """列出当前可见 CronJob 的限长摘要。"""
    try:
        cron_jobs = scheduler.list_jobs()
        return _render_tool_payload(
            {
                "ok": True,
                "action": "list_crons",
                "cronJobs": [_cron_summary(cron_job) for cron_job in cron_jobs],
            }
        )
    except Exception as error:
        return _render_tool_error("list_crons", error)


def run_cancel_cron(
    cron_job_id: str,
    *,
    scheduler: CronScheduler,
) -> str:
    """取消未来计划，并明确已排队的触发不会被撤回。"""
    try:
        cancelled_job = scheduler.cancel(cron_job_id)
        return _render_tool_payload(
            {
                "ok": True,
                "action": "cancel_cron",
                "cronJob": _cron_summary(cancelled_job),
                "queuedTriggerWithdrawn": False,
            }
        )
    except Exception as error:
        return _render_tool_error("cancel_cron", error)
