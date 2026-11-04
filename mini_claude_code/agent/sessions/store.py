"""会话快照存储：工作区绑定、原子 JSON 提交、锁内版本检查及离线列举。

这不是会话恢复器，不执行工具或启动后台任务。调用方后续需先完成消息脱敏与协议校验。
"""

from __future__ import annotations

import json
import stat
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from ..storage.atomic import atomic_replace_bytes
from ..storage.locking import exclusive_file_lock
from ..storage.paths import is_path_within_root
from .config import (
    SESSION_DIRECTORY_NAME, SESSION_LOCK_POLL_SECONDS, SESSION_LOCK_TIMEOUT_SECONDS,
    SESSION_MAX_BYTES, SESSION_SCHEMA_VERSION,
)
from .models import SessionSnapshot, parse_session_record, validate_session_id


class SessionConflictError(RuntimeError):
    """当前快照已被其他写入更新，禁止覆盖；不自动重新读取并重试。"""


def _check_plain_path(path: Path, *, directory: bool) -> None:
    """检查实际存储边界，拒绝符号链接、Windows 重解析点和不匹配的文件类型。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    valid_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_kind or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError(f"会话存储路径必须是普通{'目录' if directory else '文件'}：{path}")


def _decode(payload: bytes) -> SessionSnapshot:
    """拒绝重复 JSON 键，避免记录的含义依赖解析器覆盖顺序。"""
    def unique_fields(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ValueError(f"会话 JSON 含重复字段：{key}")
            record[key] = value
        return record

    def invalid_constant(value):
        raise ValueError(f"会话 JSON 含非有限数值：{value}")

    return parse_session_record(json.loads(
        payload.decode("utf-8"), object_pairs_hook=unique_fields,
        parse_constant=invalid_constant,
    ))


def _encode(snapshot: SessionSnapshot) -> tuple[bytes, SessionSnapshot]:
    """一次性序列化和校验；返回与磁盘字节一致、与输入对象无共享列表的快照。"""
    record = {"schema_version": SESSION_SCHEMA_VERSION, **asdict(snapshot)}
    payload = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > SESSION_MAX_BYTES:
        raise ValueError("会话快照超过文件大小上限；未写入，也未截断历史")
    return payload, _decode(payload)


class SessionStore:
    """仅管理指定启动工作目录的历史；构造、load 和 list_sessions 均不创建目录。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)  # 固定工作区身份，不按 Git 仓库根合并
        if not self.workspace.is_dir():
            raise ValueError("会话工作目录不存在或不是目录")
        self.directory = self.workspace / SESSION_DIRECTORY_NAME  # 每个工作区独立保存快照

    def _path(self, session_id: str, *, create_directory: bool = False) -> Path:
        validate_session_id(session_id)
        _check_plain_path(self.workspace, directory=True)
        _check_plain_path(self.directory, directory=True)
        if create_directory:
            self.directory.mkdir(exist_ok=True)
            _check_plain_path(self.directory, directory=True)
        path = self.directory / f"{session_id}.json"
        _check_plain_path(path, directory=False)
        return path

    def _check_workspace(self, snapshot: SessionSnapshot) -> None:
        recorded = Path(snapshot.workspace).resolve()
        if not (is_path_within_root(recorded, self.workspace)
                and is_path_within_root(self.workspace, recorded)):
            raise ValueError("会话属于其他工作目录；不会在当前目录自动恢复或迁移")

    def load(self, session_id: str) -> SessionSnapshot:
        """读取单份原子快照；只验证存储格式，不承诺消息可直接传给模型。"""
        path = self._path(session_id)
        with path.open("rb") as stream:
            payload = stream.read(SESSION_MAX_BYTES + 1)
        if len(payload) > SESSION_MAX_BYTES:
            raise ValueError(f"会话文件超过大小上限：{session_id}")
        snapshot = _decode(payload)
        if snapshot.session_id != session_id:
            raise ValueError("会话文件名与记录 ID 不一致")
        self._check_workspace(snapshot)
        return snapshot

    def create(self, title: str = "新会话") -> SessionSnapshot:
        """显式创建新的空会话；不覆盖已有 ID，不自动恢复最近历史。"""
        now = time.time()
        snapshot = SessionSnapshot(
            session_id="session_" + uuid4().hex, workspace=str(self.workspace),
            title=title, created_at=now, updated_at=now, revision=0,
            turn_in_progress=False, messages=[],
        )
        payload, saved = _encode(snapshot)
        path = self._path(saved.session_id, create_directory=True)
        with self._lock(saved.session_id):
            path = self._path(saved.session_id)
            atomic_replace_bytes(path, payload, allowed_root=self.directory, replace_existing=False)
        return saved

    def _lock(self, session_id: str):
        lock_path = self._path(session_id).with_suffix(".lock")
        _check_plain_path(lock_path, directory=False)
        return exclusive_file_lock(
            lock_path, allowed_root=self.directory,
            timeout_seconds=SESSION_LOCK_TIMEOUT_SECONDS,
            poll_interval_seconds=SESSION_LOCK_POLL_SECONDS,
        )

    def ownership(self, session_id: str):
        """整个运行期的独占锁；与单次保存锁分离，不能用锁文件存在判断占用。"""
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"找不到会话：{session_id}")
        lock_path = path.with_suffix(".owner.lock")
        _check_plain_path(lock_path, directory=False)
        return exclusive_file_lock(
            lock_path, allowed_root=self.directory,
            timeout_seconds=SESSION_LOCK_TIMEOUT_SECONDS,
            poll_interval_seconds=SESSION_LOCK_POLL_SECONDS,
        )

    def save(self, snapshot: SessionSnapshot) -> SessionSnapshot:
        """在同一事务内检查 revision 并提交；成功后必须改用返回的新 revision。

        版本检查只防止过期覆盖，不等于 CLI 整个生命周期的独占所有权；后续入口另做会话占用锁。
        """
        # 外部候选先校验并复制，后续读取不会观察到调用方修改 messages 的别名。
        _, candidate = _encode(snapshot)
        self._check_workspace(candidate)
        self._path(candidate.session_id)
        with self._lock(candidate.session_id):
            current = self.load(candidate.session_id)
            if candidate.revision != current.revision or candidate.created_at != current.created_at:
                raise SessionConflictError("会话快照已过期或身份被修改；拒绝覆盖，未自动重试")
            saved = replace(candidate, revision=current.revision + 1,
                            updated_at=max(time.time(), current.updated_at))
            payload, saved = _encode(saved)
            path = self._path(saved.session_id)
            atomic_replace_bytes(path, payload, allowed_root=self.directory)
        return saved

    def list_sessions(self) -> tuple[SessionSnapshot, ...]:
        """按最近更新时间列出本工作区历史；损坏记录显式报错，不静默丢弃。"""
        _check_plain_path(self.workspace, directory=True)
        _check_plain_path(self.directory, directory=True)
        if not self.directory.exists():
            return ()
        sessions = [self.load(path.stem) for path in self.directory.glob("*.json")]
        return tuple(sorted(sessions, key=lambda session: (session.updated_at, session.session_id), reverse=True))
