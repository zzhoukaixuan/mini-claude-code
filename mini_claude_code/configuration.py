"""模型配置解析：独立于运行时，按连接整体选择来源，不把项目地址与用户密钥拼接。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "https://openrouter.ai/api"  # Anthropic SDK 自动追加 /v1/messages
_active_connection: ModelConnection | None = None  # 单进程使用固定连接；修改后重启 CLI


class ConfigurationError(RuntimeError):
    """可直接展示的配置错误；错误消息不得包含配置值或密钥。"""


@dataclass(frozen=True)
class ModelConnection:
    base_url: str  # Anthropic Messages 协议的基础地址
    api_key: str = field(repr=False)  # 密钥不参与对象调试输出
    model: str  # 默认模型 ID
    fallback_model: str | None = None  # 可选的过载备用模型
    source: str = "用户配置"  # 诊断来源，不含配置正文

    def __post_init__(self) -> None:
        for name, value in (("OPENROUTER_BASE_URL", self.base_url),
                            ("OPENROUTER_API_KEY", self.api_key), ("MODEL_ID", self.model)):
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ConfigurationError(f"{name} 必须是非空单行文本；请运行 miniclaude configure。")
            if value != value.strip():
                raise ConfigurationError(f"{name} 不能带首尾空白。")
        try:
            url = urlsplit(self.base_url)
            _ = url.port  # 同时校验非法端口，不在错误中回显地址
        except ValueError:
            raise ConfigurationError("API 地址格式无效。") from None
        if (url.scheme not in ("https", "http") or not url.hostname or url.username
                or url.password or url.query or url.fragment or "\\" in self.base_url
                or any(c.isspace() for c in self.base_url)):
            raise ConfigurationError("API 地址必须是 HTTP(S) URL，不能含认证信息、查询参数或片段。")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ConfigurationError("非本机 API 地址必须使用 HTTPS，防止明文传输密钥。")
        if url.hostname == "openrouter.ai" and url.path.rstrip("/") != "/api":
            raise ConfigurationError("Anthropic SDK 的 OpenRouter 地址应为 https://openrouter.ai/api。")
        if self.fallback_model is not None and (
            not isinstance(self.fallback_model, str) or not self.fallback_model.strip()
            or any(c.isspace() for c in self.fallback_model)
        ):
            raise ConfigurationError("FALLBACK_MODEL_ID 必须是非空且不含空白的模型 ID。")


def _from_values(values, source: str) -> ModelConnection:
    """地址、密钥、模型必须来自同一份完整配置，禁用 dotenv 变量插值。"""
    return ModelConnection(
        base_url=values.get("OPENROUTER_BASE_URL"), api_key=values.get("OPENROUTER_API_KEY"),
        model=values.get("MODEL_ID"), fallback_model=values.get("FALLBACK_MODEL_ID") or None,
        source=source,
    )


def _from_env_file(path: Path) -> ModelConnection:
    from dotenv import dotenv_values
    if not path.is_file():
        raise ConfigurationError("指定的 .env 文件不存在或不是普通文件。")
    try:
        # 不允许 ${OPENROUTER_API_KEY} 从进程环境借入密钥。
        values = dotenv_values(path, interpolate=False, encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigurationError("无法读取指定 .env 文件。") from None
    if any("${" in str(value) for value in values.values()):
        raise ConfigurationError("连接 .env 不支持变量插值，请直接填写完整配置。")
    return _from_values(values, "显式/兼容 .env 文件")


def resolve_connection(*, workspace: Path | None = None, config_dir: Path | None = None,
                       env_file: Path | None = None, environ=None) -> ModelConnection:
    """只读解析；不创建配置、不修改环境变量、不构造模型客户端。"""
    from .config_store import load_user_connection
    workspace = Path.cwd() if workspace is None else workspace
    env = os.environ if environ is None else environ
    if env_file is not None:
        return _from_env_file(workspace / env_file)
    # 环境提供地址或密钥时，必须成组提供，不能借用用户配置补齐。
    if "OPENROUTER_BASE_URL" in env or "OPENROUTER_API_KEY" in env:
        return _from_values(env, "进程环境变量")
    saved = load_user_connection(config_dir)
    if saved is not None:
        return replace(saved, model=env.get("MODEL_ID", saved.model),
                       fallback_model=env.get("FALLBACK_MODEL_ID", saved.fallback_model))
    legacy = workspace / ".env"
    if legacy.is_file():
        return _from_env_file(legacy)
    raise ConfigurationError(
        "缺少模型连接配置（OPENROUTER_API_KEY、OPENROUTER_BASE_URL、MODEL_ID）；"
        "请运行 miniclaude configure，或提供完整环境变量/项目 .env。"
    )


def get_connection(*, env_file: Path | None = None) -> ModelConnection:
    """首次启动解析并固定连接；同一进程不热切换已导入的运行时客户端。"""
    global _active_connection
    if _active_connection is None:
        _active_connection = resolve_connection(env_file=env_file)
    elif env_file is not None:
        candidate = resolve_connection(env_file=env_file)
        if replace(candidate, source=_active_connection.source) != _active_connection:
            raise ConfigurationError("当前进程已初始化其他模型配置，请重启 CLI 后切换。")
    return _active_connection


def active_secrets() -> tuple[str, ...]:
    """给会话和归档脱敏使用；不触发配置加载，也不把密钥写入环境变量。"""
    return (_active_connection.api_key,) if _active_connection is not None else ()
