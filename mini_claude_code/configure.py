"""首次配置命令：终端安全输入、确认后持久化，不导入 Agent 或请求模型。"""

import getpass
import sys
import warnings

from .configuration import ConfigurationError, DEFAULT_BASE_URL, ModelConnection
from .config_store import load_user_connection, save_user_connection, user_config_dir


def configure() -> int:
    if not sys.stdin.isatty():
        raise ConfigurationError("configure 需要交互式终端，禁止通过管道输入密钥。")
    existing = load_user_connection()
    print("配置仅支持 Anthropic Messages 兼容接口；不会发起真实 API 请求。")
    print("配置将以明文保存在用户目录，限制文件访问权限，但并非加密凭据库。")
    base_default = existing.base_url if existing else DEFAULT_BASE_URL
    base_url = input(f"API 基础地址 [{base_default}]：").strip() or base_default
    model_default = existing.model if existing else ""
    model = input(f"默认模型 ID [{model_default}]：").strip() or model_default
    # 更换目标地址时必须重新填写密钥，避免把旧站点凭据发送给新站点。
    keep_key = existing is not None and base_url.rstrip("/") == existing.base_url.rstrip("/")
    prompt = "API Key（输入不回显" + ("，回车保留原密钥" if keep_key else "，必填") + "）："
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            secret = getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise ConfigurationError("终端无法隐藏密钥输入，已取消配置。") from None
    if not secret and keep_key:
        secret = existing.api_key
    candidate = ModelConnection(base_url, secret, model,
                                existing.fallback_model if existing else None)
    print(f"保存位置：{user_config_dir() / 'config.json'}")
    print(f"API 地址：{candidate.base_url}\n默认模型：{candidate.model}\nAPI Key：[已设置，不显示]")
    if input("确认保存？[y/N]：").strip().lower() not in ("y", "yes"):
        print("已取消，配置未修改。")
        return 0
    save_user_connection(candidate)
    print("配置已保存。新终端会话可在任意工作目录运行 miniclaude chat/ask。")
    return 0
