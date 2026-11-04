"""消息归档与会话保存共用的脱敏常量；不依赖 .env 或模型客户端。"""

import re


TRANSCRIPT_REDACTION_MARKER = "[REDACTED]"  # 已知敏感值的占位文本
MIN_TRANSCRIPT_SECRET_CHARS = 8  # 从环境变量自动收集敏感值的最小长度
SENSITIVE_ENV_NAME_PATTERN = re.compile(  # 自动收集已知密钥时匹配的环境变量名
    r"(?:^|_)(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|PRIVATE_KEY)(?:$|_)",
    re.IGNORECASE,
)
