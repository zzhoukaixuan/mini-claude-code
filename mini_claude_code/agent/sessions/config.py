"""会话存储的纯配置：与会构造模型客户端的运行配置隔离，支持离线读取历史。"""

import re


SESSION_DIRECTORY_NAME = ".sessions"  # 当前启动工作目录下的会话存储目录
SESSION_SCHEMA_VERSION = 1  # 磁盘格式版本；不识别的版本拒绝加载，不猜测迁移
SESSION_ID_PATTERN = re.compile(r"session_[0-9a-f]{32}")  # 随机 ID，不接受路径或任意文件名
SESSION_TITLE_MAX_CHARS = 120  # 选择列表中的单行标题长度上限
SESSION_MAX_BYTES = 20 * 1024 * 1024  # 单份快照上限；超限拒绝，不静默截断历史
SESSION_LOCK_TIMEOUT_SECONDS = 5.0  # 快照读改写事务的锁等待上限
SESSION_LOCK_POLL_SECONDS = 0.05  # 锁竞争时的轮询间隔
INTERRUPTED_TOOL_RESULT = (  # Harness 修复协议缺口，不伪装成真实工具输出
    "[Harness 会话恢复] 上次回合中断，本次工具调用的执行结果未确认保存。"
    "执行结果未知：它可能尚未执行，也可能已产生副作用。"
    "恢复过程未重新执行该工具；请先核验实际状态，不要仅凭本条信息重试副作用操作。"
)
INTERRUPTED_TURN_NOTICE = (  # 恢复后交给模型的状态说明，不主动启动新回合
    "[Harness 会话恢复] 上次回合未确认正常结束。已载入最近保存的上下文，"
    "并未重放历史工具或恢复后台执行。历史文件信息可能过期；"
    "请结合用户的新指令核验当前状态，不要把历史审批视为本次授权。"
)
