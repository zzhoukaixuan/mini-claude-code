"""工具注册模块：校验 Schema 与 Handler 一致性，并按模型可见顺序返回已启用工具。"""

from __future__ import annotations

from typing import Callable

def collect_enabled_tool_names(tools: list[dict], handlers: dict[str, Callable],) -> tuple[str, ...]:
    """校验工具 Schema 与 Handler 注册表一致，并按 Schema 顺序返回工具名。"""
    if not isinstance(tools, list):
        raise ValueError("工具 Schema 集合必须是列表")
    if not isinstance(handlers, dict):
        raise ValueError("工具 Handler 注册表必须是字典")

    schema_names: list[str] = []
    seen_names: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ValueError(f"第 {index} 个工具 Schema 必须是字典")
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {index} 个工具 Schema 缺少有效的非空名称")
        if name in seen_names:
            raise ValueError(f"工具 Schema 中存在重复名称 {name!r}")
        seen_names.add(name)
        schema_names.append(name)

    for name in schema_names:
        if name not in handlers:
            raise ValueError(f"工具 {name!r} 已暴露给模型，但没有注册 Handler")
        if not callable(handlers[name]):
            raise ValueError(f"工具 {name!r} 的 Handler 不可调用")

    extra_handler_names = set(handlers) - seen_names
    if extra_handler_names:
        formatted_names = ", ".join(
            repr(name)
            for name in sorted(extra_handler_names, key=lambda value: repr(value))
        )
        raise ValueError(f"以下 Handler 未通过工具 Schema 暴露给模型：{formatted_names}")

    return tuple(schema_names)
