"""文件工具目录绑定：为单次工作创建私有闭包，阻止模型覆盖 Harness 指定的目录。"""

from collections.abc import Callable
from pathlib import Path


def bind_file_tool_handler(
    handler: Callable[..., object], *, workspace: Path,
) -> Callable[..., object]:
    """绑定支持 workspace 关键字的文件 Handler，不改变原 Handler 或工具权限。

    调用方先解析工作目录，并将同一个 Path 交给权限 Hook。本层只固定参数，
    不代替文件工具执行时的路径检查，也不适用于 Shell 或协议工具。
    """
    if not callable(handler):
        raise TypeError("文件工具 Handler 必须可调用")
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("文件工具绑定目录必须是绝对 Path")

    def bound_handler(**tool_input: object) -> object:
        # Schema 不是安全边界；即使模型夹带额外字段，也不能覆盖闭包目录。
        if "workspace" in tool_input:
            raise ValueError("workspace 由 Harness 固定，工具参数不得指定工作目录")
        return handler(**tool_input, workspace=workspace)

    return bound_handler
