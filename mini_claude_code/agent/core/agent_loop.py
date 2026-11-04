"""主循环模块：编排 Prompt、Memory、上下文压缩、模型调用、权限 Hook 与工具结果回填。"""

from __future__ import annotations

from typing import Callable

from ..sessions.lifecycle import CheckpointEvent, SessionCheckpoint, SessionPersistenceError
from ..mcp.adapter import MCPToolHandler, normalize_tool_output
from rich import print as rprint

from ..config import (
    API_KEY,
    AUTO_COMPACT_CONTEXT_CHARS,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    MAIN_AGENT_DEFAULT_MAX_TOKENS,
    MAIN_AGENT_ESCALATED_MAX_TOKENS,
    MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES,
    MAX_OUTPUT_CONTINUATIONS,
    MAX_REACTIVE_RETRIES,
    MAX_ROUNDS,
    MODEL,
    OUTPUT_CONTINUATION_PROMPT,
    ROUND_LIMIT_FINALIZATION_PROMPT,
    TODO_REMINDER_AFTER_ROUNDS,
    TODO_REMINDER_TEXT,
    TOOL_ROUND_WARNING_TEMPLATE,
    TOOL_ROUND_WARNING_THRESHOLD,
    WORKDIR,
    PermissionMode,
    client,
    console,
)
from ..context.compaction import (
    compact_history,
    normalize_compact_focus,
    prepare_context,
)
from ..core.provider import content_has_tool_use, extract_text
from ..harness.hooks import (
    ErrorContext,
    PostToolUseContext,
    PreToolUseContext,
    StopContext,
    UserPromptSubmitContext,
    notify_error,
    trigger_hooks,
)
from ..harness.permissions import ApprovalCallback, ask_user
from ..harness.prompts import (
    PromptCacheState,
    format_prompt_cache_observation,
    get_prompt_assembly,
)
from ..harness.todo import TODO_STATE
from ..jobs.tools import (
    dispatch_background_shell,
    render_job_completion_notification,
)
from ..memory.recall import (
    build_memory_request_messages,
    learn_memories_from_turn,
    recall_relevant_memories,
    render_recalled_memories,
)
from ..memory.store import scan_memories
from ..runtime import (
    JOB_RUNNER,
    SKILL_REGISTRY,
    MCP_REGISTRY,
    build_turn_tool_snapshot,
    build_prompt_runtime_context,
)
from .recovery import (
    RecoveryFailureKind,
    RecoveryState,
    classify_model_error,
    format_recovery_failure,
    reactive_compact,
    with_retry,
)


def append_harness_notice(messages: list[dict], notice: str) -> None:
    """把 Harness 提醒并入最近的 user 回填消息，必要时新增 user 消息。"""
    if not isinstance(notice, str) or not notice.strip():
        raise ValueError("Harness 提醒必须是非空字符串")
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, list):
            content.append({"type": "text", "text": notice})
            return
        if isinstance(content, str):
            messages[-1]["content"] = f"{content}\n\n{notice}"
            return
    messages.append({"role": "user", "content": notice})


def inject_completed_job_notifications(messages: list[dict]) -> int:
    """在 Agent 主线程中消费终态 Job，触发 Hook 并追加一次性文本事件。"""
    completed_jobs = JOB_RUNNER.collect_completed_jobs()
    for job in completed_jobs:
        post_output_parts: list[str] = []
        if job.output:
            post_output_parts.append(job.output)
        if job.error:
            post_output_parts.append(f"错误：{job.error}")
        post_output = "\n\n".join(post_output_parts)

        hook_warning: str | None = None
        post_context = PostToolUseContext(
            tool_name=job.tool_name,
            tool_input={
                "command": job.command,
                "run_in_background": True,
                "timeout": job.timeout_seconds,
            },
            tool_use_id=job.tool_use_id,
            output=post_output,
        )
        try:
            trigger_hooks("PostToolUse", post_context)
        except Exception as error:
            notify_error(ErrorContext(
                stage="PostToolUse",
                error=error,
                tool_name=job.tool_name,
                tool_use_id=job.tool_use_id,
            ))
            hook_warning = (
                "后台 Job 已经结束，但 PostToolUse Hook 执行失败："
                f"{type(error).__name__}：{error}"
            )

        notification = render_job_completion_notification(
            job,
            hook_warning=hook_warning,
        )
        append_harness_notice(messages, notification)
    return len(completed_jobs)


def pair_forbidden_finalization_tools(response: object) -> list[dict]:
    """为收尾响应中违反无工具约束的 tool_use 生成拒绝结果。"""
    tool_results: list[dict] = []
    content = (
        response.get("content")
        if isinstance(response, dict)
        else getattr(response, "content", None)
    )
    if not isinstance(content, list):
        return tool_results
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            tool_use_id = block.get("id")
            tool_name = block.get("name", "unknown")
        else:
            block_type = getattr(block, "type", None)
            tool_use_id = getattr(block, "id", None)
            tool_name = getattr(block, "name", "unknown")
        if block_type != "tool_use":
            continue
        if not isinstance(tool_use_id, str) or not tool_use_id:
            continue
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": (
                f"工具 {tool_name} 未执行：工具回合预算已经耗尽，"
                "收尾阶段禁止继续调用工具。"
            ),
            "is_error": True,
        })
    return tool_results


def agent_loop(
    query: str,
    messages: list[dict],
    max_rounds: int = MAX_ROUNDS,
    permission_mode: PermissionMode = "ask",
    approval_callback: ApprovalCallback | None = None,
    memory_enabled: bool = True,
    prompt_cache: PromptCacheState | None = None,
    checkpoint: SessionCheckpoint | None = None,
    single_turn: bool = False,
) -> str:
    """以明确事件记录回合；异常退出只保存中断状态，绝不重试工具。"""
    active = False
    completed = False

    def record(event: CheckpointEvent) -> None:
        nonlocal active, completed
        active = True
        if checkpoint is not None:
            checkpoint(messages, event)
        completed = event == "completed"

    try:
        answer = _run_agent_loop(
            query, messages, max_rounds, permission_mode, approval_callback,
            memory_enabled, prompt_cache, record, single_turn,
        )
    except SessionPersistenceError:
        raise  # 存储已熔断，不尝试覆盖最后一个成功恢复点
    except BaseException:
        if active and not completed:
            record("interrupted")
        raise
    if active and not completed:
        record("interrupted")
    return answer


def _run_agent_loop(
    query: str,
    messages: list[dict],
    max_rounds: int,
    permission_mode: PermissionMode,
    approval_callback: ApprovalCallback | None,
    memory_enabled: bool,
    prompt_cache: PromptCacheState | None,
    record: Callable[[CheckpointEvent], None],
    single_turn: bool,
) -> str:
    prompt_context = UserPromptSubmitContext(query=query)
    try:
        prompt_result = trigger_hooks("UserPromptSubmit", prompt_context)
    except Exception as error:
        notify_error(ErrorContext(stage="UserPromptSubmit", error=error))
        return f"处理用户输入时 Hook 执行失败：{type(error).__name__}：{error}"
    if prompt_result.behavior == "block":
        return prompt_result.reason

    working_prompt_cache = (
        prompt_cache
        if prompt_cache is not None
        else PromptCacheState()
    )
    try:
        tool_pool_revision, turn_tools, turn_handlers = build_turn_tool_snapshot(single_turn=single_turn)
        prompt_runtime_context = build_prompt_runtime_context(
            turn_tools,
            turn_handlers,
            SKILL_REGISTRY,
            workspace=WORKDIR,
            memory_enabled=memory_enabled,
        )
    except Exception as error:
        notify_error(ErrorContext(stage="PromptAssembly", error=error))
        return f"System Prompt 运行时上下文构建失败：{type(error).__name__}：{error}"

    messages.append({"role": "user", "content": prompt_context.query})
    record("progress")
    memory_content = ""
    if memory_enabled:
        try:
            memory_registry = scan_memories()
            memory_recalls = recall_relevant_memories(prompt_context.query, memory_registry)
            memory_content = render_recalled_memories(memory_registry, memory_recalls)
        except Exception as error:
            notify_error(ErrorContext(stage="Memory", error=error))
            console.print("Memory 初始化失败，本轮将不使用跨会话记忆。", style="yellow")
    approver = approval_callback if approval_callback is not None else ask_user
    stop_hook_active = False
    rounds_since_todo = 0
    consecutive_auto_compact_failures = 0
    recovery_state = RecoveryState(
        current_model=MODEL,
        current_max_tokens=MAIN_AGENT_DEFAULT_MAX_TOKENS,
    )
    continued_output_parts: list[str] = []
    round_budget_warning_sent = False

    # max_rounds 限制的是有工具能力的决策回合；耗尽后仅追加一次无工具收尾。
    for idx in range(max_rounds + 1):
        finalization_round = idx == max_rounds
        round_number = idx + 1
        round_label = "无工具收尾" if finalization_round else f"第 {round_number} 轮"
        rprint("=" * 40 + round_label + "：" + "=" * 40)

        try:
            completed_job_count = 0 if single_turn else inject_completed_job_notifications(messages)
        except Exception as error:
            notify_error(ErrorContext(stage="BackgroundJob", error=error))
            return f"后台 Job 完成事件处理失败：{type(error).__name__}：{error}"
        if completed_job_count:
            console.print(
                f"已向本轮上下文注入 {completed_job_count} 条后台 Job 完成通知。",
                style="green",
                markup=False,
            )

        if finalization_round:
            append_harness_notice(messages, ROUND_LIMIT_FINALIZATION_PROMPT)
        else:
            remaining_tool_rounds = max_rounds - idx
            if (
                not round_budget_warning_sent
                and remaining_tool_rounds <= TOOL_ROUND_WARNING_THRESHOLD
            ):
                append_harness_notice(
                    messages,
                    TOOL_ROUND_WARNING_TEMPLATE.format(
                        remaining=remaining_tool_rounds,
                    ),
                )
                round_budget_warning_sent = True

        record("progress")

        # -------------------- 调用模型前：便宜的上下文预处理 --------------------
        try:
            preparation = prepare_context(messages)
        except Exception as error:
            notify_error(ErrorContext(stage="ContextCompact", error=error))
            return f"上下文预处理失败：{type(error).__name__}：{error}"

        record("progress")
        if preparation.budget_applied:
            console.print("过大的工具结果已旁路保存，活跃上下文仅保留引用。", style="dim")
        if preparation.lossy_compacted:
            transcript_display = preparation.transcript_path.relative_to(WORKDIR).as_posix()
            console.print(
                f"已裁剪较早消息或工具结果：{preparation.before_chars} → "
                f"{preparation.after_chars} 字符；Transcript：{transcript_display}",
                style="dim",
            )

        # 三层零 API 处理后仍超阈值，才使用一次较贵的 LLM 语义摘要。
        request_context_chars = preparation.after_chars + len(memory_content)
        if request_context_chars > AUTO_COMPACT_CONTEXT_CHARS:
            try:
                messages[:] = compact_history(
                    messages,
                    mode="automatic",
                    transcript_path=preparation.transcript_path,
                )
            except Exception as error:
                consecutive_auto_compact_failures += 1
                notify_error(ErrorContext(stage="ContextCompact", error=error))
                console.print(
                    "自动上下文压缩失败，暂时保留当前历史继续尝试。"
                    f"（{consecutive_auto_compact_failures}/"
                    f"{MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES}）",
                    style="yellow",
                )
                if consecutive_auto_compact_failures >= MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES:
                    return (
                        "自动上下文压缩连续失败，已触发熔断器："
                        f"{type(error).__name__}：{error}"
                    )
            else:
                consecutive_auto_compact_failures = 0
        else:
            consecutive_auto_compact_failures = 0

        record("progress")

        # -------------------- 模型调用与一次性 Prompt 过长恢复 --------------------
        while True:
            try:
                # 上一批 tool_result 已全部回填；仅在新模型请求前更新能力快照。
                # 不在工具批次中途替换映射，断开后的旧 Handler 自身会拒绝调用。
                if not single_turn and tool_pool_revision != MCP_REGISTRY.revision:
                    next_revision, next_tools, next_handlers = build_turn_tool_snapshot(single_turn=False)
                    next_context = build_prompt_runtime_context(
                        next_tools, next_handlers, SKILL_REGISTRY,
                        workspace=WORKDIR, memory_enabled=memory_enabled,
                    )
                    # 构建全部成功才切换；失败终止本轮，不用旧能力继续请求模型。
                    tool_pool_revision, turn_tools, turn_handlers, prompt_runtime_context = (
                        next_revision, next_tools, next_handlers, next_context,
                    )
                prompt_lookup = get_prompt_assembly(
                    prompt_runtime_context,
                    working_prompt_cache,
                )
                prompt_observation = format_prompt_cache_observation(
                    prompt_lookup,
                    working_prompt_cache,
                )
            except Exception as error:
                notify_error(ErrorContext(stage="PromptAssembly", error=error))
                return f"System Prompt 组装失败：{type(error).__name__}：{error}"
            console.print(prompt_observation, style="dim", markup=False)
            try:
                request_messages = build_memory_request_messages(messages, memory_content)
            except Exception as error:
                notify_error(ErrorContext(stage="Memory", error=error))
                console.print("Memory 注入失败，本轮后续调用将不再注入 Memory。", style="yellow")
                memory_content = ""
                request_messages = messages
            try:
                if finalization_round:
                    response = with_retry(
                        lambda: client.messages.create(
                            model=recovery_state.current_model,
                            system=prompt_lookup.assembly.system_prompt,
                            max_tokens=recovery_state.current_max_tokens,
                            messages=request_messages,
                        ),
                        recovery_state,
                    )
                else:
                    response = with_retry(
                        lambda: client.messages.create(
                            model=recovery_state.current_model,
                            system=prompt_lookup.assembly.system_prompt,
                            max_tokens=recovery_state.current_max_tokens,
                            messages=request_messages,
                            tools=turn_tools,
                        ),
                        recovery_state,
                    )
            except Exception as error:
                error_kind = classify_model_error(error)
                notify_error(ErrorContext(stage="ModelCall", error=error))
                if (
                    error_kind == "prompt_too_long"
                    and recovery_state.reactive_compact_count < MAX_REACTIVE_RETRIES
                ):
                    recovery_state.reactive_compact_count += 1
                    try:
                        compacted_messages = reactive_compact(messages)
                    except Exception as compact_error:
                        notify_error(ErrorContext(stage="ContextCompact", error=compact_error))
                        return format_recovery_failure(
                            "reactive_compact_failed",
                            recovery_state,
                            error=compact_error,
                        )
                    messages[:] = compacted_messages
                    record("progress")
                    continue
                failure_kind: RecoveryFailureKind
                if error_kind == "prompt_too_long":
                    failure_kind = "prompt_too_long"
                elif error_kind == "rate_limit":
                    failure_kind = "rate_limit_exhausted"
                elif error_kind == "overloaded":
                    failure_kind = "overloaded_exhausted"
                else:
                    failure_kind = "fatal"
                return format_recovery_failure(
                    failure_kind,
                    recovery_state,
                    error=error,
                )
            break

        if finalization_round and content_has_tool_use(response.content):
            denied_results = pair_forbidden_finalization_tools(response)
            if denied_results:
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": denied_results})
            return (
                f"达到工具回合上限 {max_rounds}；模型在无工具收尾阶段仍请求工具，"
                "相关工具均未执行。"
            )

        if finalization_round and response.stop_reason == "max_tokens":
            messages.append({"role": "assistant", "content": response.content})
            partial_text = extract_text(response.content)
            diagnostic = (
                f"达到工具回合上限 {max_rounds}；唯一一次无工具收尾响应达到输出上限。"
            )
            return "\n\n".join(
                part for part in [partial_text, diagnostic] if part
            )

        if response.stop_reason == "max_tokens":
            if not recovery_state.has_escalated_output:
                recovery_state.current_max_tokens = MAIN_AGENT_ESCALATED_MAX_TOKENS
                recovery_state.has_escalated_output = True
                console.print(
                    "模型输出达到 Token 上限；已将最大输出预算从 "
                    f"{MAIN_AGENT_DEFAULT_MAX_TOKENS} 提升到 "
                    f"{MAIN_AGENT_ESCALATED_MAX_TOKENS}，将重新请求。",
                    style="yellow",
                )
                continue

            if content_has_tool_use(response.content):
                diagnostic = format_recovery_failure(
                    "output_tool_use_truncated",
                    recovery_state,
                )
                console.print(diagnostic, style="red")
                return "\n\n".join(
                    part for part in [*continued_output_parts, diagnostic] if part
                )

            messages.append({"role": "assistant", "content": response.content})
            partial_text = extract_text(response.content)
            if partial_text:
                continued_output_parts.append(partial_text)

            if recovery_state.continuation_count >= MAX_OUTPUT_CONTINUATIONS:
                diagnostic = format_recovery_failure(
                    "output_continuations_exhausted",
                    recovery_state,
                )
                console.print(diagnostic, style="red")
                return "\n\n".join(
                    part for part in [*continued_output_parts, diagnostic] if part
                )

            messages.append({"role": "user", "content": OUTPUT_CONTINUATION_PROMPT})
            record("progress")
            recovery_state.continuation_count += 1
            console.print(
                "模型在升级输出预算后仍被截断；正在请求续写"
                f"（{recovery_state.continuation_count}/"
                f"{MAX_OUTPUT_CONTINUATIONS}）。",
                style="yellow",
            )
            continue

        messages.append({"role": "assistant", "content": response.content})
        record("progress")  # 工具执行前必须先成功保存完整调用请求

        if response.stop_reason != "tool_use":
            final_response_text = extract_text(response.content)
            final_text = "\n".join(
                part for part in [*continued_output_parts, final_response_text] if part
            )
            stop_context = StopContext(
                messages=messages,
                final_text=final_text,
                round_number=round_number,
            )
            try:
                stop_result = trigger_hooks("Stop", stop_context)
            except Exception as error:
                notify_error(ErrorContext(stage="Stop", error=error))
                if final_text.strip():
                    record("completed")
                return final_text

            if (
                stop_result.behavior == "block"
                and not stop_hook_active
                and not finalization_round
            ):
                stop_hook_active = True
                messages.append({
                    "role": "user",
                    "content": stop_result.reason.strip() or "请继续完成任务并给出最终回答。",
                })
                record("progress")
                continue

            if stop_result.behavior == "block" and finalization_round:
                diagnostic = (
                    stop_result.reason.strip()
                    or "无工具收尾没有返回可用的最终答复。"
                )
                return "\n\n".join(
                    part for part in [final_text, diagnostic] if part
                )

            if memory_enabled:
                known_memory_secrets: tuple[str, ...]
                if isinstance(API_KEY, str) and API_KEY:
                    known_memory_secrets = (API_KEY,)
                else:
                    known_memory_secrets = ()
                try:
                    persistence_results = learn_memories_from_turn(prompt_context.query, final_text, known_memory_secrets)
                except Exception as error:
                    notify_error(ErrorContext(stage="Memory", error=error))
                    console.print("Memory 回合学习失败，最终回答不受影响。", style="yellow")
                else:
                    if persistence_results:
                        written_count = sum(result.status == "written" for result in persistence_results)
                        rejected_count = sum(result.status == "rejected" for result in persistence_results)
                        failed_count = sum(result.status == "failed" for result in persistence_results)
                        console.print(
                            "Memory 学习结果："
                            f"写入 {written_count}，"
                            f"拒绝 {rejected_count}，"
                            f"失败 {failed_count}。",
                            style="yellow" if failed_count else "dim",
                        )
            if final_text.strip():
                record("completed")
            return final_text

        # -------------------- 工具执行：始终保持 tool_use/tool_result 配对 --------------------
        todo_updated_this_round = False
        compact_requested = False
        compact_focus: str | None = None
        tool_results: list[dict] = []
        messages.append({"role": "user", "content": tool_results})

        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_input = dict(block.input)
            tool_is_error = False
            pre_context = PreToolUseContext(
                tool_name=block.name,
                tool_input=tool_input,
                tool_use_id=block.id,
                permission_mode=permission_mode,
                approver=approver,
                registered_handlers=turn_handlers,
            )

            try:
                pre_result = trigger_hooks("PreToolUse", pre_context)
            except Exception as error:
                notify_error(ErrorContext(
                    stage="PreToolUse",
                    error=error,
                    tool_name=block.name,
                    tool_use_id=block.id,
                ))
                output = (
                    f"工具 {block.name} 未执行；"
                    f"执行前 Hook 发生异常：{type(error).__name__}：{error}；"
                    "规则 ID：hook.pre_tool_use.error。"
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": True,
                })
                record("progress")
                continue

            if pre_result.behavior == "block":
                tool_is_error = True
                output = (
                    f"工具 {block.name} 被拒绝执行；"
                    f"拒绝原因：{pre_result.reason}；"
                    f"规则 ID：{pre_result.rule_id or 'hook.pre_tool_use.block'}；"
                    "工具未执行。"
                )
            elif pre_result.behavior == "continue":
                handler = turn_handlers.get(block.name)
                if not callable(handler):
                    tool_is_error = True
                    handler_error = RuntimeError(f"工具 {block.name} 没有可调用的处理函数")
                    notify_error(ErrorContext(
                        stage="ToolExecution",
                        error=handler_error,
                        tool_name=block.name,
                        tool_use_id=block.id,
                    ))
                    output = f"工具 {block.name} 注册信息错误，找不到可调用的处理函数。"
                else:
                    revision_before_tool = TODO_STATE.revision
                    handler_input = dict(tool_input)
                    tool_completed_synchronously = True
                    try:
                        background_requested = False
                        if block.name == "shell":
                            raw_background = handler_input.pop(
                                "run_in_background",
                                False,
                            )
                            if type(raw_background) is not bool:
                                raise ValueError(
                                    "shell.run_in_background 必须是布尔值"
                                )
                            background_requested = raw_background
                            if single_turn and background_requested:
                                raise ValueError("一次性 ask 模式不支持后台执行；请同步执行或使用 miniclaude chat")

                        if background_requested:
                            command = handler_input.get("command")
                            if not isinstance(command, str) or not command.strip():
                                raise ValueError("后台 Shell 命令必须是非空字符串")
                            raw_timeout = handler_input.pop(
                                "timeout",
                                DEFAULT_JOB_TIMEOUT_SECONDS,
                            )
                            if (
                                isinstance(raw_timeout, bool)
                                or not isinstance(raw_timeout, (int, float))
                            ):
                                raise ValueError("后台 Shell timeout 必须是数字")
                            output = dispatch_background_shell(
                                tool_use_id=block.id,
                                command=command,
                                timeout_seconds=float(raw_timeout),
                                runner=JOB_RUNNER,
                            )
                            tool_completed_synchronously = False
                        else:
                            output = handler(**handler_input)
                        output, tool_is_error = normalize_tool_output(output)
                    except Exception as error:
                        reported_error = (
                            RuntimeError(f"外部 MCP 调用失败：{type(error).__name__}；执行状态可能未知")
                            if isinstance(handler, MCPToolHandler) else error
                        )
                        notify_error(ErrorContext(
                            stage="ToolExecution",
                            error=reported_error,
                            tool_name=block.name,
                            tool_use_id=block.id,
                        ))
                        if isinstance(handler, MCPToolHandler):
                            output = f"外部 MCP 工具调用失败：{type(error).__name__}；执行状态可能未知，不自动重试。"
                        else:
                            output = f"工具 {block.name} 执行失败：{type(error).__name__}：{error}"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output,
                            "is_error": True,
                        })
                        record("progress")
                        continue

                    # compact 只是先登记；必须等本轮所有工具结果配对完成后才能替换历史。
                    if block.name == "compact":
                        compact_requested = True
                        requested_focus = normalize_compact_focus(tool_input.get("focus"))
                        if requested_focus is not None:
                            compact_focus = requested_focus
                    if block.name == "todo_write" and TODO_STATE.revision > revision_before_tool:
                        todo_updated_this_round = True

                    if tool_completed_synchronously:
                        post_context = PostToolUseContext(
                            tool_name=block.name,
                            tool_input=tool_input,
                            tool_use_id=block.id,
                            output=output,
                            is_error=tool_is_error,
                        )
                        try:
                            trigger_hooks("PostToolUse", post_context)
                        except Exception as error:
                            notify_error(ErrorContext(stage="PostToolUse", error=error, tool_name=block.name, tool_use_id=block.id))
                            output = (
                                f"{output}\n\n"
                                "警告：工具已返回结果，但执行后 Hook 失败："
                                f"{type(error).__name__}：{error}"
                            )

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
                "is_error": tool_is_error,
            })
            record("progress")

        if todo_updated_this_round:
            rounds_since_todo = 0
        else:
            rounds_since_todo += 1
        if rounds_since_todo >= TODO_REMINDER_AFTER_ROUNDS:
            tool_results.append({"type": "text", "text": TODO_REMINDER_TEXT})
            rounds_since_todo = 0
        record("progress")

        # -------------------- 主动 compact：在完整工具回合之后事务式提交 --------------------
        if compact_requested:
            try:
                compacted_messages = compact_history(messages, focus=compact_focus, mode="manual")
            except Exception as error:
                notify_error(ErrorContext(stage="ContextCompact", error=error))
                # tool_results 已经与 tool_use 配对；在同一 user 消息中补充失败状态即可继续。
                tool_results.append({
                    "type": "text",
                    "text": (
                        "<context-compact-error>"
                        f"主动上下文压缩失败：{type(error).__name__}：{error}。"
                        "当前历史未被替换。"
                        "</context-compact-error>"
                    ),
                })
            else:
                messages[:] = compacted_messages
            record("progress")

    return (
        f"达到工具回合上限 {max_rounds}，"
        "且唯一一次无工具收尾未能生成可用最终答复。"
    )
