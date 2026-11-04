"""恢复模块：维护恢复状态、分类模型错误，并执行保持消息原子性的应急压缩。"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

from ..config import (
    BASE_RETRY_DELAY_SECONDS,
    FALLBACK_MODEL,
    MAX_CONSECUTIVE_529,
    MAX_RETRY_DELAY_SECONDS,
    MAX_TRANSIENT_RETRIES,
    REACTIVE_KEEP_RECENT_MESSAGES,
    RETRY_JITTER_RATIO,
    WORKDIR,
    console,
)
from ..context.compaction import (
    build_context_summary_message,
    compact_history,
    summarize_history,
)
from ..context.messages import message_has_tool_result, message_has_tool_use
from ..context.transcript import make_transcript_snapshot, write_transcript

RecoveryKind = Literal["prompt_too_long", "rate_limit", "overloaded", "fatal"] # 模型调用错误分类
RecoveryFailureKind = Literal[
    "prompt_too_long",
    "reactive_compact_failed",
    "rate_limit_exhausted",
    "overloaded_exhausted",
    "fatal",
    "output_tool_use_truncated",
    "output_continuations_exhausted",
]  # 恢复最终失败原因
T = TypeVar("T") # 重试操作返回的类型
SleepFn = Callable[[float], None]
JitterFn = Callable[[float, float], float]

@dataclass
class RecoveryState:
    """记录一次主 Agent 运行中已经消耗的恢复机会。"""
    current_model: str  # 当前模型；发生连续 529 时可以切换为备用模型
    current_max_tokens: int  # 当前主模型请求的最大输出 Token 数
    has_escalated_output: bool = False  # 是否已经使用过一次输出预算升级
    continuation_count: int = 0  # 输出升级后仍被截断时，已执行的续写次数
    reactive_compact_count: int = 0  # Prompt 过长后已尝试的应急压缩次数
    transient_retry_count: int = 0  # 本次 Agent 运行累计消耗的瞬时错误重试次数
    consecutive_529: int = 0  # 当前连续出现的 529/overloaded 次数
    fallback_activated: bool = False  # 是否已经因连续过载切换到备用模型


def format_recovery_failure(kind: RecoveryFailureKind, state: RecoveryState, *, error: Exception | None = None) -> str:
    """将恢复终止原因、已消耗策略和原始异常渲染为稳定的 CLI 诊断。"""
    failure_messages: dict[RecoveryFailureKind, str] = {
        "prompt_too_long": "模型上下文在应急压缩后仍超过上限。",
        "reactive_compact_failed": "模型上下文过长，且应急压缩执行失败。",
        "rate_limit_exhausted": "模型持续返回 429 限流，瞬时错误重试预算已经耗尽。",
        "overloaded_exhausted": "模型持续返回 529 过载，瞬时错误重试预算已经耗尽。",
        "fatal": "模型调用发生不可恢复错误，未继续重试。",
        "output_tool_use_truncated": (
            "模型在包含工具调用的输出中达到 Token 上限；"
            "为保持 tool_use/tool_result 配对，截断响应未写入历史，工具也未执行。"
        ),
        "output_continuations_exhausted": "模型输出在有限次续写后仍被截断。",
    }
    yes_or_no = {True: "是", False: "否"}
    lines = [
        f"恢复终止：{failure_messages[kind]}",
        (
            "恢复状态："
            f"model={state.current_model}；"
            f"max_tokens={state.current_max_tokens}；"
            f"输出预算已升级={yes_or_no[state.has_escalated_output]}；"
            f"续写次数={state.continuation_count}；"
            f"应急压缩次数={state.reactive_compact_count}；"
            f"瞬时重试次数={state.transient_retry_count}；"
            f"已切换备用模型={yes_or_no[state.fallback_activated]}"
        ),
    ]
    if error is not None:
        error_message = " ".join(str(error).split()) or "无异常信息"
        lines.append(f"原始错误：{type(error).__name__}：{error_message}")
    return "\n".join(lines)


def _extract_status_code(error: Exception) -> int | None:
    """兼容从异常本身或其 response 对象读取 HTTP 状态码。"""
    direct_status = getattr(error, "status_code", None)
    if isinstance(direct_status, int) and not isinstance(direct_status, bool):
        return direct_status
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int) and not isinstance(response_status, bool):
        return response_status
    return None


def _normalize_error_text(error: Exception) -> str:
    """汇总异常类名和常见正文属性，供兼容性字符串判断使用。"""
    return " ".join(
        str(value)
        for value in (
            type(error).__name__,
            error,
            getattr(error, "body", None),
            getattr(error, "message", None),
        )
        if value is not None
    ).casefold()


def is_prompt_too_long_error(error: Exception) -> bool:
    """识别 Anthropic/OpenRouter 常见的上下文过长错误表达。"""
    if _extract_status_code(error) == 413:
        return True
    error_text = _normalize_error_text(error)
    indicators = (
        "prompt_too_long",
        "prompt is too long",
        "too many tokens",
        "context length exceeded",
        "maximum context length",
        "exceeds the context window",
        "input is too long",
    )
    return any(indicator in error_text for indicator in indicators)


def classify_model_error(error: Exception) -> RecoveryKind:
    """将模型调用异常分类为一种恢复路径；无法确认可恢复时安全地归为 fatal。"""
    if is_prompt_too_long_error(error):
        return "prompt_too_long"
    status_code = _extract_status_code(error)
    error_text = _normalize_error_text(error)
    if status_code == 429:
        return "rate_limit"
    if status_code == 529:
        return "overloaded"
    rate_limit_indicators = (
        "ratelimit",
        "rate_limit",
        "rate limit",
        "too many requests",
        "error code: 429",
        "status 429",
        "http 429",
    )
    if any(indicator in error_text for indicator in rate_limit_indicators):
        return "rate_limit"
    overloaded_indicators = (
        "overloaded",
        "overload_error",
        "high demand",
        "error code: 529",
        "status 529",
        "http 529",
    )
    if any(indicator in error_text for indicator in overloaded_indicators):
        return "overloaded"
    return "fatal"


def _extract_retry_after_seconds(error: Exception) -> float | None:
    """读取数值形式的 Retry-After 秒数；无效或不支持的值交给本地退避策略。"""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(error, "headers", None)
    get_header = getattr(headers, "get", None)
    if not callable(get_header):
        return None
    raw_value = get_header("retry-after")
    if raw_value is None:
        raw_value = get_header("Retry-After")
    try:
        retry_after = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(retry_after) or retry_after < 0:
        return None
    return retry_after


def _as_non_negative_finite_number(value: object, name: str) -> float:
    """将数值边界参数规范化为有限的非负浮点数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是非负有限数字")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} 必须是非负有限数字")
    return normalized


def retry_delay(
    attempt: int,
    *,
    jitter_fn: JitterFn = random.uniform,
    retry_after: float | None = None,
) -> float:
    """计算一次指数退避延迟；服务端 Retry-After 优先于本地抖动。"""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt 必须是非负整数")
    if retry_after is not None:
        return _as_non_negative_finite_number(retry_after, "retry_after")
    if not callable(jitter_fn):
        raise TypeError("jitter_fn 必须可调用")

    try:
        exponential_delay = math.ldexp(BASE_RETRY_DELAY_SECONDS, attempt)
    except OverflowError:
        exponential_delay = MAX_RETRY_DELAY_SECONDS
    base_delay = min(exponential_delay, MAX_RETRY_DELAY_SECONDS)
    max_jitter = base_delay * RETRY_JITTER_RATIO
    jitter = _as_non_negative_finite_number(
        jitter_fn(0.0, max_jitter),
        "jitter_fn 返回值",
    )
    if jitter > max_jitter:
        raise ValueError("jitter_fn 返回值超出允许的抖动范围")
    return base_delay + jitter


def maybe_activate_fallback(state: RecoveryState) -> bool:
    """连续 529 达到阈值后至多切换一次备用模型。"""
    if state.consecutive_529 < MAX_CONSECUTIVE_529:
        return False
    if state.fallback_activated:
        return False
    if not isinstance(FALLBACK_MODEL, str) or not FALLBACK_MODEL.strip():
        return False

    fallback_model = FALLBACK_MODEL.strip()
    if fallback_model == state.current_model:
        return False

    state.current_model = fallback_model
    state.fallback_activated = True
    state.consecutive_529 = 0
    return True


def with_retry(operation: Callable[[], T], state: RecoveryState, *, sleep_fn: SleepFn = time.sleep, jitter_fn: JitterFn = random.uniform) -> T:
    """只对模型调用的 429/529 异常执行受总预算约束的退避重试。"""
    if not callable(operation):
        raise TypeError("operation 必须可调用")
    if not callable(sleep_fn):
        raise TypeError("sleep_fn 必须可调用")
    if not callable(jitter_fn):
        raise TypeError("jitter_fn 必须可调用")
    attempt = 0
    while True:
        try:
            result = operation()
        except Exception as error:
            error_kind = classify_model_error(error)
            if error_kind not in ("rate_limit", "overloaded"):
                raise
            if error_kind == "overloaded":
                state.consecutive_529 += 1
            else:
                state.consecutive_529 = 0
            if state.transient_retry_count >= MAX_TRANSIENT_RETRIES:
                raise

            previous_model = state.current_model
            fallback_activated = (
                error_kind == "overloaded"
                and maybe_activate_fallback(state)
            )
            delay = retry_delay(attempt, jitter_fn=jitter_fn, retry_after=_extract_retry_after_seconds(error))
            state.transient_retry_count += 1
            attempt += 1
            if fallback_activated:
                console.print(
                    f"模型连续 {MAX_CONSECUTIVE_529} 次返回 529；"
                    f"已从 {previous_model} 切换到备用模型 {state.current_model}。",
                    style="yellow",
                )
            error_label = "529 过载" if error_kind == "overloaded" else "429 限流"
            console.print(
                f"模型调用遇到 {error_label}；将在 {delay:.2f} 秒后重试"
                f"（总预算 {state.transient_retry_count}/{MAX_TRANSIENT_RETRIES}）。",
                style="yellow",
            )
            sleep_fn(delay)
        else:
            state.consecutive_529 = 0
            return result


def reactive_compact(messages: list[dict]) -> list[dict]:
    """在 Prompt 过长后总结旧历史，并原样保留最近的原子消息组。"""
    if not messages:
        raise ValueError("没有可供应急压缩的消息历史")
    snapshot = make_transcript_snapshot(messages)
    tail_start = max(0, len(snapshot) - REACTIVE_KEEP_RECENT_MESSAGES)
    # 若切口正好落在 tool_use/tool_result 之间，将 assistant 一并放入保留尾部。
    if (
        tail_start > 0
        and message_has_tool_result(snapshot[tail_start])
        and message_has_tool_use(snapshot[tail_start - 1])
    ):
        tail_start -= 1
    # 历史太短却仍超限时，通常是单条消息过大，只能对全部历史做语义压缩。
    if tail_start == 0:
        return compact_history(snapshot, mode="reactive")
    saved_transcript = write_transcript(messages)
    older_history = snapshot[:tail_start]
    recent_tail = snapshot[tail_start:]
    summary = summarize_history(older_history, focus="保留当前任务目标、已完成变更、关键证据、用户约束和未完成工作")
    result = [build_context_summary_message(summary, saved_transcript, "reactive"), *recent_tail]
    relative_transcript = saved_transcript.relative_to(WORKDIR).as_posix()
    console.print(f"已执行应急上下文压缩；脱敏 Transcript：{relative_transcript}", style="yellow")
    return result
