"""运行时组装模块：构建工具、Skill、后台任务、Cron、Agent Team 与权限依赖。"""

from __future__ import annotations

from functools import partial
import hashlib
import json
from pathlib import Path
from threading import Event
from typing import Callable

from .config import ProtocolDecision, WORKDIR, TASKS_DIR
from .context.compaction import run_compact_request
from .harness.permissions import configure_permission_handlers
from .harness.prompts import PromptRuntimeContext
from .harness.skills import (
    SkillDefinition,
    configure_skill_registry,
    render_skill_catalog,
    run_load_skill,
    scan_skills,
)
from .harness.subagent import configure_subagent_tools, spawn_subagent
from .harness.todo import run_todo_write
from .jobs.registry import JobRegistry
from .jobs.runner import BackgroundJobRunner
from .jobs.tools import run_job_cancel, run_job_status
from .scheduler.queue import CronTriggerQueue
from .scheduler.service import CronScheduler
from .scheduler.tools import (
    run_cancel_cron,
    run_list_crons,
    run_schedule_cron,
)
from .tasks.tools import (
    run_task_claim,
    run_task_complete,
    run_task_create,
    run_task_get,
    run_task_list,
    run_task_update,
)
from .team.agent import TeammateAgentRunner
from .team.file_bus import FileMailboxBus
from .team.manager import TeammateManager
from .team.protocols import ProtocolRegistry
from .team.registry import TeammateRegistry
from .team.tools import (
    run_check_inbox,
    run_review_plan,
    run_request_shutdown,
    run_send_message,
    run_spawn_teammate,
)
from .tools.filesystem import run_edit, run_glob, run_read, run_shell, run_write
from .tools.registry import collect_enabled_tool_names
from .tools.schemas import TOOLS as DECLARED_TOOLS
from .tools.profiles import select_turn_tools
from .mcp.factories import build_http_factories
from mini_claude_code.mcp_configuration import load_mcp_servers
from .mcp.models import MCPToolResult
from .mcp.registry import MCPRegistry
from .mcp.service import MCPConnectionService
from .mcp.tools import build_connection_tools
from .worktrees.tools import run_worktree_create, run_worktree_status, run_worktree_keep, run_worktree_remove


JOB_REGISTRY = JobRegistry()  # 当前 CLI 进程共享的后台 Job 状态事实来源
JOB_RUNNER = BackgroundJobRunner(JOB_REGISTRY)  # 使用同一注册表执行、取消并等待后台 Job
CRON_TRIGGER_QUEUE = CronTriggerQueue()  # Scheduler 生产、后续 Queue Processor 消费的进程内触发队列
CRON_SCHEDULER = CronScheduler(CRON_TRIGGER_QUEUE)  # 管理 durable/session 计划并执行显式 Tick
SKILL_REGISTRY = scan_skills()  # 启动时扫描得到的 Skill 注册表
configure_skill_registry(SKILL_REGISTRY)

# 复制列表，保证运行时裁剪不会修改工具声明模块本身。
TOOLS = list(DECLARED_TOOLS)
if not SKILL_REGISTRY:
    TOOLS = [tool for tool in TOOLS if tool.get("name") != "load_skill"]

TOOL_HANDLERS: dict[str, Callable] = {
    "shell": run_shell,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
    "task_create": run_task_create,
    "task_list": run_task_list,
    "task_get": run_task_get,
    "task_update": run_task_update,
    "task_claim": run_task_claim,
    "task_complete": run_task_complete,
    "job_status": partial(run_job_status, registry=JOB_REGISTRY),
    "job_cancel": partial(
        run_job_cancel,
        registry=JOB_REGISTRY,
        runner=JOB_RUNNER,
    ),
    "schedule_cron": partial(run_schedule_cron, scheduler=CRON_SCHEDULER),
    "list_crons": partial(run_list_crons, scheduler=CRON_SCHEDULER),
    "cancel_cron": partial(run_cancel_cron, scheduler=CRON_SCHEDULER),
    "delegate": spawn_subagent,
    "compact": run_compact_request,
}
if SKILL_REGISTRY:
    TOOL_HANDLERS["load_skill"] = run_load_skill

# Agent Team 按 Bus → Runner → Manager 顺序组装；Runner 此时只提取 read_file/glob，
# 不要求尚未加入完整注册表的 Lead 团队工具已经存在。
TEAM_MESSAGE_BUS = FileMailboxBus()  # 当前工作区共享的带锁文件消息总线
TEAM_PROTOCOL_REGISTRY = ProtocolRegistry()  # 当前进程共享的协议状态，不随文件邮箱跨进程恢复
TEAMMATE_REGISTRY = TeammateRegistry()  # Runner 与 Manager 共享的成员生命周期注册表
TEAMMATE_STOP_EVENT = Event()  # CLI 生命周期内共享的不可逆软停止信号，不代表模型关闭审批
TEAMMATE_AGENT_RUNNER = TeammateAgentRunner(
    TEAM_MESSAGE_BUS,
    TOOLS,
    TOOL_HANDLERS,
    registry=TEAM_PROTOCOL_REGISTRY,
    teammate_registry=TEAMMATE_REGISTRY,
    stop_event=TEAMMATE_STOP_EVENT,
)  # 每项工作默认只读；已核验隔离 Task 才选入文件写工具，执行仍须计划审批
TEAMMATE_MANAGER = TeammateManager(
    TEAMMATE_AGENT_RUNNER,
    registry=TEAMMATE_REGISTRY,
    stop_event=TEAMMATE_STOP_EVENT,
)  # 当前 CLI 进程中的 Teammate 线程与生命周期管理器


def _request_shutdown(recipient: str, reason: str) -> str:
    """固定 Lead 的关闭请求依赖，模型不能替换 Manager、注册表或总线。"""
    return run_request_shutdown(
        recipient, reason, manager=TEAMMATE_MANAGER,
        registry=TEAM_PROTOCOL_REGISTRY, bus=TEAM_MESSAGE_BUS,
    )


def _review_plan(request_id: str, decision: ProtocolDecision, reason: str) -> str:
    """只开放模型参数，防止额外参数覆盖 Harness 的共享注册表和消息总线。"""
    return run_review_plan(
        request_id, decision, reason,
        registry=TEAM_PROTOCOL_REGISTRY, bus=TEAM_MESSAGE_BUS,
    )


def _worktree_create(task_id: str, name: str) -> str:
    """只接受模型业务参数，固定源工作区与任务板，防止额外参数覆盖依赖。"""
    return run_worktree_create(task_id, name, workspace=WORKDIR, tasks_dir=TASKS_DIR)


def _worktree_status(name: str) -> str:
    """固定只读查询所使用的工作区与任务板。"""
    return run_worktree_status(name, workspace=WORKDIR, tasks_dir=TASKS_DIR)


def _worktree_keep(task_id: str) -> str:
    """只开放任务 ID，模型不能替换保留操作的依赖。"""
    return run_worktree_keep(task_id, workspace=WORKDIR, tasks_dir=TASKS_DIR)


def _worktree_remove(task_id: str) -> str:
    """只开放任务 ID，不提供 force、目录覆盖或删除分支选项。"""
    return run_worktree_remove(task_id, workspace=WORKDIR, tasks_dir=TASKS_DIR)


TOOL_HANDLERS.update(
    {
        "worktree_create": _worktree_create,
        "worktree_status": _worktree_status,
        "worktree_keep": _worktree_keep,
        "worktree_remove": _worktree_remove,
        "review_plan": _review_plan,
        "request_shutdown": _request_shutdown,
        "spawn_teammate": partial(
            run_spawn_teammate,
            manager=TEAMMATE_MANAGER,
        ),
        "send_message": partial(
            run_send_message,
            bus=TEAM_MESSAGE_BUS,
            manager=TEAMMATE_MANAGER,
        ),
        "check_inbox": partial(
            run_check_inbox,
            bus=TEAM_MESSAGE_BUS,
            registry=TEAM_PROTOCOL_REGISTRY,
        ),
    }
)

def _connect_mcp(server_name: str) -> MCPToolResult:
    """固定连接服务，模型不能通过额外参数覆盖注册表或客户端工厂。"""
    return MCP_CONNECTION_SERVICE.connect(server_name)


def _disconnect_mcp(server_name: str) -> MCPToolResult:
    """仅允许按预配置名称断开；调用时服务已完成组装。"""
    return MCP_CONNECTION_SERVICE.disconnect(server_name)


MCP_FACTORIES = build_http_factories(load_mcp_servers())  # 启动读取一次；只构造工厂，不联网
TOOLS.extend(build_connection_tools(tuple(MCP_FACTORIES)))
if MCP_FACTORIES:
    TOOL_HANDLERS.update({"connect_mcp": _connect_mcp, "disconnect_mcp": _disconnect_mcp})
MCP_REGISTRY = MCPRegistry(TOOLS, TOOL_HANDLERS)  # 内置基线包含管理工具；此处不创建外部连接
MCP_CONNECTION_SERVICE = MCPConnectionService(
    MCP_REGISTRY, MCP_FACTORIES,
)  # 不自动连接；用户配置变更需重启，Fake/演示工厂仅供测试

# 底层模块只接收组装结果，不反向导入 runtime，从而避免循环依赖。
configure_permission_handlers(TOOL_HANDLERS)
configure_subagent_tools(TOOLS, TOOL_HANDLERS)


def build_turn_tool_snapshot(*, single_turn: bool) -> tuple[int, list[dict], dict[str, Callable]]:
    """在 Lead 回合边界取一致快照；调用方持有 Agent 锁，不支持并发注册。

    一次性模式始终使用内置表，即使进程中已有连接也不继承 MCP 工具。
    权限层由 PreToolUseContext 接收这份 Handler 表，不更新全局权限映射。
    """
    revision = MCP_REGISTRY.revision
    tools, handlers = (TOOLS, TOOL_HANDLERS) if single_turn else MCP_REGISTRY.assemble_tool_pool()
    selected, selected_handlers = select_turn_tools(tools, handlers, single_turn=single_turn)
    return revision, selected, selected_handlers


def build_prompt_runtime_context(
    tools: list[dict],
    handlers: dict[str, Callable],
    skill_registry: dict[str, SkillDefinition],
    *,
    workspace: Path,
    memory_enabled: bool,
) -> PromptRuntimeContext:
    """根据真实运行状态构建确定性的 System Prompt 上下文快照。"""
    if not isinstance(skill_registry, dict):
        raise ValueError("Skill 注册表必须是字典")
    if not isinstance(workspace, Path):
        raise ValueError("Prompt 运行时上下文的工作区必须是 pathlib.Path 类型")
    if type(memory_enabled) is not bool:
        raise ValueError("Memory 开关必须是布尔值")
    enabled_tools = collect_enabled_tool_names(tools, handlers)
    skill_catalog = ""
    if "load_skill" in enabled_tools and skill_registry:
        skill_catalog = render_skill_catalog(skill_registry)
    return PromptRuntimeContext(
        workspace=str(workspace.resolve()),
        enabled_tools=enabled_tools,
        skill_catalog=skill_catalog,
        memory_enabled=memory_enabled,
        tool_schema_fingerprint=hashlib.sha256(json.dumps(
            tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest(),
    )


# 导入阶段即验证一次完整注册表，使配置错误在进入 Agent Loop 前暴露。
collect_enabled_tool_names(TOOLS, TOOL_HANDLERS)
