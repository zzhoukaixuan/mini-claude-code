"""Worktree 名称校验：供 Task 模型与目录服务共用，不执行 Git 或磁盘操作。"""

from ..config import MAX_WORKTREE_NAME_LENGTH, WORKTREE_NAME_PATTERN, WORKTREE_RESERVED_NAMES


def validate_worktree_name(name: str) -> str:
    """合法则原样返回；不自动修剪或替换字符，避免创建名称偏离用户输入。"""
    if not isinstance(name, str):
        raise TypeError("Worktree 名称必须是字符串")
    if not 1 <= len(name) <= MAX_WORKTREE_NAME_LENGTH:
        raise ValueError(f"Worktree 名称长度必须为 1～{MAX_WORKTREE_NAME_LENGTH}")
    if WORKTREE_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("Worktree 名称必须以英文字母或数字开头，其余仅允许字母、数字、下划线和连字符")
    if name.upper() in WORKTREE_RESERVED_NAMES:
        raise ValueError(f"Worktree 名称 {name!r} 是 Windows 保留设备名")
    return name
