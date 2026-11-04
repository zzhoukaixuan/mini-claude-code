"""从启动时用户清单生成连接管理 Schema；不将 URL、认证信息提供给模型。"""


def build_connection_tools(server_names: tuple[str, ...]) -> list[dict]:
    if not server_names:
        return []  # 无配置就不向模型展示不可用的连接管理能力
    return [
        {"name": name, "description": description, "input_schema": {
            "type": "object", "properties": {"server_name": {
                "type": "string", "enum": list(server_names),
                "description": "用户预配置的 MCP 服务名称；不可自行提供地址、凭据或启动命令。"}},
            "required": ["server_name"], "additionalProperties": False}}
        for name, description in (
            ("connect_mcp", "连接用户预配置的 Streamable HTTP MCP 服务并发现工具，需要人工批准。"
             "不启动本地程序，不自动执行发现的工具；新增工具以之后实际收到的工具列表为准。"),
            ("disconnect_mcp", "断开预配置 MCP 服务并移除工具，需要人工批准；不会撤销已产生的副作用。"))
    ]
