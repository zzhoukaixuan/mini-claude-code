"""基础工具模块：实现受工作区边界约束的文件读取、写入、编辑、Glob 与 Shell 调用。"""

from __future__ import annotations

import glob as g
import hashlib
import math
import os
import subprocess
from pathlib import Path

from ..config import (
    DEFAULT_SHELL_TIMEOUT_SECONDS,
    FILE_VERSION_PATTERN,
    MAX_JOB_TIMEOUT_SECONDS,
    MIN_JOB_TIMEOUT_SECONDS,
    WORKDIR,
)
from ..storage.atomic import atomic_replace_bytes, atomic_replace_text
from ..storage.paths import is_path_within_root


def _resolve_workspace(workspace: Path) -> Path:
    """解析 Harness 提供的现存工作目录，不创建目录或改变进程 cwd。"""
    if not isinstance(workspace, Path):
        raise TypeError("workspace 必须是 Path")
    if not workspace.is_absolute():
        raise ValueError("workspace 必须是绝对路径")
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"workspace 不是目录：{root}")
    return root


def _path_in_workspace(path: str, workspace: Path) -> Path:
    """在已解析的工作目录内定位目标；供公共边界与工具内部共用。"""
    resolved_path = (workspace / path).resolve()
    if not is_path_within_root(resolved_path, workspace):
        raise ValueError(f"路径 {resolved_path} 不在工作目录 {workspace} 内")
    return resolved_path


def safe_path(path: str, *, workspace: Path = WORKDIR) -> Path:
    """确保目标位于指定工作目录；workspace 由 Harness 绑定，不加入模型 Schema。"""
    return _path_in_workspace(path, _resolve_workspace(workspace))


def build_file_version(raw_content: bytes) -> str:
    """根据文件的原始字节生成带算法前缀的确定性版本令牌。"""
    if not isinstance(raw_content, bytes):
        raise TypeError("文件版本只能根据 bytes 类型的原始内容生成")
    return f"sha256:{hashlib.sha256(raw_content).hexdigest()}"


def read_text_file_snapshot(file_path: Path) -> tuple[str, str]:
    """从同一份原始字节快照中解码 UTF-8 文本并计算文件版本。"""
    if not isinstance(file_path, Path):
        raise TypeError("文件快照路径必须是 pathlib.Path 类型")
    raw_content = file_path.read_bytes()
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"文件 {file_path} 不是有效的 UTF-8 文本") from error
    return content, build_file_version(raw_content)


def run_read(
    path: str, limit: int | None = None, start_line: int = 1,
    *, workspace: Path = WORKDIR,
) -> str:
    """读取带完整文件版本的文本窗口，供后续编辑进行新鲜度校验。"""
    try:
        if type(start_line) is not int or start_line < 1:
            raise ValueError("起始行号必须是大于或等于 1 的整数")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("最大读取行数必须是正整数或 None")
        workspace = _resolve_workspace(workspace)
        file_path = _path_in_workspace(path, workspace)
        full_content, file_version = read_text_file_snapshot(file_path)
        all_lines = full_content.splitlines()
        total_lines = len(all_lines)

        if total_lines == 0:
            range_description = "文件为空"
            displayed_content = "(文件为空)"
        elif start_line > total_lines:
            range_description = f"未读取；起始行 {start_line} 超出文件范围，文件共 {total_lines} 行"
            displayed_content = f"(起始行 {start_line} 超出文件范围；文件共 {total_lines} 行)"
        else:
            start_index = start_line - 1
            end_index = total_lines if limit is None else min(total_lines, start_index + limit)
            displayed_content = "\n".join(all_lines[start_index:end_index])
            range_description = f"第 {start_line} 至 {end_index} 行，共 {total_lines} 行"
        display_path = file_path.relative_to(workspace).as_posix()
        return (
            f"<file-snapshot>\n"
            f"文件路径：{display_path}\n"
            f"文件版本：{file_version}\n"
            f"读取范围：{range_description}\n"
            f"文件内容：\n{displayed_content}\n"
            f"</file-snapshot>"
        )
    except Exception as e:
        return f"读取失败：{e}"


def run_shell(
    command: str,
    timeout: float = DEFAULT_SHELL_TIMEOUT_SECONDS,
) -> str:
    """在工作目录中执行 Windows Shell 命令。"""
    cwd = WORKDIR
    try:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Shell 命令必须是非空字符串")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not MIN_JOB_TIMEOUT_SECONDS <= timeout <= MAX_JOB_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "Shell timeout 必须位于 "
                f"[{MIN_JOB_TIMEOUT_SECONDS}, {MAX_JOB_TIMEOUT_SECONDS}] 秒之间"
            )
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        output = (result.stdout + result.stderr).strip()
        if not output:
            return "(无输出)"
        return output

    except subprocess.TimeoutExpired:
        return f"错误：命令执行超过 {timeout:g} 秒"

def run_write(path: str, content: str, *, workspace: Path = WORKDIR) -> str:
    """通过同目录临时文件与原子替换完整写入 UTF-8 文本。"""
    try:
        if not isinstance(content, str):
            raise TypeError("写入内容必须是字符串")
        workspace = _resolve_workspace(workspace)
        file_path = _path_in_workspace(path, workspace)
        atomic_replace_text(file_path, content, allowed_root=workspace)
        return f"写入成功：已向 {path} 写入 {len(content)} 个字符"
    except Exception as e:
        return f"写入文件失败：{e}"

def run_edit(
    path: str, old_text: str, new_text: str, expected_version: str,
    *, workspace: Path = WORKDIR,
) -> str:
    """仅在文件版本仍与最近读取结果一致时执行一次原子文本替换。"""
    try:
        if not isinstance(expected_version, str) or FILE_VERSION_PATTERN.fullmatch(expected_version) is None:
            return "编辑失败：expected_version 必须是 read_file 返回的完整 sha256 文件版本，请先重新读取文件"
        if not isinstance(old_text, str) or not old_text:
            return "编辑失败：old_text 必须是非空字符串"
        if not isinstance(new_text, str):
            return "编辑失败：new_text 必须是字符串"
        workspace = _resolve_workspace(workspace)
        file_path = _path_in_workspace(path, workspace)
        content, current_version = read_text_file_snapshot(file_path)
        if current_version != expected_version:
            return (
                f"编辑拒绝：文件 {path} 自最近一次 read_file 后已经变化；"
                f"期望版本 {expected_version[:19]}...，当前版本 {current_version[:19]}...；"
                "文件未修改，请重新调用 read_file 获取最新内容和完整版本"
            )
        if "\r\n" in content:
            newline_sequence = "\r\n"
        elif "\n" in content:
            newline_sequence = "\n"
        elif "\r" in content:
            newline_sequence = "\r"
        else:
            newline_sequence = os.linesep
        normalized_old_text = old_text.replace("\r\n", "\n").replace("\r", "\n")
        normalized_new_text = new_text.replace("\r\n", "\n").replace("\r", "\n")
        old_text_for_file = normalized_old_text.replace("\n", newline_sequence)
        new_text_for_file = normalized_new_text.replace("\n", newline_sequence)
        if old_text_for_file not in content and old_text in content:
            old_text_for_file = old_text
        if old_text_for_file not in content:
            return f"编辑失败：未在 {path} 中找到待替换文本，待替换文本长度为 {len(old_text)} 个字符"
        updated_content = content.replace(old_text_for_file, new_text_for_file, 1)
        updated_raw_content = updated_content.encode("utf-8")
        atomic_replace_bytes(file_path, updated_raw_content, allowed_root=workspace)
        updated_version = build_file_version(updated_raw_content)
        return (
            f"编辑成功：已在 {path} 完成 1 处替换，"
            f"新文本长度为 {len(new_text)} 个字符；"
            f"新文件版本：{updated_version}"
        )
    except Exception as e:
        return f"编辑失败：{e}"


def run_glob(pattern: str, *, workspace: Path = WORKDIR) -> str:
    """查找指定工作目录内的文件和目录，保留搜索前后两层路径检查。"""
    try:
        results = []
        workspace = _resolve_workspace(workspace)
        if not is_path_within_root((workspace / pattern).resolve(), workspace):
            return f"错误：路径 {pattern} 不在工作目录 {workspace} 内"
        for match in g.glob(pattern, root_dir=workspace, recursive=True):
            if is_path_within_root((workspace / match).resolve(), workspace):
                results.append(match)
        return "\n".join(results) if results else "无匹配文件"
    except Exception as e:
        return f"查找失败：{e}"
