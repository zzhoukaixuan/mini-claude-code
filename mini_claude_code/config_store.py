"""用户级连接存储：单文件原子提交、受限权限及无敏感正文的错误提示。"""

from __future__ import annotations

import csv
import ctypes
import json
import os
from pathlib import Path
from contextlib import contextmanager
import stat
import subprocess
import tempfile

from .configuration import ConfigurationError, ModelConnection


MAX_CONFIG_BYTES = 64 * 1024  # 配置文件读取上限


def user_config_dir() -> Path:
    return Path.home() / ".mini-claude-code"


@contextmanager
def mcp_config_transaction(config_dir: Path | None = None):
    """短事务：锁内重读、修改、保存；不在锁内等待交互或网络。"""
    from .agent.storage.locking import exclusive_file_lock
    root = user_config_dir() if config_dir is None else config_dir
    _plain_path(root, directory=True)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _restrict_access(root, directory=True)
    lock_path = root / ".mcp.lock"
    _plain_path(lock_path, directory=False)
    with exclusive_file_lock(lock_path, allowed_root=root, timeout_seconds=5,
                             poll_interval_seconds=.05):
        yield


def _plain_path(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))):
        raise ConfigurationError("配置目录/文件必须是普通路径，不允许符号链接或重解析点。")


def _restrict_access(path: Path, *, directory: bool) -> None:
    """POSIX 使用 700/600；Windows 仅授权当前用户 SID，失败时不写入密钥。"""
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, check=True, timeout=10)
    sid = next(csv.reader(identity.stdout.splitlines()))[-1].strip()
    if not sid.startswith("S-1-") or any(c not in "S-0123456789" for c in sid):
        raise ConfigurationError("无法识别当前 Windows 用户 SID，未保存配置。")
    # 完整替换受保护 DACL，而非只追加授权（追加会留下已有的其他用户 ACE）。
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    inherit = "OICI" if directory else ""
    if not convert(f"D:P(A;{inherit};FA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(str(path), 0x80000004, descriptor):  # 受保护 DACL，关闭父目录继承
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def load_user_connection(config_dir: Path | None = None) -> ModelConnection | None:
    root = user_config_dir() if config_dir is None else config_dir
    path = root / "config.json"
    try:
        _plain_path(root, directory=True)
        _plain_path(path, directory=False)
        try:
            with path.open("rb") as stream:
                payload = stream.read(MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(payload) > MAX_CONFIG_BYTES:
            raise ConfigurationError("用户配置文件超过大小上限。")
        record = json.loads(payload.decode("utf-8"))
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int or record.get("schema_version") != 1
                or set(record) != {"schema_version", "connection"}):
            raise ConfigurationError("用户配置版本或字段无效。")
        connection = record["connection"]
        if not isinstance(connection, dict) or set(connection) != {"base_url", "api_key", "model", "fallback_model"}:
            raise ConfigurationError("用户连接配置字段无效。")
        return ModelConnection(**connection, source="用户级 config.json")
    except (OSError, ValueError, TypeError):
        raise ConfigurationError("无法读取用户配置 JSON；请检查文件格式和权限，不要粘贴密钥排错。") from None


def save_user_connection(connection: ModelConnection, config_dir: Path | None = None) -> Path:
    """确认后一次提交整个连接；中断时保留旧文件，不会产生地址/密钥半更新。"""
    return save_private_config("config.json", {"schema_version": 1, "connection": {
        "base_url": connection.base_url, "api_key": connection.api_key,
        "model": connection.model, "fallback_model": connection.fallback_model,
    }}, config_dir)


def save_private_config(filename: str, record: dict, config_dir: Path | None = None) -> Path:
    """复用受限权限与原子替换；调用方负责配置结构和读改写事务的并发协调。"""
    if filename not in {"config.json", "mcp.json"}:
        raise ConfigurationError("不支持的用户配置文件。")
    root = user_config_dir() if config_dir is None else config_dir
    target = root / filename
    temporary: Path | None = None
    try:
        _plain_path(root, directory=True)
        _plain_path(target, directory=False)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _restrict_access(root, directory=True)
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
        if len(payload) > MAX_CONFIG_BYTES:
            raise ConfigurationError("配置内容超过大小上限。")
        descriptor, name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=root)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            _restrict_access(temporary, directory=False)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _plain_path(target, directory=False)
        os.replace(temporary, target)
        return target
    except (OSError, subprocess.SubprocessError):
        raise ConfigurationError("保存配置失败；请检查用户目录权限及文件占用。旧配置未被部分覆盖。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_private_config(filename: str, config_dir: Path | None = None) -> dict | None:
    """有界读取私有 JSON；不存在返回 None，损坏时不覆盖旧配置。"""
    if filename not in {"config.json", "mcp.json"}:
        raise ConfigurationError("不支持的用户配置文件。")
    root = user_config_dir() if config_dir is None else config_dir
    target = root / filename
    try:
        _plain_path(root, directory=True)
        _plain_path(target, directory=False)
        try:
            with target.open("rb") as stream:
                payload = stream.read(MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(payload) > MAX_CONFIG_BYTES:
            raise ConfigurationError("用户配置文件超过大小上限。")
        record = json.loads(payload.decode("utf-8"))
        if not isinstance(record, dict):
            raise ConfigurationError("用户配置必须是 JSON 对象。")
        return record
    except (OSError, ValueError, TypeError):
        raise ConfigurationError("无法读取用户配置 JSON；请检查格式和权限，不要粘贴密钥排错。") from None
