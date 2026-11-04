"""Worktree 路径基础：校验名称、只读识别仓库，并推导和预检查隔离目录的创建目标。"""

from __future__ import annotations

import stat
from pathlib import Path

from ..config import (
    WORKTREE_DIRECTORY_NAME,
    WORKTREE_BRANCH_PREFIX,
)
from ..storage.paths import is_path_within_root
from .models import RepositoryLayout, WorktreeTarget
from .git import run_git
from .names import validate_worktree_name as validate_worktree_name  # 保留第一阶段的导入入口


def discover_repository(workspace: Path) -> RepositoryLayout:
    """识别 workspace 所属的当前 Git 工作树，不创建仓库、不检查或创建 HEAD。

    仅在请求 Worktree 能力时调用；不要在模块导入或普通 CLI 启动时执行。
    使用子进程 cwd，不修改全局 WORKDIR 或进程工作目录。linked worktree
    中的 .git 可以是文件，因此交由 Git 识别，不手工判断 .git 文件夹。
    """
    if not isinstance(workspace, Path):
        raise TypeError("工作目录必须是 Path")
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise NotADirectoryError(f"启动工作目录不是目录：{workspace}")

    output = run_git(["rev-parse", "--show-toplevel"], cwd=workspace)

    # 只去掉命令的行终止符，不 strip 路径中的合法空格。
    root_text = output.rstrip("\r\n")
    if not root_text or "\n" in root_text or "\r" in root_text:
        raise ValueError("Git 未返回唯一的工作树根目录路径")
    root_path = Path(root_text)
    if not root_path.is_absolute():
        raise ValueError("Git 返回的工作树根目录不是绝对路径")
    repository_root = root_path.resolve(strict=True)
    if not repository_root.is_dir():
        raise NotADirectoryError(f"Git 返回的工作树根目录不是目录：{repository_root}")
    try:
        project_relative = workspace.relative_to(repository_root)
    except ValueError as error:
        raise ValueError("启动工作目录不在 Git 返回的工作树根目录内") from error
    return RepositoryLayout(repository_root, workspace, project_relative)


def _require_plain_directory_if_present(path: Path) -> None:
    """允许目录尚不存在；已有项必须是普通目录，错误不伪装成不存在。"""
    try:
        info = path.lstat()  # 不跟随链接，悬空链接也必须被识别
    except FileNotFoundError:
        return
    # Python 3.11 没有 Path.is_junction；Windows 重解析点属性可覆盖 junction。
    # 保守拒绝所有重解析点（也包括部分云盘占位目录），不尝试猜测其安全性。
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ValueError(f"Worktree 管理路径不能是符号链接或重解析点：{path}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"Worktree 管理路径不是目录：{path}")


def prepare_worktree_target(layout: RepositoryLayout, name: str) -> WorktreeTarget:
    """只读推导创建目标，不建目录、不调用 Git、不取得写入授权。

    这是当前时刻的预检查，不能消除检查与使用之间的竞态。真正创建前须
    重新检查；检出完成后也须重新确认项目目录存在且仍位于新工作树内。
    """
    if not isinstance(layout, RepositoryLayout):
        raise TypeError("layout 必须是 RepositoryLayout")
    name = validate_worktree_name(name)

    _require_plain_directory_if_present(layout.repository_root)
    repository_root = layout.repository_root.resolve(strict=True)
    # 布局生成后根路径可能被替换；双向包含表示相同目录，兼容 Windows 扩展前缀。
    if not (
        is_path_within_root(repository_root, layout.repository_root)
        and is_path_within_root(layout.repository_root, repository_root)
    ):
        raise ValueError("仓库根路径发生重定向，请重新识别仓库布局")

    management_path = repository_root / WORKTREE_DIRECTORY_NAME
    _require_plain_directory_if_present(management_path)
    management_root = management_path.resolve()
    if not (
        is_path_within_root(management_root, management_path)
        and is_path_within_root(management_path, management_root)
    ):
        raise ValueError("Worktree 管理目录发生路径重定向")
    if not is_path_within_root(management_root, repository_root):
        raise ValueError("Worktree 管理目录越出仓库根目录")

    target_path = management_root / name
    try:
        target_path.lstat()  # exists() 会漏掉悬空链接，因此不能用于允许创建的判断
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"Worktree 目标已被占用，不能复用或覆盖：{target_path}")
    target_root = target_path.resolve()
    if not is_path_within_root(target_root, management_root) or is_path_within_root(
        management_root, target_root,
    ):
        raise ValueError("Worktree 目标必须是管理目录内的独立子目录")
    workspace = (target_root / layout.project_relative).resolve()
    if not is_path_within_root(workspace, target_root):
        raise ValueError("队友项目执行目录越出新工作树")

    return WorktreeTarget(name, WORKTREE_BRANCH_PREFIX + name, target_root, workspace)
