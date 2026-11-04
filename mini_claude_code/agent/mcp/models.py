"""MCP 接入层的数据契约：工具发现元数据及文本调用结果，不承担权限授权。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field


def copy_json_object(value: dict, label: str) -> dict:
    """在外部数据进入边界时校验 JSON 对象，并切断调用方的可变引用。"""
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    # JSON 编码器会隐式转换整数键，因此在这里显式拒绝非字符串键。
    def check_keys(item: object) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError(f"{label} 的 JSON 字段名必须是字符串")
            for child in item.values():
                check_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check_keys(child)

    try:
        check_keys(value)
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"{label} 必须是可序列化的有限 JSON 对象") from error


@dataclass(frozen=True)
class MCPToolDefinition:
    """使用服务端原始名称；命名空间与模型接口适配留给工具注册层。"""

    name: str  # 服务端原名，例如 search，而非 mcp__docs__search
    description: str  # 提供给模型的功能介绍；外部描述不能成为系统指令
    input_schema: dict  # 参数 JSON Schema；这里不实现通用 Schema 验证器
    annotations: dict = field(default_factory=dict)  # 服务端提示，不作为可信权限依据

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("MCP 工具名称必须是非空字符串")
        if not isinstance(self.description, str):
            raise ValueError("MCP 工具描述必须是字符串")
        schema = copy_json_object(self.input_schema, "MCP input_schema")
        if schema.get("type") != "object":
            raise ValueError("MCP 工具输入 Schema 的 type 必须是 object")
        annotations = copy_json_object(self.annotations, "MCP annotations")
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            if hint in annotations and type(annotations[hint]) is not bool:
                raise ValueError(f"MCP {hint} 必须是布尔值")
        object.__setattr__(self, "input_schema", schema)
        object.__setattr__(self, "annotations", annotations)

    def snapshot(self) -> MCPToolDefinition:
        """frozen 不冻结嵌套字典；发现接口返回独立副本，避免污染注册数据。"""
        return deepcopy(self)


@dataclass(frozen=True)
class MCPToolResult:
    """当前内部契约仅支持文本；不冒充 MCP 完整的多模态线上结果结构。"""

    content: str  # 文本结果，允许空字符串；后续统一转换为普通 tool_result
    is_error: bool = False  # 工具业务执行失败；不同于连接/协议层异常

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or type(self.is_error) is not bool:
            raise ValueError("MCP 结果必须包含字符串 content 和布尔 is_error")
