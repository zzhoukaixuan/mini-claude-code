"""Worktree 数据模型：记录仓库布局和工作树目标，只校验字段关系，不访问磁盘。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import WORKTREE_BRANCH_PREFIX, TaskStatus
from ..storage.paths import is_path_within_root


@dataclass(frozen=True)
class RepositoryLayout:
    """只校验路径之间的结构关系；磁盘存在性及 Git 归属由发现函数确认。"""

    repository_root: Path  # 当前 Git 工作树的绝对根目录，不强行回溯到主工作树
    workspace: Path  # 解析后的启动工作目录，允许是仓库的子目录
    project_relative: Path  # workspace 相对于 repository_root 的路径；根目录启动时为 Path('.')

    def __post_init__(self) -> None:
        if not all(isinstance(path, Path) for path in (
            self.repository_root, self.workspace, self.project_relative,
        )):
            raise TypeError("RepositoryLayout 的路径字段必须是 Path")
        if not self.repository_root.is_absolute() or not self.workspace.is_absolute():
            raise ValueError("仓库根目录和启动工作目录必须是绝对路径")
        if self.project_relative.anchor or any(".." in path.parts for path in (
            self.repository_root, self.workspace, self.project_relative,
        )):
            raise ValueError("布局路径不能包含上级跳转，相对路径不能带盘符或根目录")
        try:
            expected_relative = self.workspace.relative_to(self.repository_root)
        except ValueError as error:
            raise ValueError("启动工作目录必须位于当前 Git 工作树根目录内") from error
        if self.project_relative != expected_relative:
            raise ValueError("project_relative 与启动工作目录的实际相对关系不一致")


@dataclass(frozen=True)
class WorktreeTarget:
    """工作树目标快照；只校验字段关系，磁盘核验及操作授权由调用方负责。"""

    name: str  # 由创建预检查或已有工作树核验入口校验的名称，不是任意路径
    branch: str  # 预期分支名称，例如 wt/task-a；数据类不查询 Git
    root: Path  # 目标工作树绝对根目录
    workspace: Path  # 目标工作树中对应的项目执行目录，允许与 root 相同

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("WorktreeTarget name 必须是非空字符串")
        if self.branch != WORKTREE_BRANCH_PREFIX + self.name:
            raise ValueError("WorktreeTarget branch 必须由固定前缀和名称组成")
        if not isinstance(self.root, Path) or not isinstance(self.workspace, Path):
            raise TypeError("WorktreeTarget 的目录字段必须是 Path")
        if not self.root.is_absolute() or not self.workspace.is_absolute():
            raise ValueError("WorktreeTarget 的目录必须是绝对路径")
        if ".." in self.root.parts or ".." in self.workspace.parts:
            raise ValueError("WorktreeTarget 的目录不能包含上级跳转")
        if self.root.name != self.name:
            raise ValueError("WorktreeTarget 根目录名称必须与 name 一致")
        if not is_path_within_root(self.workspace, self.root):
            raise ValueError("队友执行目录必须位于新工作树内")


@dataclass(frozen=True)
class WorktreeStatus:
    """只读查询快照，由状态服务构建；不是删除许可，不重复校验内部已解析字段。"""

    target: WorktreeTarget  # 已核验的工作树位置与预期分支
    head: str  # 查询时目标工作树的提交 ID
    source_head: str  # 用于比较的源工作树提交 ID，不假定源分支名为 main
    staged_paths: tuple[str, ...]  # 暂存修改，路径相对于整个工作树根目录
    unstaged_paths: tuple[str, ...]  # 未暂存的已跟踪文件修改
    untracked_paths: tuple[str, ...]  # 未跟踪且未被忽略的文件
    ignored_paths: tuple[str, ...]  # 被忽略但仍实际存在的未跟踪文件
    unique_commit_count: int  # 目标 HEAD 可达而源 HEAD 不可达的提交数，不证明语义上未合并
    task_id: str | None  # 当前任务板中绑定此工作树的 Task；不存在时为 None
    task_status: TaskStatus | None  # 绑定任务的状态快照
    owner: str | None  # 当前认领者或 completed 的历史完成者，后者不单独阻止清理
    removal_blockers: tuple[str, ...]  # 当前发现的阻止移除原因；空元组也不保证之后可安全删除
