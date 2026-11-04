"""权限模块：分类工具风险、检查路径与命令边界，并在 Ask 模式下请求人工批准。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from ..config import (
    DELEGATION_TOOLS,
    HARD_DENY_PATTERNS,
    PATH_ARGUMENT_BY_TOOL,
    PERSISTENT_STATE_TOOLS,
    READ_ONLY_TOOLS,
    SESSION_STATE_TOOLS,
    SIDE_EFFECT_TOOLS,
    VALID_PERMISSION_MODES,
    WINDOWS_EXECUTABLE_NAMES,
    WORKDIR,
    TEAMMATE_WRITE_TOOL_NAMES,
    PermissionMode,
)
from ..tools.filesystem import safe_path
from ..mcp.adapter import MCPToolHandler

_PERMISSION_TOOL_HANDLERS: dict[str, Callable] = {}


def configure_permission_handlers(handlers: dict[str, Callable]) -> None:
    """由运行时组装层注入工具注册表，避免权限层反向依赖 runtime。"""
    if not isinstance(handlers, dict):
        raise TypeError("权限层工具 Handler 注册表必须是字典")
    if any(not isinstance(name, str) or not callable(handler) for name, handler in handlers.items()):
        raise ValueError("权限层工具 Handler 注册表必须由工具名和可调用对象组成")
    global _PERMISSION_TOOL_HANDLERS
    _PERMISSION_TOOL_HANDLERS = handlers

@dataclass(frozen=True)
class PermissionDecision:
    behavior: Literal["allow", "deny", "ask"]  # 权限策略要求允许、拒绝还是人工确认
    reason: str  # 权限决定的可读原因
    rule_id: str  # 命中的稳定权限规则标识
    def __post_init__(self):
        if self.behavior not in ("allow", "deny", "ask"):
            raise ValueError(f"未知的权限决策 {self.behavior}")

def is_within_workspace(raw_path: str, *, workspace: Path = WORKDIR) -> bool:
    """复用文件工具的边界规则；目录无效、解析失败或越界均不能视为允许。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False
    try:
        safe_path(raw_path, workspace=workspace)
    except (OSError, ValueError, TypeError, RuntimeError):
        # resolve 可能因权限、失效目录或链接环失败；权限层统一拒绝，工具仍独立复核。
        return False
    return True

def matches_hard_deny(command: str) -> str | None:
    """命中硬拒绝规则时返回模式，否则返回 None。"""
    normalized_command = " ".join(command.casefold().split())
    for executable_name in WINDOWS_EXECUTABLE_NAMES:
        normalized_command = normalized_command.replace(f"{executable_name}.exe", executable_name)
    normalized_command = normalized_command.replace("format.com", "format")
    for pattern in HARD_DENY_PATTERNS:
        if pattern in normalized_command:
            return pattern
    return None

def evaluate_permission(
    tool_name: str,
    args: dict,
    mode: PermissionMode,
    *,
    registered_handlers: Mapping[str, Callable] | None = None,
    workspace: Path = WORKDIR,
    isolated_file_write: bool = False,
) -> PermissionDecision:
    """评估工具权限；显式映射限定当前调用方的工具范围，None 沿用全局注册表。

    映射由 Harness 注入，不从模型参数读取；只确定工具是否注册，不能替代
    后续风险分类、路径边界及权限模式检查。调用期间不得并发修改此映射。
    workspace 仅由 Harness 指定文件工具的检查范围，不从 args 读取，
    不改变风险策略，也不表示 Shell 已被隔离。无路径的协议工具沿用原策略。
    isolated_file_write 仅由队友 Harness 在隔离 Task 核验且计划获批后传入，
    为两种文件写工具提供窄范围授权；默认 False，不读取模型参数中的同名字段。
    """
    # 1. 检查权限模式是否有效
    if type(isolated_file_write) is not bool:
        raise TypeError("隔离文件写许可必须是 bool，由 Harness 提供")
    if mode not in VALID_PERMISSION_MODES:
        raise ValueError(f"未知的权限模式 {mode}")
    # 2. 检查当前调用方的注册范围；空映射意味着没有工具，不能回退到全局。
    if registered_handlers is not None and not isinstance(registered_handlers, Mapping):
        raise TypeError("权限检查 registered_handlers 必须是映射或 None")
    handlers = (
        registered_handlers
        if registered_handlers is not None
        else _PERMISSION_TOOL_HANDLERS
    )
    if not callable(handlers.get(tool_name)):
        return PermissionDecision(behavior="deny", reason=f"工具 {tool_name} 未注册", rule_id="tool.unknown")
    # 外部服务不能通过只读声明获得授权，也不继承本地文件的隔离写许可。
    # 当前无本地 MCP 授权配置：strict 拒绝，ask/trusted 均需逐次人工确认。
    if isinstance(handlers[tool_name], MCPToolHandler) or tool_name in ("connect_mcp", "disconnect_mcp"):
        if mode == "strict":
            return PermissionDecision("deny", "严格模式禁止尚未获得本地授权的外部 MCP 工具", "mcp.strict")
        return PermissionDecision("ask", "外部 MCP 工具需要人工确认；服务端声明不作为授权", "mcp.ask")
    # 3. 检查工具是否已经被分类
    if (
        tool_name not in READ_ONLY_TOOLS
        and tool_name not in SIDE_EFFECT_TOOLS
        and tool_name not in SESSION_STATE_TOOLS
        and tool_name not in PERSISTENT_STATE_TOOLS
        and tool_name not in DELEGATION_TOOLS
    ):
        return PermissionDecision(behavior="deny", reason=f"工具 {tool_name} 未被分类", rule_id="tool.unclassified")
    # 4. 检查Shell工具参数是否有效
    if tool_name == "shell":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return PermissionDecision(behavior="deny", reason=f"工具 {tool_name} 的命令参数不能为空", rule_id="tool.invalid_arguments")
        matched_pattern = matches_hard_deny(command)
        if matched_pattern is not None:
            return PermissionDecision(behavior="deny", reason=f"工具 {tool_name} 禁止执行命令 {matched_pattern}", rule_id="command.hard_deny")
    # 5. 检查文件工具路径边界
    path_argument = PATH_ARGUMENT_BY_TOOL.get(tool_name)
    if path_argument is not None:
        raw_path = args.get(path_argument)
        if not isinstance(raw_path, str) or not raw_path.strip():
            return PermissionDecision(behavior="deny", reason=f"工具 {tool_name} 的路径参数不能为空", rule_id="tool.invalid_arguments")
        if not is_within_workspace(raw_path, workspace=workspace):
            return PermissionDecision(
                behavior="deny",
                reason=f"无法确认工具 {tool_name} 的路径参数 {raw_path!r} 位于有效工作目录 {workspace!r} 内；可能越界、目录无效或路径解析失败",
                rule_id="workspace.escape",
            )
    # 此许可只放行指定文件写工具；注册、分类与路径检查必须已经通过。
    # 不是 trusted 模式，也不会放行 Shell、Git 管理或其他持久副作用。
    if isolated_file_write and tool_name in TEAMMATE_WRITE_TOOL_NAMES:
        target = safe_path(args["path"], workspace=workspace)
        if ".git" in (
            part.casefold().split(":", 1)[0].rstrip(" .")
            for part in target.relative_to(workspace.resolve()).parts
        ):
            return PermissionDecision("deny", "队友不能改写 Git 元数据", "team.git_metadata")
        return PermissionDecision("allow", "已核验隔离任务且当前计划获批，允许工作目录内文件写入", "team.isolated_file_write")
    # 6. 委派工具直接允许
    if tool_name in DELEGATION_TOOLS:
        return PermissionDecision(behavior="allow", reason=f"工具 {tool_name} 只创建受 Harness 预算与工具白名单约束的 Agent 执行，允许使用", rule_id="tool.restricted_delegation")
    # 7. 会话状态工具直接允许
    if tool_name in SESSION_STATE_TOOLS:
        return PermissionDecision(behavior="allow", reason=f"工具 {tool_name} 只管理 Harness 会话状态、内部上下文、团队邮箱或受控后台 Job，允许执行", rule_id="tool.session_state")
    # 8. Harness 持久状态工具直接允许
    if tool_name in PERSISTENT_STATE_TOOLS:
        return PermissionDecision(behavior="allow", reason=f"工具 {tool_name} 只修改经过校验的 Harness 持久状态，允许执行", rule_id="tool.persistent_state")
    # 9. 只读工具直接允许
    if tool_name in READ_ONLY_TOOLS:
        return PermissionDecision(behavior="allow", reason=f"工具 {tool_name} 是只读工具，允许执行", rule_id="tool.read_only")
    # 10. 合法的副作用工具需要确认权限
    if mode == "strict":
        if tool_name in SIDE_EFFECT_TOOLS:
            return PermissionDecision(behavior="deny", reason=f"在{mode}模式下，{tool_name} 是副作用工具，禁止执行", rule_id="mode.strict")
    if mode == "ask":
        if tool_name in SIDE_EFFECT_TOOLS:
            return PermissionDecision(behavior="ask", reason=f"在{mode}模式下，{tool_name} 是副作用工具，需要确认执行", rule_id="side_effect.ask")
    if mode == "trusted":
        return PermissionDecision(behavior="allow", reason=f"在{mode}模式下，{tool_name} 是受信任工具，允许执行", rule_id="mode.trusted")

ApprovalCallback = Callable[[str, dict, PermissionDecision], bool]  # 人工审批回调函数类型

def ask_user(tool_name: str, args:dict, decision: PermissionDecision) -> bool:
    display_args = dict(args)
    for key in ("content", "old_text", "new_text", "prompt"):
        value = display_args.get(key)
        if isinstance(value, str):
            display_args[key] = f"<内容已省略，共 {len(value)} 个字符>"
    print()
    print("╭──────────────────────────────────────────────╮")
    print("│              🔐 工具执行审批                │")
    print("╰──────────────────────────────────────────────╯")
    print(f"\n🛠  工具名称：{tool_name}")
    print("\n📦 参数：")
    if display_args:
        for key, value in display_args.items():
            print(f"   • {key}：{value}")
    else:
        print("   （无参数）")
    print(f"\n💡 执行原因：{decision.reason}")
    print(f"🏷  规则 ID：{decision.rule_id}")
    print("\n────────────────────────────────────────────────")
    try:
        user_input = input("是否允许执行？[y/yes = 允许，其他 = 拒绝]：")
        if user_input.casefold().strip() == "y" or user_input.casefold().strip() == "yes":
            print("✅ 已批准执行\n")
            return True
        else:
            print("❌ 已拒绝执行\n")
            return False
    except (EOFError, KeyboardInterrupt):
        print("\n⚠️  未获得明确确认，默认拒绝执行。\n")
        return False
