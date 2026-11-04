"""原子文件提交模块：统一同目录暂存、持久化刷新、权限保留与替换清理。"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from .paths import is_path_within_root


def atomic_replace_bytes(
    path: Path,
    content: bytes,
    *,
    allowed_root: Path,
    preserve_mode: bool = True,
    replace_existing: bool = True,
) -> Path:
    """将完整字节写入同目录临时文件，再原子替换允许根目录内的目标文件。"""
    if not isinstance(path, Path):
        raise TypeError("原子替换路径必须是 pathlib.Path 类型")
    if not isinstance(content, bytes):
        raise TypeError("字节原子替换内容必须是 bytes 类型")
    if not isinstance(allowed_root, Path):
        raise TypeError("原子替换允许根目录必须是 pathlib.Path 类型")
    if type(preserve_mode) is not bool:
        raise TypeError("preserve_mode 必须是布尔值")
    if type(replace_existing) is not bool:
        raise TypeError("replace_existing 必须是布尔值")

    resolved_root = allowed_root.resolve()
    target_path = path.resolve()
    if not is_path_within_root(target_path, resolved_root):
        raise ValueError(f"原子替换路径 {target_path} 不在允许目录 {resolved_root} 内")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target_path.parent.resolve()
    if not is_path_within_root(resolved_parent, resolved_root):
        raise ValueError(f"原子替换目标目录 {resolved_parent} 不在允许目录 {resolved_root} 内")
    target_path = resolved_parent / target_path.name

    existing_mode: int | None = None
    if target_path.exists():
        if not target_path.is_file():
            raise ValueError(f"原子替换目标 {target_path} 不是文件")
        if not replace_existing:
            raise FileExistsError(f"原子替换目标已经存在：{target_path}")
        if preserve_mode:
            existing_mode = stat.S_IMODE(target_path.stat().st_mode)

    temporary_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary_path, "xb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            os.fsync(temporary_file.fileno())
        if not replace_existing and target_path.exists():
            raise FileExistsError(f"原子替换目标已经存在：{target_path}")
        os.replace(temporary_path, target_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            # 清理失败不能覆盖真正的写入或替换异常。
            pass
    return target_path


def atomic_replace_text(
    path: Path,
    content: str,
    *,
    allowed_root: Path,
    preserve_mode: bool = True,
    replace_existing: bool = True,
) -> Path:
    """将字符串编码为精确的 UTF-8 字节，并复用字节接口完成原子替换。"""
    if not isinstance(content, str):
        raise TypeError("文本原子替换内容必须是字符串类型")
    return atomic_replace_bytes(
        path,
        content.encode("utf-8"),
        allowed_root=allowed_root,
        preserve_mode=preserve_mode,
        replace_existing=replace_existing,
    )
