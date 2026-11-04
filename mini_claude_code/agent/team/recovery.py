"""人工 Task 恢复入口：展示遗留认领、确认旧执行者已停止，再按预期 owner 释放。

仅由交互 CLI 调用，不注册为模型工具；不检测其他进程是否死亡，不实现自动接管或租约。
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..config import TASKS_DIR, console
from ..tasks.models import Task, task_to_record
from ..tasks.service import get_task, list_tasks, release_task
from .manager import TeammateManager
from .task_dispatch import teammate_task_owner


def _owner_activity(manager: TeammateManager, expected_owner: str) -> tuple[bool, str]:
    """只报告本进程已知事实；找不到成员不能推断其已死亡。"""
    for snapshot in manager.list_all():
        if expected_owner in (
            teammate_task_owner(snapshot.spec), f"teammate:{snapshot.spec.name}",
        ):
            active = manager.is_member_active(snapshot.spec.name)
            return active, (
                f"{snapshot.spec.name}：本进程仍活跃，禁止恢复"
                if active else f"{snapshot.spec.name}：本进程已结束，需核验"
            )
    return False, "未找到本地成员，需人工核验其他进程"


def _require_inactive_owner(manager: TeammateManager, expected_owner: str) -> None:
    """拒绝当前进程已知的活跃 owner；兼容旧版按成员名生成的 owner。"""
    active, description = _owner_activity(manager, expected_owner)
    if active:
        raise ValueError(description + "；请先停止成员并确认线程退出")


def _select_recovery_task(
    keyword: str, manager: TeammateManager, *, tasks_dir: Path, ui: Console,
) -> Task | None:
    """展示固定候选快照，序号只对应本次列表；完整 ID 和 owner 不取自缩写。"""
    candidates = tuple(
        task for task in list_tasks(tasks_dir)
        if task.status == "in_progress" and keyword.casefold() in task.subject.casefold()
    )
    if not candidates:
        ui.print("没有符合标题筛选条件的 in_progress 任务。", style="dim", markup=False)
        return None
    table = Table(title="人工恢复候选（in_progress 不代表执行者已经停止）")
    for column in ("序号", "任务标题", "Task ID（缩写）", "Owner（缩写）", "本进程情况"):
        table.add_column(column)
    for index, task in enumerate(candidates, start=1):
        if task.owner is None:
            raise ValueError("in_progress Task 缺少 owner，不能恢复")
        _, activity = _owner_activity(manager, task.owner)
        owner_label = task.owner if len(task.owner) <= 24 else task.owner[:16] + "…" + task.owner[-6:]
        # JSON 转义控制字符；Text 禁止将任务正文解释为 Rich 标记。
        table.add_row(
            str(index), Text(json.dumps(task.subject, ensure_ascii=False)),
            task.id[:13] + "…" + task.id[-6:],
            Text(json.dumps(owner_label, ensure_ascii=False)), Text(activity),
        )
    ui.print(table)
    ui.print(
        f"共 {len(candidates)} 项。可取消后使用 /task recover 关键词 按标题缩小范围。",
        style="dim", markup=False,
    )
    try:
        choice = ui.input("输入序号查看完整详情（回车、q 或其他非序号输入取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        ui.print("\n已取消恢复，未修改任务。", style="dim", markup=False)
        return None
    if not choice.isascii() or not choice.isdecimal() or len(choice) > len(str(len(candidates))):
        ui.print("已取消恢复，未修改任务。", style="dim", markup=False)
        return None
    index = int(choice)
    if not 1 <= index <= len(candidates):
        ui.print("序号不在本次列表范围内，已取消恢复。", style="yellow", markup=False)
        return None
    return candidates[index - 1]


def handle_task_command(
    query: str,
    manager: TeammateManager,
    *,
    tasks_dir: Path = TASKS_DIR,
    ui: Console = console,
) -> None:
    """处理 /task recover；人工等待期间不持 Task 文件锁，不进入 Agent 历史。

    CLI 在主线程的 Agent 锁内调用，避免后台 Lead 回合抢占确认输入。
    expected_owner 只是并发校验条件，不是执行者已死亡的证明或逐次认领令牌。
    """
    if not isinstance(manager, TeammateManager):
        raise TypeError("Task 恢复 manager 必须是 TeammateManager")
    parts = query.split(maxsplit=2)
    usage = "用法：/task recover [标题关键词]，或 /task recover <task_id> <expected_owner>"
    if len(parts) < 2 or parts[0].casefold() != "/task" or parts[1].casefold() != "recover":
        ui.print(usage, style="yellow", markup=False)
        return
    tail = parts[2].strip() if len(parts) == 3 else ""
    try:
        # task_ 开头保留给原来的精确 ID 命令；其他文本作为标题关键词（允许空格）。
        if tail.startswith("task_"):
            identifiers = tail.split()
            if len(identifiers) != 2:
                ui.print(usage, style="yellow", markup=False)
                return
            task_id, expected_owner = identifiers
        else:
            selected = _select_recovery_task(tail, manager, tasks_dir=tasks_dir, ui=ui)
            if selected is None:
                return
            if selected.owner is None:
                raise ValueError("所选任务快照缺少 owner")
            task_id, expected_owner = selected.id, selected.owner
        # 序号选择期间也可能发生变化，先重新读取；仍保留列表快照中的 expected_owner。
        task = get_task(task_id, tasks_dir=tasks_dir)
        # 用 JSON 转义换行及控制字符，禁止标题中的伪造终端提示或 Rich 标记生效。
        ui.print("待恢复任务完整详情：\n" + json.dumps(
            task_to_record(task), ensure_ascii=False, indent=2,
        ), markup=False)
        if task.status != "in_progress":
            raise ValueError("只有 in_progress Task 可以恢复，未修改任务")
        if task.owner != expected_owner:
            raise ValueError("当前 owner 与 expected_owner 不一致，请重新核验，未修改任务")
        _require_inactive_owner(manager, expected_owner)
        ui.print(
            "警告：程序不能确认其他进程是否仍在执行此任务。请先确认原执行者已经停止。\n"
            "恢复仅将任务释放为 pending，不撤销已有操作；恢复后其他成员可能立即认领。\n"
            "同一 owner 的释放后重新认领无法仅靠 owner 区分；不要在原执行者仍可能运行时恢复。",
            style="yellow", markup=False,
        )
        try:
            confirmation = ui.input("若确认原执行者已停止并允许释放，请输入「确认释放」（其他输入取消）：")
        except (EOFError, KeyboardInterrupt):
            ui.print("\n已取消恢复，未修改任务。", style="dim", markup=False)
            return
        if confirmation.strip() != "确认释放":
            ui.print("已取消恢复，未修改任务。", style="dim", markup=False)
            return
        # 人工等待期间状态可能改变；再次拦截本进程活跃成员，再由服务锁内复核磁盘状态与 owner。
        _require_inactive_owner(manager, expected_owner)
        released = release_task(task_id, expected_owner, tasks_dir=tasks_dir)
        ui.print(
            f"Task {released.id} 已释放为 pending，owner/result 已清空，标题、描述和依赖保留。"
            "这是本次提交结果，不保证随后仍未被其他成员认领。",
            style="green", markup=False,
        )
    except Exception as error:
        ui.print(
            f"Task 恢复未确认成功：{type(error).__name__}：{error}。请核验任务当前状态。",
            style="red", markup=False,
        )
