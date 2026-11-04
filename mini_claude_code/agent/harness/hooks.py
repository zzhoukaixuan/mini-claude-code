"""Hook 模块：注册并触发用户输入、工具前后、停止和错误阶段的 Harness 回调。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from ..config import PermissionMode, WORKDIR
from .permissions import ApprovalCallback, evaluate_permission

HookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Error"]  # 钩子事件
HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Error")  # 钩子事件列表

@dataclass(frozen=True)
class HookResult:
    behavior: Literal["continue", "block"]  # Hook 要求继续流程还是阻断流程
    reason: str = ""  # Hook 决定的可读原因
    rule_id: str | None = None  # 产生该决定的可选稳定规则标识

    def __post_init__(self):
        if self.behavior not in ("continue", "block"):
            raise ValueError(f"未知的钩子行为 {self.behavior}")

HookCallback = Callable[..., HookResult]  # 钩子回调函数
HOOKS: dict[str, list[HookCallback]] = {event: [] for event in HOOK_EVENTS}  # 钩子注册表

def register_hook(event: HookEvent, callback: HookCallback) -> None:
    """注册钩子回调函数。"""
    if event not in HOOK_EVENTS:
        raise ValueError(f"未知的钩子事件 {event}")
    if not callable(callback):
        raise TypeError(f"钩子回调函数 {callback} 不是可调用对象")
    HOOKS[event].append(callback)

def trigger_hooks(event: HookEvent, *args) -> HookResult:
    """触发钩子回调函数。"""
    if event not in HOOK_EVENTS:
        raise ValueError(f"未知的钩子事件 {event}")
    for callback in HOOKS[event]:
        result = callback(*args)
        if not isinstance(result, HookResult):
            raise TypeError(f"钩子回调函数 {callback} 返回的不是 HookResult 类型")
        if result.behavior == "block":
            return result
        elif result.behavior == "continue":
            continue
    return HookResult(behavior="continue")

def clear_hooks(event: HookEvent | None = None) -> None:
    """清除钩子回调函数。"""
    if event is None:
        for hook_event in HOOK_EVENTS:
            HOOKS[hook_event].clear()
    else:
        if event not in HOOK_EVENTS:
            raise ValueError(f"未知的钩子事件 {event}")
        HOOKS[event].clear()

# -------------------- PreToolUse Hook --------------------

@dataclass(frozen=True)
class PreToolUseContext:
    tool_name: str  # 模型请求调用的工具名称
    tool_input: dict  # 模型为工具提供的原始参数
    tool_use_id: str  # 与后续 tool_result 一一对应的工具调用 ID
    permission_mode: PermissionMode  # 当前 Agent Loop 使用的权限模式
    approver: ApprovalCallback  # 需要人工确认时使用的审批回调
    registered_handlers: Mapping[str, Callable] | None = None  # Harness 注入的当前工具范围；None 沿用全局，空映射禁止所有工具
    workspace: Path = WORKDIR  # Harness 指定的本次文件工具工作目录，不由模型参数提供
    isolated_file_write: bool = False  # Harness 确认隔离任务且当前计划获批后的单次调用许可

def log_tool_use_hook(context: PreToolUseContext) -> HookResult:
    """记录工具调用信息。"""
    display_args = dict(context.tool_input)
    for key in ("content", "old_text", "new_text", "prompt"):
        value = display_args.get(key)
        if isinstance(value, str):
            display_args[key] = f"<内容已省略，共 {len(value)} 个字符>"
    print(f"🛠  工具名称：{context.tool_name}")
    print(f"🔧  工具调用 ID：{context.tool_use_id}")
    print("📦 参数：")
    if display_args:
        for key, value in display_args.items():
            print(f"   • {key}：{value}")
    else:
        print("   （无参数）")
    return HookResult(behavior="continue")

def permission_hook(context: PreToolUseContext) -> HookResult:
    """评估工具权限。"""
    decision = evaluate_permission(
        context.tool_name,
        context.tool_input,
        context.permission_mode,
        registered_handlers=context.registered_handlers,
        workspace=context.workspace,
        isolated_file_write=context.isolated_file_write,
    )
    if decision.behavior == "deny":
        return HookResult(behavior="block", reason=decision.reason, rule_id=decision.rule_id)
    elif decision.behavior == "ask":
        display_input = dict(context.tool_input)
        approved = context.approver(context.tool_name, display_input, decision)
        allowed = approved is True
        if allowed:
            return HookResult(behavior="continue", reason="用户同意执行", rule_id=decision.rule_id)
        else:
            return HookResult(behavior="block", reason="用户拒绝执行", rule_id=decision.rule_id)
    elif decision.behavior == "allow":
        return HookResult(behavior="continue", reason=decision.reason, rule_id=decision.rule_id)

register_hook("PreToolUse", log_tool_use_hook)
register_hook("PreToolUse", permission_hook)

# -------------------- PostToolUse Hook --------------------

@dataclass(frozen=True)
class PostToolUseContext:
    tool_name: str  # 已执行完成的工具名称
    tool_input: dict  # 本次工具执行使用的参数
    tool_use_id: str  # 与模型 tool_use 对应的工具调用 ID
    output: str  # 工具 Handler 返回的文本结果
    is_error: bool = False  # Handler 明确返回业务失败；Hook 失败不改变工具执行事实

def large_output_hook(context: PostToolUseContext) -> HookResult:
    """处理大输出。"""
    if len(context.output) > 100_000:
        print(
            f"⚠️ 工具 {context.tool_name} 的输出较大，"
            f"共 {len(context.output)} 个字符。"
            )
    return HookResult(behavior="continue")

register_hook("PostToolUse", large_output_hook)

# -------------------- UserPromptSubmit Hook --------------------

@dataclass
class UserPromptSubmitContext:
    query: str  # 当前准备提交给 Agent Loop 的用户请求

def normalize_prompt_hook(context: UserPromptSubmitContext) -> HookResult:
    """将用户提交的提示转换为规范格式。"""
    context.query = context.query.strip()
    if context.query == "":
        return HookResult(behavior="block", reason="用户输入为空")
    return HookResult(behavior="continue")

register_hook("UserPromptSubmit", normalize_prompt_hook)

# -------------------- Stop Hook --------------------

@dataclass(frozen=True)
class StopContext:
    messages: list  # Agent Loop 停止时的完整上下文消息列表
    final_text: str  # 模型返回的最终文本答案
    round_number: int  # 产生最终答案时所在的循环轮次

def summary_hook(context: StopContext) -> HookResult:
    """输出 Agent 循环摘要。"""
    tool_count = 0
    for message in context.messages:
        if message.get("role") != "user":
            continue
        if not isinstance(message.get("content"), list):
            continue
        for item in message.get("content"):
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_count += 1
    print(f"Agent Loop 共执行了 {context.round_number} 轮，处理了 {tool_count} 个工具请求。")
    return HookResult(behavior="continue")

def require_final_answer_hook(context: StopContext) -> HookResult:
    """要求最终回答不能为空。"""
    if not context.final_text.strip():
        return HookResult(behavior="block", reason="最终回答不能为空，请继续完成任务。")
    return HookResult(behavior="continue")

register_hook("Stop", summary_hook)
register_hook("Stop", require_final_answer_hook)

# -------------------- Error Hook --------------------

ErrorStage = Literal[  # Error Hook 支持的错误阶段
    "UserPromptSubmit",
    "PromptAssembly",
    "ModelCall",
    "ContextCompact",
    "PreToolUse",
    "ToolExecution",
    "PostToolUse",
    "BackgroundJob",
    "CronScheduler",
    "CronDelivery",
    "TeamInboxPoller",
    "TeamInboxDelivery",
    "Stop",
    "Memory",
]


@dataclass(frozen=True)
class ErrorContext:
    stage: ErrorStage  # 异常发生的 Harness 阶段
    error: Exception  # 被 Error Hook 观察到的原始异常
    tool_name: str | None = None  # 与异常相关的可选工具名称
    tool_use_id: str | None = None  # 与异常相关的可选工具调用 ID

def error_log_hook(context: ErrorContext) -> HookResult:
    """记录错误信息。"""
    print(f"{context.stage} 阶段发生错误")
    print(f"异常类型：{type(context.error).__name__}")
    print(f"异常信息：{context.error}")
    if context.tool_name is not None:
        print(f"工具名称：{context.tool_name}")
        print(f"工具调用 ID：{context.tool_use_id}")
    return HookResult(behavior="continue")

register_hook("Error", error_log_hook)

def notify_error(context: ErrorContext) -> None:
    """安全触发 Error Hook，错误报告失败时不递归触发。"""
    try:
        trigger_hooks("Error", context)
    except Exception as e:
        print("Error Hook 自身执行失败："
              f"{type(e).__name__}：{e}")
