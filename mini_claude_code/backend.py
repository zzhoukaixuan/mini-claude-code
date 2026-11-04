"""产品运行时适配层：ask/chat 共用 agent 包，分别采用单回合与持久聊天生命周期。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from .configuration import ConfigurationError, get_connection


PermissionMode = Literal["strict", "ask", "trusted"]
VALID_PERMISSION_MODES = ("strict", "ask", "trusted")
ProductConfigurationError = ConfigurationError  # 保持产品错误处理入口兼容


def _validate_configuration(*, env_file: Path | None = None) -> None:
    """验证当前产品连接，不加载模型客户端，不写入环境变量。"""
    get_connection(env_file=env_file)


def run_once(
    query: str,
    *,
    permission_mode: PermissionMode = "ask",
    memory_enabled: bool = True,
    env_file: Path | None = None,
) -> str:
    """在全新的单回合会话中执行一次用户任务，并返回最终文本。"""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("任务内容不能为空")
    if permission_mode not in VALID_PERMISSION_MODES:
        raise ValueError(f"未知权限模式：{permission_mode}")
    if type(memory_enabled) is not bool:
        raise TypeError("memory_enabled 必须是布尔值")

    _validate_configuration(env_file=env_file)

    # 配置加载、Skill 扫描和工具注册等启动副作用必须延迟到真正执行任务时。
    from .agent.core.agent_loop import agent_loop
    from .agent.harness.prompts import PromptCacheState
    from .agent.harness.todo import reset_todo_state

    reset_todo_state()
    messages: list[dict] = []
    return agent_loop(
        normalized_query,
        messages,
        permission_mode=permission_mode,
        memory_enabled=memory_enabled,
        prompt_cache=PromptCacheState(),
        single_turn=True,  # 不进入 CLI/startup，不创建 Session，也不启动后台 Runner
    )


def run_chat(argv: list[str] | None = None, *, env_file: Path | None = None) -> int:
    """先选择并占用会话，再验证配置；帮助与历史选择不加载模型客户端。"""
    from .agent.sessions.startup import main as run_session_chat
    return run_session_chat(
        [] if argv is None else argv,
        prepare_runtime=lambda: _validate_configuration(env_file=env_file),
    )
