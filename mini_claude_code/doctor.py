"""只读环境诊断：不创建运行时、不连接模型/MCP，不展示连接地址和凭据。"""

import importlib.util
import json
from pathlib import Path
import shutil
import sys

from . import __version__
from .configuration import ConfigurationError, resolve_connection
from .mcp_configuration import load_mcp_servers


def collect_diagnostics() -> dict:
    checks = []
    for module in ('anthropic', 'dotenv', 'yaml', 'rich', 'mcp'):
        available = importlib.util.find_spec(module) is not None
        checks.append({'name': module, 'status': 'ok' if available else ('optional-missing' if module == 'mcp' else 'error')})
    checks.append({'name':'git', 'status':'ok' if shutil.which('git') else 'optional-missing'})
    try:
        connection = resolve_connection()
        checks.append({'name':'model_config', 'status':'ok', 'source':connection.source})
    except ConfigurationError:
        checks.append({'name':'model_config', 'status':'error', 'hint':'请运行 miniclaude configure；本诊断不回显配置正文。'})
    try:
        servers = load_mcp_servers()
        checks.append({'name':'mcp_config', 'status':'ok', 'count':len(servers)})
    except ConfigurationError:
        checks.append({'name':'mcp_config', 'status':'error', 'hint':'用户 MCP 配置格式或权限有误。'})
    return {'version':__version__, 'python':sys.version.split()[0],
            'executable':sys.executable, 'package':str(Path(__file__).resolve().parent),
            'workspace':str(Path.cwd().resolve()), 'network_tested':False, 'checks':checks}


def run_doctor(*, as_json: bool = False) -> int:
    report = collect_diagnostics()
    if as_json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(f"MiniClaudeCode {report['version']} / Python {report['python']}")
        for label in ('executable','package','workspace'):
            print(f"{label}: {json.dumps(report[label], ensure_ascii=False)}")
        for check in report['checks']:
            print(f"{check['name']}: {check['status']}" + (f" — {check['hint']}" if 'hint' in check else ''))
        print('只读检查；未验证凭据有效性、服务在线状态或目录可写性。')
    return 1 if any(item['status'] == 'error' for item in report['checks']) else 0
