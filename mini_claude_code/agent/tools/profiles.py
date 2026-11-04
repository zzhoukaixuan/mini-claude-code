"""回合工具配置：一次性模式保留同步能力，不修改聊天模式的全局工具表。"""

from copy import deepcopy
from collections.abc import Callable


CHAT_ONLY_TOOLS = frozenset({  # 依赖常驻进程、队列或团队协议生命周期的能力
    "connect_mcp", "disconnect_mcp",  # 第一版连接管理仅开放给聊天 Lead
    "job_status", "job_cancel", "schedule_cron", "list_crons", "cancel_cron",
    "spawn_teammate", "send_message", "check_inbox", "review_plan", "request_shutdown",
})


def select_turn_tools(
    tools: list[dict], handlers: dict[str, Callable], *, single_turn: bool,
) -> tuple[list[dict], dict[str, Callable]]:
    """Schema、权限注册表、实际分派使用同一份能力范围；不改写 runtime 单例。"""
    if not single_turn:
        return tools, handlers
    selected = deepcopy([tool for tool in tools if tool["name"] not in CHAT_ONLY_TOOLS])
    names = {tool["name"] for tool in selected}
    for tool in selected:
        if tool["name"] == "shell":
            tool["description"] = (
                "通过 Windows cmd.exe 同步执行测试、构建、Git 或专用工具不能完成的命令。"
                "一次性 ask 模式不支持后台 Job，也不要自行启动脱离当前进程的后台工作。"
                "文件读写和检索优先使用专用工具；执行命令仍须遵守当前权限策略。"
            )
            properties = tool["input_schema"]["properties"]
            properties.pop("run_in_background", None)
            if "timeout" in properties:
                properties["timeout"]["description"] = "同步命令执行超时秒数；省略时使用默认前台超时。"
    return selected, {name: handler for name, handler in handlers.items() if name in names}
