"""会话生命周期：共享检查点、明确完成信号及保存失败后的执行熔断。"""

from dataclasses import replace
from typing import Callable, Literal

from ..context.serialization import collect_transcript_secrets
from .config import SESSION_TITLE_MAX_CHARS
from .messages import make_session_messages, restore_session_messages
from .models import SessionSnapshot
from .store import SessionStore

CheckpointEvent = Literal["progress", "completed", "interrupted"]  # 主循环明确发出的生命周期信号
SessionCheckpoint = Callable[[list[dict], CheckpointEvent], None]  # 接收当前历史与事件的保存回调


class SessionPersistenceError(RuntimeError):
    """保存失败后禁止继续调用模型或执行工具；不自动重试副作用。"""


class SessionController:
    """调用者持有共享 Agent 锁；控制器只管理一个会话的磁盘版本。"""

    def __init__(self, store: SessionStore, snapshot: SessionSnapshot,
                 secrets: tuple[str, ...] = ()) -> None:
        self.store = store  # 当前启动工作区的存储入口
        self.snapshot = snapshot  # 最近一次成功提交的恢复点及 revision
        self.secrets = secrets  # 客户端显式配置的已知密钥
        self.failed = False  # 保存失败后保持熔断，不能原地盲目重试
        self.release_safe = True  # CLI 确认后台全部退出后，才允许释放会话占用锁

    def ensure_available(self) -> None:
        if self.failed:
            raise SessionPersistenceError("会话保存已失败，禁止继续执行；请退出并检查存储。")

    def checkpoint(self, history: list[dict], event: CheckpointEvent) -> None:
        """先序列化、脱敏和校验，再提交；不把脱敏后的内容覆盖在线上下文。"""
        self.ensure_available()
        stage = "消息序列化或协议校验"
        try:
            messages = make_session_messages(
                history, secrets=tuple(set(self.secrets + collect_transcript_secrets())),
            )
            title = self.snapshot.title
            if self.snapshot.revision == 0 and messages:
                content = messages[0]["content"]
                if isinstance(content, str):
                    plain = "".join(char if ord(char) >= 32 and char != "\x7f" else " " for char in content)
                    title = " ".join(plain.split())[:SESSION_TITLE_MAX_CHARS].strip() or "新会话"
            candidate = replace(
                self.snapshot, messages=messages, title=title,
                turn_in_progress=event != "completed",
            )
            if event == "completed":
                restore_session_messages(candidate)  # 正常完成不允许缺失工具结果
            if candidate == self.snapshot:
                return  # 多个安全边界之间历史没有变化时，不重复写整份快照
            stage = "磁盘提交或版本校验"
            self.snapshot = self.store.save(candidate)
        except Exception as error:
            self.failed = True
            raise SessionPersistenceError(
                f"会话检查点保存失败（{stage}阶段，{type(error).__name__}），"
                "已停止后续执行；若本轮已执行工具，请核验实际文件。"
            ) from error

    def prepare_turn(self, history: list[dict]) -> tuple[str, ...]:
        """上轮中断后先补齐未知结果并保存，再允许新的请求；不重放工具。"""
        self.ensure_available()
        if not self.snapshot.turn_in_progress:
            return ()
        try:
            restored, warnings = restore_session_messages(self.snapshot)
        except Exception as error:
            self.failed = True
            raise SessionPersistenceError("中断会话校验失败，禁止继续执行；请检查恢复点。") from error
        self.checkpoint(restored, "completed")
        history[:] = restored
        return warnings
