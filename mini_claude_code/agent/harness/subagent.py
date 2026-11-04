"""Subagent 模块：运行受只读工具白名单、轮数、Token 与软超时约束的探索子 Agent。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..config import (
    MODEL,
    SUBAGENT_ALLOWED_TOOL_NAMES,
    SUBAGENT_MAX_ROUNDS,
    SUBAGENT_MAX_TOKENS,
    SUBAGENT_TIMEOUT_SECONDS,
    SUBAGENT_TOTAL_TOKEN_BUDGET,
    SubagentStatus,
    client,
)
from ..core.provider import extract_text, extract_token_usage
from .hooks import (
    ErrorContext,
    PostToolUseContext,
    PreToolUseContext,
    notify_error,
    trigger_hooks,
)
from .permissions import ask_user
from .prompts import SUB_SYSTEM

SUB_TOOLS: list[dict] = []
SUB_HANDLERS: dict[str, Callable[..., str]] = {}

def build_tool_subset(tools: list[dict], handlers: dict[str, Callable[..., str]], allowed_names: frozenset[str]) -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """根据允许的工具名称构建工具子集。"""
    tool_name = []
    for tool in tools:
        tool_name.append(tool["name"])
    for allowed_name in allowed_names:
        if allowed_name not in tool_name:
            raise ValueError(f"允许的工具名称 {allowed_name} 不在工具列表中")
        handler = handlers.get(allowed_name)
        if handler is None:
            raise ValueError(f"允许的工具名称 {allowed_name} 未注册")
        if not callable(handler):
            raise ValueError(f"允许的工具名称 {allowed_name} 没有可调用的处理函数")
    sub_tools = [tool for tool in tools if tool["name"] in allowed_names]
    sub_handlers = {tool["name"]: handlers[tool["name"]] for tool in sub_tools}

    return sub_tools, sub_handlers


def configure_subagent_tools(
    tools: list[dict],
    handlers: dict[str, Callable[..., str]],
) -> None:
    """由运行时组装层建立只读工具子集，避免 Subagent 与主注册表循环导入。"""
    sub_tools, sub_handlers = build_tool_subset(
        tools,
        handlers,
        SUBAGENT_ALLOWED_TOOL_NAMES,
    )
    global SUB_TOOLS, SUB_HANDLERS
    SUB_TOOLS = sub_tools
    SUB_HANDLERS = sub_handlers


@dataclass
class SubagentRunStats:
    model_calls: int = 0  # 子 Agent 已完成的模型请求次数
    tool_calls: int = 0  # 子 Agent 已执行的工具调用次数
    input_tokens: int = 0  # 子 Agent 累计输入 Token 数
    output_tokens: int = 0  # 子 Agent 累计输出 Token 数

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

def finish_subagent(result: str, status: SubagentStatus, stats: SubagentRunStats, started_at: float) -> str:
    """完成子 Agent 执行，打印统计信息。"""
    time_used = time.monotonic() - started_at
    print(f"[子 Agent] 状态：{status}；"
          f"模型调用：{stats.model_calls} 次；"
          f"工具请求：{stats.tool_calls} 次；"
          f"输入 Token：{stats.input_tokens} 个；"
          f"输出 Token：{stats.output_tokens} 个；"
          f"总 Token：{stats.total_tokens} 个；"
          f"耗时：{time_used:.2f} 秒")
    return result

def spawn_subagent(description: str, max_rounds: int = SUBAGENT_MAX_ROUNDS, token_budget: int = SUBAGENT_TOTAL_TOKEN_BUDGET, timeout_seconds: float = SUBAGENT_TIMEOUT_SECONDS) -> str:
    started_at = time.monotonic()
    stats = SubagentRunStats()

    if not isinstance(description, str) or not description.strip():
        return finish_subagent(result = "子 Agent 的任务描述必须是非空字符串", status = "error", stats = stats, started_at = started_at)
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds <= 0:
        return finish_subagent(result = f"子 Agent 的最大轮数必须是正整数", status = "error", stats = stats, started_at = started_at)
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget <= 0:
        return finish_subagent(result = f"子 Agent 的总 Token 预算必须是正整数", status = "error", stats = stats, started_at = started_at)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int)) or timeout_seconds <= 0:
        return finish_subagent(result = f"子 Agent 的超时时间必须是正数", status = "error", stats = stats, started_at = started_at)

    sub_messages = [{"role": "user", "content": description.strip()}]
    for round_number in range(max_rounds):

        if timeout_seconds <= time.monotonic() - started_at:
            return finish_subagent(result = f"子 Agent 执行超时，耗时 {time.monotonic() - started_at:.2f} 秒", status = "timeout", stats = stats, started_at = started_at)

        remaining_tokens = token_budget - stats.total_tokens
        if remaining_tokens <= 0:
            return finish_subagent(result = f"子 Agent 已达到或超过 Token 预算：已使用 {stats.total_tokens} 个 Token，预算 {token_budget} 个 Token", status = "token_budget", stats = stats, started_at = started_at)

        try:
            response = client.messages.create(
                model=MODEL,
                system=SUB_SYSTEM,
                max_tokens=min(SUBAGENT_MAX_TOKENS, remaining_tokens),
                messages=sub_messages,
                tools=SUB_TOOLS,
            )
        except Exception as e:
            notify_error(ErrorContext(stage="ModelCall", error=e))
            return finish_subagent(result = f"子 Agent 调用模型失败：{type(e).__name__}：{e}", status = "error", stats = stats, started_at = started_at)

        sub_messages.append({"role": "assistant", "content": response.content})
        stats.model_calls += 1
        try:
            input_tokens, output_tokens = extract_token_usage(response)
        except ValueError as e:
            notify_error(ErrorContext(stage="ModelCall", error=e))
            return finish_subagent(result = f"子 Agent 模型响应中 Token 使用情况提取失败：{e}", status = "error", stats = stats, started_at = started_at)
        stats.input_tokens += input_tokens
        stats.output_tokens += output_tokens

        if timeout_seconds <= time.monotonic() - started_at:
            return finish_subagent(result = f"子 Agent 执行超时，耗时 {time.monotonic() - started_at:.2f} 秒", status = "timeout", stats = stats, started_at = started_at)

        if response.stop_reason != "tool_use":
            sub_final_text = extract_text(response.content)
            if not sub_final_text.strip():
                return finish_subagent(result = f"子 Agent 未返回最终结论", status = "error", stats = stats, started_at = started_at)
            return finish_subagent(result = sub_final_text, status = "completed", stats = stats, started_at = started_at)

        if token_budget - stats.total_tokens <= 0:
            return finish_subagent(result = f"子 Agent 已达到或超过 Token 预算：已使用 {stats.total_tokens} 个 Token，预算 {token_budget} 个 Token", status = "token_budget", stats = stats, started_at = started_at)

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            stats.tool_calls += 1
            sub_tool_input = dict(block.input)
            sub_pre_content = PreToolUseContext(
                tool_name=block.name,
                tool_input=sub_tool_input,
                tool_use_id=block.id,
                permission_mode="strict",
                approver=ask_user,
            )
            try:
                sub_pre_result = trigger_hooks("PreToolUse", sub_pre_content)
            except Exception as e:
                notify_error(ErrorContext(stage="PreToolUse", error=e, tool_name=block.name, tool_use_id=block.id))
                output = (
                    f"工具 {block.name} 未执行；"
                    f"执行前 Hook 发生异常："
                    f"{type(e).__name__}：{e}；"
                    f"规则 ID：hook.pre_tool_use.error。"
                )
                tool_results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                continue
            if sub_pre_result.behavior == "block":
                output = (
                    f"工具 {block.name} 被拒绝执行；"
                    f"拒绝原因：{sub_pre_result.reason}；"
                    f"规则 ID：{sub_pre_result.rule_id or 'hook.pre_tool_use.block'}；"
                    f"工具未执行。"
                )
            elif sub_pre_result.behavior == "continue":
                handler = SUB_HANDLERS.get(block.name)
                if not callable(handler):
                    error = RuntimeError(f"工具 {block.name} 没有可调用的处理函数")
                    notify_error(ErrorContext(stage="ToolExecution", error=error, tool_name=block.name, tool_use_id=block.id))
                    output = (
                        f"工具 {block.name} 注册信息错误，"
                        f"找不到可调用的处理函数。"
                        )
                else:
                    if timeout_seconds <= time.monotonic() - started_at:
                        return finish_subagent(result = f"子 Agent 执行超时，耗时 {time.monotonic() - started_at:.2f} 秒", status = "timeout", stats = stats, started_at = started_at)
                    try:
                        output = handler(**sub_tool_input)
                        if not isinstance(output, str):
                            output = str(output)
                    except Exception as error:
                        notify_error(ErrorContext(stage="ToolExecution", error=error, tool_name=block.name, tool_use_id=block.id))
                        output = (
                            f"工具 {block.name} 执行失败："
                            f"{type(error).__name__}：{error}"
                            )
                        tool_results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                        continue
                    sub_post_context = PostToolUseContext(
                        tool_name=block.name,
                        tool_input=sub_tool_input,
                        tool_use_id=block.id,
                        output=output,
                    )

                    try:
                        trigger_hooks("PostToolUse", sub_post_context)
                    except Exception as error:
                        notify_error(ErrorContext(stage="PostToolUse", error=error, tool_name=block.name, tool_use_id=block.id))
                        output = (
                            f"{output}\n\n"
                            f"警告：工具已执行成功，但执行后 Hook 失败："
                            f"{type(error).__name__}：{error}"
                            )
            tool_results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
        sub_messages.append({"role": "user", "content": tool_results})
        if timeout_seconds <= time.monotonic() - started_at:
            return finish_subagent(result = f"子 Agent 执行超时，耗时 {time.monotonic() - started_at:.2f} 秒", status = "timeout", stats = stats, started_at = started_at)

    return finish_subagent(result = f"子 Agent 达到最大轮数 {max_rounds}，未得到最终结论。", status = "max_rounds", stats = stats, started_at = started_at)
