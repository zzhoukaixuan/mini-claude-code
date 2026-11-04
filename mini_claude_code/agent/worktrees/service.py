"""Worktree 服务：创建并绑定任务，或只读核验已有工作树的目录、分支及仓库归属。

提供创建与 Task 绑定的上层编排，不开放模型工具、不自动清理失败残留；调用方必须先获得写入授权。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from typing import NoReturn

from ..config import (
    WORKTREE_CREATE_TIMEOUT_SECONDS, WORKTREE_COMMIT_ID_PATTERN,
    WORKTREE_DIRECTORY_NAME, WORKTREE_BRANCH_PREFIX,
)
from ..storage.paths import is_path_within_root
from ..tasks.service import get_task, list_tasks, bind_task_to_worktree
from ..tasks.models import Task
from .events import append_worktree_event
from .git import run_git
from .models import RepositoryLayout, WorktreeTarget, WorktreeStatus
from .names import validate_worktree_name
from .paths import (
    _require_plain_directory_if_present,
    discover_repository,
    prepare_worktree_target,
)


class WorktreeCreationError(RuntimeError):
    """创建或复核失败；携带阶段和目标供人工核验，不代表回滚成功。"""

    def __init__(self, stage: str, target: WorktreeTarget, error: Exception) -> None:
        self.stage = stage  # 失败发生于 Git 创建还是创建后的复核
        self.target = target  # 可能残留的目录、分支及项目路径
        super().__init__(
            f"Worktree {stage}失败：{type(error).__name__}：{error}。"
            f"可能残留目录 {str(target.root)!r}、分支 {target.branch!r} 或 Git 登记；"
            "未自动删除或重试，请先核验实际状态。"
        )


def _same_path(left: Path, right: Path) -> bool:
    """比较规范路径，兼容 Windows 普通路径与扩展路径前缀。"""
    return is_path_within_root(left, right) and is_path_within_root(right, left)


def _verify_worktree(
    layout: RepositoryLayout, target: WorktreeTarget, *, expected_commit: str | None = None,
) -> None:
    """共用核验；仅创建后要求固定提交，已有工作树允许合法的新提交和未提交修改。"""
    _require_plain_directory_if_present(layout.repository_root)
    if not _same_path(layout.repository_root.resolve(strict=True), layout.repository_root):
        raise ValueError("源仓库根目录发生重定向")
    source = discover_repository(layout.workspace)
    if (not _same_path(source.repository_root, layout.repository_root)
            or not _same_path(source.workspace, layout.workspace)):
        raise ValueError("源仓库布局发生变化，请重新识别")
    # 先查路径再在其中执行 Git；不能仅凭路径存在就进入未知目录。
    for directory in (target.root.parent, target.root):
        _require_plain_directory_if_present(directory)
        resolved = directory.resolve(strict=True)
        if not resolved.is_dir() or not _same_path(resolved, directory):
            raise ValueError(f"Worktree 目录发生变化：{directory}")

    output = run_git(["worktree", "list", "--porcelain", "-z"], cwd=layout.repository_root)
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for field in output.split("\0"):
        if not field:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, value = field.partition(" ")
        if key in record:
            raise ValueError("Git Worktree 登记输出包含重复字段")
        record[key] = value
    if record:
        records.append(record)
    matches = [entry for entry in records if "worktree" in entry and _same_path(
        Path(entry["worktree"]), target.root,
    )]
    if len(matches) != 1:
        raise ValueError("源仓库中没有唯一匹配的 Worktree 登记")
    entry = matches[0]
    if entry.get("branch") != f"refs/heads/{target.branch}":
        raise ValueError("Worktree 登记的分支与目标不一致")
    if WORKTREE_COMMIT_ID_PATTERN.fullmatch(entry.get("HEAD", "")) is None:
        raise ValueError("Worktree 登记没有有效的提交 ID")
    if expected_commit is not None and entry.get("HEAD") != expected_commit:
        raise ValueError("Worktree 登记的提交与本次创建基准不一致")
    if "prunable" in entry or "bare" in entry:
        raise ValueError("Worktree 登记处于不可用状态")

    directory = target.root
    for part in layout.project_relative.parts:
        directory = directory / part
        _require_plain_directory_if_present(directory)
    workspace = target.workspace.resolve(strict=True)
    if not workspace.is_dir() or not is_path_within_root(workspace, target.root):
        raise ValueError("检出后的项目目录不存在、不是目录或越出 Worktree")
    actual = discover_repository(workspace)
    if not _same_path(actual.repository_root, target.root):
        raise ValueError("队友执行目录不属于目标工作树（可能位于嵌套仓库）")

    # 目录和登记相符还不够：目标 .git 可能被换成另一仓库的元数据。
    common_dirs: list[Path] = []
    for root in (layout.repository_root, target.root):
        text = run_git(["rev-parse", "--git-common-dir"], cwd=root).rstrip("\r\n")
        if not text or "\n" in text or "\r" in text:
            raise ValueError("Git 未返回唯一的公共元数据目录")
        common_dir = (root / text).resolve(strict=True)
        if not common_dir.is_dir():
            raise ValueError("Git 公共元数据目录无效")
        common_dirs.append(common_dir)
    if not _same_path(common_dirs[0], common_dirs[1]):
        raise ValueError("目标工作树与源仓库不共享 Git 元数据")
    branch = run_git(["symbolic-ref", "--quiet", "HEAD"], cwd=target.root).strip()
    commit = run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=target.root).strip()
    if branch != f"refs/heads/{target.branch}" or commit != entry["HEAD"]:
        raise ValueError("目标工作树实际分支或 HEAD 与登记不一致")


def _verify_created_worktree(
    layout: RepositoryLayout, target: WorktreeTarget, base_commit: str,
) -> None:
    """创建后的严格核验：除共用检查外，还要求 HEAD 等于固定基准提交。"""
    _verify_worktree(layout, target, expected_commit=base_commit)


def inspect_worktree(layout: RepositoryLayout, name: str) -> WorktreeTarget:
    """只读核验已有工作树；不创建、修复、重置或清理，也不授予写入权限。

    不要求工作区干净或 HEAD 等于创建时提交。返回的是当前时刻的快照，
    不能防御核验后同权限进程替换目录，文件工具仍须执行自身边界检查。
    """
    if not isinstance(layout, RepositoryLayout):
        raise TypeError("layout 必须是 RepositoryLayout")
    name = validate_worktree_name(name)
    # 保留词法路径供核验逐段拒绝链接，不能先 resolve 隐藏重定向。
    root = layout.repository_root / WORKTREE_DIRECTORY_NAME / name
    target = WorktreeTarget(
        name, WORKTREE_BRANCH_PREFIX + name, root, root / layout.project_relative,
    )
    _verify_worktree(layout, target)
    return target


def query_worktree_status(
    layout: RepositoryLayout, name: str, *, tasks_dir: Path,
) -> WorktreeStatus:
    """获取受锁保护的任务快照，再查询 Git 状态；不是删除授权。"""
    return _query_worktree_status(layout, name, tasks=list_tasks(tasks_dir))


def _query_worktree_status(
    layout: RepositoryLayout, name: str, *, tasks: tuple[Task, ...],
) -> WorktreeStatus:
    """核验已有工作树后，只读查询文件、提交及当前任务板的绑定状态。

    查询整个工作树，而非仅项目子目录，避免漏掉同仓库其他目录的成果。
    Git/任务读取失败直接抛错，不把未知状态伪装成干净；各查询不是原子快照，
    真正删除时须协调认领并重新核验。本函数不写事件、不刷新索引、不修复任务。
    """
    target = inspect_worktree(layout, name)  # 名称、布局、目录和 Git 归属统一在此入口校验

    def read_head(root: Path) -> str:
        commit = run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=root).strip()
        if WORKTREE_COMMIT_ID_PATTERN.fullmatch(commit) is None:
            raise ValueError("Git 未返回有效的完整提交 ID")
        return commit

    def read_paths(args: list[str]) -> tuple[str, ...]:
        # NUL 分隔保留路径中的空格，不按行解析或将展示截断当成完整状态。
        output = run_git(["--no-optional-locks", *args], cwd=target.root)
        if not output:
            return ()
        if not output.endswith("\0"):
            raise ValueError("Git 文件列表缺少 NUL 终止符，不能确认完整状态")
        return tuple(dict.fromkeys(output[:-1].split("\0")))

    head = read_head(target.root)
    source_head = read_head(layout.repository_root)
    diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                 "--ignore-submodules=none", "--name-only", "-z"]
    staged = read_paths([*diff_args, "--cached", head, "--"])
    unstaged = read_paths([*diff_args, "--"])
    untracked = read_paths(["ls-files", "--others", "--exclude-standard", "-z"])
    ignored = read_paths(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"])
    count_text = run_git(
        ["rev-list", "--count", f"{source_head}..{head}", "--"], cwd=target.root,
    ).strip()
    if not count_text.isascii() or not count_text.isdecimal():
        raise ValueError("Git 未返回有效的独有提交数量")
    unique_count = int(count_text)

    # 使用服务层已校验的快照；持锁移除时不再取同一把非重入文件锁。
    task = next((item for item in tasks
                 if item.worktree is not None and item.worktree.casefold() == target.name.casefold()), None)
    blockers: list[str] = []
    for paths, label in ((staged, "暂存修改"), (unstaged, "未暂存修改"),
                         (untracked, "未跟踪文件"), (ignored, "被忽略的文件")):
        if paths:
            blockers.append(f"存在{label}（{len(paths)} 项）")
    if unique_count:
        blockers.append(f"存在相对源工作树 HEAD 的独有提交（{unique_count} 个），需人工核验整合情况")
    if task is None:
        blockers.append("当前任务板未找到绑定 Task，不能确认工作树生命周期")
    else:
        if task.status == "in_progress":
            blockers.append("绑定 Task 仍为 in_progress，不能移除其工作目录")
        if task.status == "pending" and task.owner is not None:
            blockers.append("绑定 Task 仍有 owner；不能凭本进程未发现成员就认定可清理")
        if task.status == "pending":
            blockers.append("绑定 Task 尚未完成，当前策略仅允许移除 completed 任务的工作树")

    return WorktreeStatus(
        target=target, head=head, source_head=source_head,
        staged_paths=staged, unstaged_paths=unstaged,
        untracked_paths=untracked, ignored_paths=ignored,
        unique_commit_count=unique_count,
        task_id=task.id if task else None, task_status=task.status if task else None,
        owner=task.owner if task else None, removal_blockers=tuple(blockers),
    )


def create_worktree(layout: RepositoryLayout, name: str) -> WorktreeTarget:
    """从当前 HEAD 的固定提交创建新分支和工作树，复核成功才返回。

    不自动提交或复制未跟踪文件；不调用 git init、不改变 Task。
    此函数有磁盘副作用，仅供已获授权的调用方使用。路径预检查不是锁，
    不能防御具有同等本地权限的恶意进程在检查后替换目录。
    """
    target = prepare_worktree_target(layout, name)
    actual = discover_repository(layout.workspace)
    if not _same_path(actual.repository_root, layout.repository_root):
        raise ValueError("仓库归属已改变，请重新识别布局")
    base_commit = run_git(
        ["rev-parse", "--verify", "HEAD^{commit}"], cwd=layout.repository_root,
    ).strip()
    if WORKTREE_COMMIT_ID_PATTERN.fullmatch(base_commit) is None:
        raise ValueError("Git 未返回有效的完整基准提交 ID")
    ref = f"refs/heads/{target.branch}"
    # for-each-ref 成功但无匹配时返回空输出；查询失败由 run_git 抛出，不能当作不存在。
    refs = run_git(["for-each-ref", "--format=%(refname)", ref], cwd=layout.repository_root)
    if ref in refs.splitlines():
        raise FileExistsError(f"目标分支已存在，不能覆盖：{target.branch}")

    # Git 查询耗时期间目标可能被占用；写操作前再次检查，不提前 mkdir。
    if prepare_worktree_target(layout, name) != target:
        raise ValueError("Worktree 创建目标发生变化，请重新准备")
    stage = "Git 创建"
    try:
        # -b 而非 -B：即使分支在预检查后被创建，也由 Git 拒绝覆盖。
        run_git(
            ["worktree", "add", "-b", target.branch, str(target.root), base_commit],
            cwd=layout.repository_root, timeout=WORKTREE_CREATE_TIMEOUT_SECONDS,
        )
        stage = "创建后复核"
        _verify_created_worktree(layout, target, base_commit)
    except Exception as error:
        raise WorktreeCreationError(stage, target, error) from error
    return target


class TaskWorktreeOperationError(RuntimeError):
    """上层操作未完整成功；保留已知阶段与审计错误，不伪装成回滚。"""

    def __init__(
        self, operation_id: str, stage: str, target: WorktreeTarget,
        created: bool | None, bound: bool | None, error: Exception,
        audit_error: Exception | None = None,
    ) -> None:
        self.operation_id = operation_id  # 同一流程事件的关联 ID
        self.stage = stage  # 失败的业务或日志阶段
        self.target = target  # 需核验的目录及分支
        self.created = created  # True 已核验创建；False 尚未确认创建；None 可能部分创建
        self.bound = bound  # True 绑定调用已成功返回；False 未尝试；None 写入结果未确认
        self.audit_error = audit_error  # 故障记录也失败时保留第二条诊断
        created_text = "已核验创建" if created is True else "创建结果不确定" if created is None else "未确认创建成功"
        bound_text = "绑定已完成" if bound is True else "绑定结果不确定" if bound is None else "尚未尝试绑定"
        extra = f"；失败事件也未确认保存：{type(audit_error).__name__}：{audit_error}" if audit_error else ""
        super().__init__(
            f"操作 {operation_id} 在 {stage} 阶段失败：{type(error).__name__}：{error}；"
            f"{created_text}，{bound_text}。目录 {str(target.root)!r}，分支 {target.branch!r}{extra}。"
            "未回滚、未自动重试，请核验 Git、Task 和事件文件的实际状态。"
        )


def create_task_worktree(
    layout: RepositoryLayout, task_id: str, name: str, *, tasks_dir: Path,
) -> WorktreeTarget:
    """预检查 Task → 创建并核验 → 锁内绑定 → 审计；不持 Task 锁等待 Git。

    只提供诊断性事件，不构成跨 Git/Task/日志事务；崩溃后不会自动续跑。
    此函数不是幂等重试入口：已有目标拒绝复用，残留需人工核验。
    """
    target = prepare_worktree_target(layout, name)
    task = get_task(task_id, tasks_dir)
    if task.status != "pending" or task.worktree is not None:
        raise ValueError("只能为未绑定的 pending Task 创建 Worktree")
    if any(item.worktree is not None and item.worktree.casefold() == name.casefold()
           for item in list_tasks(tasks_dir)):
        raise ValueError("Worktree 名称已被当前任务板中的 Task 绑定")
    operation_id = "wt_" + uuid4().hex
    created: bool | None = False
    bound: bool | None = False
    stage = "create"

    def record(outcome: str, detail: str = "") -> None:
        append_worktree_event(layout, operation_id=operation_id, task_id=task_id,
                              name=name, stage=stage, outcome=outcome, detail=detail)

    def require_event(outcome: str) -> None:
        try:
            record(outcome)
        except Exception as error:
            # 不再递归尝试记录日志失败。开始记录失败则不进入下一步；成功记录失败不撤销业务。
            raise TaskWorktreeOperationError(
                operation_id, f"{stage} 审计 {outcome}", target, created, bound, error,
            ) from error

    def fail(error: Exception) -> NoReturn:
        audit_error = None
        try:
            record("failed", f"{type(error).__name__}：{error}；结果需核验，未回滚")
        except Exception as logging_error:
            audit_error = logging_error
        raise TaskWorktreeOperationError(
            operation_id, stage, target, created, bound, error, audit_error,
        ) from error

    require_event("started")  # 首条记录失败时，不尝试 Git 创建
    try:
        target = create_worktree(layout, name)  # 内含创建前和创建后的核验
    except Exception as error:
        created = None if isinstance(error, WorktreeCreationError) else False
        fail(error)
    created = True
    require_event("succeeded")
    stage = "bind"
    require_event("started")
    try:
        # 预检查后 Task 可能已被认领；服务必须在 Task 文件锁内重新检查。
        bind_task_to_worktree(task_id, target.name, tasks_dir)
    except Exception as error:
        bound = None  # 存储调用报错也可能已落盘，不直接声称未绑定
        fail(error)
    bound = True
    require_event("succeeded")
    return target
