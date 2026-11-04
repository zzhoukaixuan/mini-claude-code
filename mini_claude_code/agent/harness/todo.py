"""Todo 模块：维护单轮任务计划、状态迁移、版本号与 todo_write 工具结果。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import (
    MAX_TODO_CONTENT_LENGTH,
    MAX_TODOS,
    TodoStatus,
    VALID_STATUS,
)

@dataclass(frozen=True)
class TodoItem:
    content: str  # 待办事项内容
    status: TodoStatus  # 待办事项状态

@dataclass
class TodoState:
    items: list[TodoItem] = field(default_factory=list)  # 待办事项列表
    revision: int = 0  # 状态版本号

TODO_STATE = TodoState()  # 当前用户任务的进程内 TODO 状态

ALLOWED_TODO_TRANSITIONS = {  # TODO 状态机允许的状态迁移
    "pending": {"pending", "in_progress"},
    "in_progress": {"pending", "in_progress", "completed"},
    "completed": {"completed"},
}

def reset_todo_state() -> None:
    """重置待办事项状态。"""
    TODO_STATE.items = []
    TODO_STATE.revision = 0

def normalize_todos(raw_todos: object) -> list[TodoItem]:
    """校验并标准化待办事项，但不修改全局状态。"""
    if not isinstance(raw_todos, list):
        raise ValueError("待办事项必须是列表")
    if not raw_todos:
        raise ValueError("待办事项列表不能为空")
    if len(raw_todos) > MAX_TODOS:
        raise ValueError(f"待办事项数量不能超过 {MAX_TODOS} 项")
    normalized_todos: list[TodoItem] = []
    seen_contents: set[str] = set()
    in_progress_count = 0
    for index, raw_todo in enumerate(raw_todos, start=1):
        if not isinstance(raw_todo, dict):
            raise ValueError(f"第 {index} 项待办事项必须是字典")
        if set(raw_todo) != {"content", "status"}:
            raise ValueError(f"第 {index} 项只能包含 content 和 status 两个字段")
        raw_content = raw_todo["content"]
        if not isinstance(raw_content, str):
            raise ValueError(f"第 {index} 项的 content 必须是字符串")
        content = raw_content.strip()
        if not content:
            raise ValueError(f"第 {index} 项的 content 不能为空")
        if len(content) > MAX_TODO_CONTENT_LENGTH:
            raise ValueError(f"第 {index} 项的 content 不能超过 {MAX_TODO_CONTENT_LENGTH} 个字符")
        content_key = content.casefold()
        if content_key in seen_contents:
            raise ValueError(f"存在重复的待办事项：{content}")
        raw_status = raw_todo["status"]
        if not isinstance(raw_status, str) or raw_status not in VALID_STATUS:
            raise ValueError(f"第 {index} 项的 status 必须是 {VALID_STATUS}")
        if raw_status == "in_progress":
            in_progress_count += 1
            if in_progress_count > 1:
                raise ValueError("最多只能有一项待办事项处于 in_progress 状态")
        seen_contents.add(content_key)
        normalized_todos.append(TodoItem(content = content, status = raw_status))
    return normalized_todos

def validate_todo_transitions(previous_todos: list[TodoItem], updated_todos: list[TodoItem],) -> None:
    """校验待办事项状态转换是否符合允许。"""
    previous_by_content = {todo.content.casefold(): todo for todo in previous_todos}
    updated_by_content = {todo.content.casefold(): todo for todo in updated_todos}
    for content_key, previous_todo in previous_by_content.items():
        if previous_todo.status == "in_progress" and content_key not in updated_by_content:
            raise ValueError(f"正在进行的待办事项“{previous_todo.content}”不能直接删除，请先将其改为 pending 或 completed")
    for updated_todo in updated_todos:
        content_key = updated_todo.content.casefold()
        previous_todo = previous_by_content.get(content_key)
        if previous_todo is None:
            if updated_todo.status == "completed":
                raise ValueError(f"新待办事项“{updated_todo.content}”不能直接设为 completed")
            continue
        allowed_statuses = ALLOWED_TODO_TRANSITIONS[previous_todo.status]
        if updated_todo.status not in allowed_statuses:
            raise ValueError(f"待办事项“{updated_todo.content}”不能从 {previous_todo.status} 直接变为 {updated_todo.status}")

def render_todos(todos: list[TodoItem]) -> str:
    """把待办事项渲染成适合终端阅读的文本。"""
    status_icons = {"pending": " ", "in_progress": "▸", "completed": "✓"}
    lines = ["当前 TODO 列表："]
    for index, todo in enumerate(todos, start=1):
        icon = status_icons[todo.status]
        lines.append(f"{index}. [{icon}] {todo.content}")
    return "\n".join(lines)

def run_todo_write(todos: list[dict]) -> str:
    try:
        normalized_todos = normalize_todos(todos)
        validate_todo_transitions(TODO_STATE.items, normalized_todos)
    except ValueError as e:
        return f"TODO 更新失败：{e}"
    TODO_STATE.items = normalized_todos
    TODO_STATE.revision += 1
    print(render_todos(TODO_STATE.items))
    pending_count = sum(todo.status == "pending" for todo in TODO_STATE.items)
    in_progress_count = sum(todo.status == "in_progress" for todo in TODO_STATE.items)
    completed_count = sum(todo.status == "completed" for todo in TODO_STATE.items)
    return (
            f"TODO 更新成功：共 {len(TODO_STATE.items)} 项，"
            f"待处理 {pending_count} 项，"
            f"进行中 {in_progress_count} 项，"
            f"已完成 {completed_count} 项，"
            f"当前版本为 {TODO_STATE.revision}"
    )
