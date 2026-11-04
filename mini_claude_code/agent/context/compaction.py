"""语义压缩模块：事务式预处理上下文、生成摘要并构造可审计的 context-summary 消息。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import (
    MAX_COMPACT_FOCUS_CHARS,
    MICRO_COMPACT_TRIGGER_CHARS,
    MODEL,
    SUMMARY_MAX_INPUT_CHARS,
    SUMMARY_MAX_TOKENS,
    SUMMARY_SYSTEM,
    TRANSCRIPT_DIR,
    WORKDIR,
    client,
    console,
)
from ..core.provider import extract_text
from .messages import micro_compact, snip_compact, tool_result_budget
from .transcript import (
    make_redacted_transcript_snapshot,
    make_transcript_snapshot,
    write_transcript,
)


@dataclass(frozen=True)
class ContextPreparationResult:
    """记录一次零 API 上下文预处理实际发生的变化。"""

    before_chars: int  # 上下文预处理前的估算字符数
    after_chars: int  # 上下文预处理后的估算字符数
    budget_applied: bool  # 是否执行过基于预算的无损结果持久化
    lossy_compacted: bool  # 是否执行过 Micro、Snip 或语义摘要等有损压缩
    transcript_path: Path | None = None  # 有损压缩前保存的脱敏 Transcript 路径


def estimate_context_chars(messages: list[dict]) -> int:
    """用 JSON 字符数近似活跃消息大小；这里只是触发阈值，不是精确 Token 数。"""
    snapshot = make_transcript_snapshot(messages)
    serialized = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return len(serialized)


def prepare_context(messages: list[dict]) -> ContextPreparationResult:
    """按 budget → snip → micro 顺序预处理，并在有损提交前保存 Transcript。"""
    if not isinstance(messages, list):
        raise TypeError("消息历史必须是列表")

    before_snapshot = make_transcript_snapshot(messages)
    before_chars = estimate_context_chars(messages)

    # budget 是可恢复转换：完整大输出先落盘，再把活跃上下文替换成引用。
    tool_result_budget(messages)
    budget_snapshot = make_transcript_snapshot(messages)
    budget_applied = budget_snapshot != before_snapshot

    # snip 与 micro 都会丢掉活跃上下文细节，因此先在独立副本上计算候选结果。
    candidate = make_transcript_snapshot(messages)
    candidate = snip_compact(candidate)
    # Micro 是有损优化，不应在短上下文中仅因工具调用数量增加就持续抹去证据。
    # 达到真实字符压力后才压缩，并继续由 micro_compact 保证批次原子性。
    if estimate_context_chars(candidate) > MICRO_COMPACT_TRIGGER_CHARS:
        candidate = micro_compact(candidate)
    lossy_compacted = candidate != budget_snapshot

    transcript_path: Path | None = None
    if lossy_compacted:
        # 只有完整 Transcript 成功提交后，才把有损候选结果替换进共享 messages。
        transcript_path = write_transcript(messages)
        messages[:] = candidate

    return ContextPreparationResult(
        before_chars=before_chars,
        after_chars=estimate_context_chars(messages),
        budget_applied=budget_applied,
        lossy_compacted=lossy_compacted,
        transcript_path=transcript_path,
    )


# -------------------- LLM 语义摘要与主动 compact 工具 --------------------

CompactMode = Literal["automatic", "manual", "reactive"]  # 上下文压缩的触发方式


def normalize_compact_focus(focus: str | None) -> str | None:
    """校验并规范 compact 的可选摘要重点。"""
    if focus is None:
        return None
    if not isinstance(focus, str):
        raise ValueError("compact 的 focus 必须是字符串")
    normalized = focus.strip()
    if len(normalized) > MAX_COMPACT_FOCUS_CHARS:
        raise ValueError(f"compact 的 focus 不能超过 {MAX_COMPACT_FOCUS_CHARS} 个字符")
    return normalized or None


def run_compact_request(focus: str = "") -> str:
    """登记模型的压缩请求；真正压缩要等本轮 tool_result 完成配对后执行。"""
    normalized_focus = normalize_compact_focus(focus)
    if normalized_focus is None:
        return "上下文压缩请求已登记，将在本轮工具结果完成配对后执行。"
    return (
        "上下文压缩请求已登记，将在本轮工具结果完成配对后执行；"
        f"摘要重点：{normalized_focus}"
    )


def render_summary_source(messages: list[dict]) -> str:
    """生成摘要模型的脱敏输入；过长时同时保留首尾证据。"""
    snapshot = make_redacted_transcript_snapshot(messages)
    serialized = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(serialized) <= SUMMARY_MAX_INPUT_CHARS:
        return serialized

    marker = "\n...[摘要输入过长，中间内容已截断；完整记录见 Transcript]...\n"
    if SUMMARY_MAX_INPUT_CHARS <= len(marker):
        return serialized[:SUMMARY_MAX_INPUT_CHARS]
    available_chars = SUMMARY_MAX_INPUT_CHARS - len(marker)
    head_chars = available_chars // 2
    tail_chars = available_chars - head_chars
    return serialized[:head_chars] + marker + serialized[-tail_chars:]


def summarize_history(messages: list[dict], focus: str | None = None) -> str:
    """调用一个不带工具的模型请求，总结即将移出活跃上下文的历史。"""
    if not messages:
        raise ValueError("没有可供摘要的消息历史")
    normalized_focus = normalize_compact_focus(focus)
    focus_instruction = (
        f"\n额外摘要重点：{normalized_focus}"
        if normalized_focus is not None
        else ""
    )
    prompt = (
        "请总结下面即将从 Coding Agent 活跃上下文中移除的会话历史。\n"
        "必须保留：\n"
        "1. 当前用户目标与验收条件；\n"
        "2. 已确认的关键事实、证据路径和技术决策；\n"
        "3. 已读取、创建或修改的文件，以及真实执行结果；\n"
        "4. 尚未完成的工作、失败尝试和下一步；\n"
        "5. 用户约束、权限决定和不得重复执行的有副作用操作。\n"
        "明确区分事实与推断，保持紧凑但具体。只输出摘要正文。"
        f"{focus_instruction}\n\n"
        "<conversation-history>\n"
        f"{render_summary_source(messages)}\n"
        "</conversation-history>"
    )
    response = client.messages.create(
        model=MODEL,
        system=SUMMARY_SYSTEM,
        max_tokens=SUMMARY_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        # 故意不传 tools：摘要模型只能返回文本，不能产生新的工作区副作用。
    )
    summary = extract_text(response.content).strip()
    if not summary:
        raise RuntimeError("摘要模型没有返回可用文本")
    return summary


def validate_existing_transcript(transcript_path: Path) -> Path:
    """验证可复用的 Transcript 确实是内部归档目录中的普通文件。"""
    if not isinstance(transcript_path, Path):
        raise TypeError("Transcript 路径必须是 Path")
    resolved_dir = TRANSCRIPT_DIR.resolve()
    if not resolved_dir.is_relative_to(WORKDIR):
        raise ValueError("Transcript 归档目录不在工作区内")
    resolved_path = transcript_path.resolve()
    if not resolved_path.is_relative_to(resolved_dir):
        raise ValueError("要复用的 Transcript 不在归档目录内")
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Transcript 不存在：{resolved_path}")
    return resolved_path


def build_context_summary_message(
    summary: str,
    transcript_path: Path,
    mode: CompactMode,
) -> dict:
    """把摘要包装成下一轮模型可以识别的 user 消息。"""
    mode_labels = {
        "automatic": "自动压缩",
        "manual": "主动压缩",
        "reactive": "应急压缩",
    }
    if mode not in mode_labels:
        raise ValueError(f"未知的上下文压缩模式：{mode}")
    relative_transcript = transcript_path.relative_to(WORKDIR).as_posix()
    return {
        "role": "user",
        "content": (
            f'<context-summary mode="{mode}">\n'
            f"压缩方式：{mode_labels[mode]}\n"
            f"脱敏 Transcript：{relative_transcript}\n"
            "下面的摘要只替代已移出的较早历史。未记录的细节需要重新核验，"
            "不要自动重放有副作用的操作。\n\n"
            f"{summary}\n"
            "</context-summary>"
        ),
    }


def compact_history(messages: list[dict], focus: str | None = None,
                    mode: CompactMode = "automatic",
                    transcript_path: Path | None = None,) -> list[dict]:
    """保存完整历史并用一条语义摘要替换它；失败时不修改原 messages。"""
    if not messages:
        raise ValueError("没有可压缩的消息历史")
    normalized_focus = normalize_compact_focus(focus)
    if transcript_path is None:
        saved_transcript = write_transcript(messages)
    else:
        saved_transcript = validate_existing_transcript(transcript_path)

    # 摘要成功前只产生审计归档，不触碰共享消息列表，保证压缩提交具备事务性。
    summary = summarize_history(messages, normalized_focus)
    compacted_messages = [
        build_context_summary_message(summary, saved_transcript, mode)
    ]
    relative_transcript = saved_transcript.relative_to(WORKDIR).as_posix()
    console.print(
        f"上下文已压缩；脱敏 Transcript：{relative_transcript}",
        style="dim cyan",
    )
    return compacted_messages
