"""Transcript 归档：复用纯转换与脱敏函数，将压缩前历史原子保存为 JSONL。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..config import TRANSCRIPT_DIR, WORKDIR
from ..storage.atomic import atomic_replace_text
from .serialization import (
    collect_transcript_secrets, make_redacted_transcript_snapshot, make_transcript_snapshot,
    redact_sensitive_data, to_json_compatible,
)


# -------------------- Transcript 原子写入 --------------------

def write_transcript(messages: list[dict]) -> Path:
    """将压缩前的脱敏消息历史原子写入唯一的 JSONL 文件。"""
    if not isinstance(messages, list) or not messages:
        raise ValueError("Transcript 至少需要包含一条消息")

    # 先完成全部转换、脱敏和序列化；任何一步失败都不会留下半个 Transcript。
    snapshot = make_redacted_transcript_snapshot(messages)
    serialized_lines = [
        json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        for message in snapshot
    ]
    payload = "\n".join(serialized_lines) + "\n"

    # 内部归档目录也必须遵守工作区边界，防止目录被替换成指向外部的链接。
    resolved_transcript_dir = TRANSCRIPT_DIR.resolve()
    if not resolved_transcript_dir.is_relative_to(WORKDIR):
        raise ValueError("Transcript 目录不在工作区内")
    resolved_transcript_dir.mkdir(parents=True, exist_ok=True)
    resolved_transcript_dir = TRANSCRIPT_DIR.resolve()
    if not resolved_transcript_dir.is_relative_to(WORKDIR):
        raise ValueError("Transcript 目录创建后越出了工作区")

    unique_suffix = f"{time.time_ns()}_{uuid.uuid4().hex}"
    target_path = (resolved_transcript_dir / f"transcript_{unique_suffix}.jsonl").resolve()
    if not target_path.is_relative_to(resolved_transcript_dir):
        raise ValueError("Transcript 文件不在指定归档目录内")
    if target_path.exists():
        raise FileExistsError(f"Transcript 文件已存在：{target_path.name}")

    return atomic_replace_text(
        target_path,
        payload,
        allowed_root=resolved_transcript_dir,
        preserve_mode=False,
        replace_existing=False,
    )
