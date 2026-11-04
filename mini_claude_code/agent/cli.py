"""CLI 模块：串行处理交互、Cron 与团队消息回合，并管理后台 Runner 生命周期。"""

from __future__ import annotations

from threading import Lock

from rich import print as rprint

from .sessions.lifecycle import CheckpointEvent, SessionController, SessionPersistenceError
from . import __version__
from .config import (
    API_KEY,
    CRON_RUNNER_JOIN_TIMEOUT_SECONDS,
    SCHEDULED_AGENT_PERMISSION_MODE,
    TEAM_INBOX_AGENT_PERMISSION_MODE,
    TEAM_INBOX_RUNNER_JOIN_TIMEOUT_SECONDS,
    TEAMMATE_JOIN_TIMEOUT_SECONDS,
    TEAM_LEAD_NAME,
    TERMINAL_JOB_STATUSES,
    WORKDIR,
    PermissionMode,
    console,
)
from .core.agent_loop import agent_loop
from .harness.btw import (
    format_btw_isolation_report,
    run_isolated_btw_query,
)
from .harness.hooks import ErrorContext, notify_error
from .harness.prompts import PromptCacheState, get_prompt_assembly
from .harness.todo import reset_todo_state
from .memory.consolidation import (
    handle_memory_command,
    maybe_prepare_memory_consolidation,
)
from .memory.store import MemoryCLIState
from .runtime import (
    MCP_CONNECTION_SERVICE,
    JOB_REGISTRY,
    CRON_SCHEDULER,
    CRON_TRIGGER_QUEUE,
    SKILL_REGISTRY,
    TEAM_MESSAGE_BUS,
    TEAM_PROTOCOL_REGISTRY,
    TEAMMATE_MANAGER,
    TOOL_HANDLERS,
    TOOLS,
    build_prompt_runtime_context,
)
from .scheduler.events import CronTrigger
from .scheduler.runner import (
    CronPollingRunner,
    CronQueueProcessor,
    CronRunnerStage,
)
from .team.inbox_runner import (
    LeadInboxProcessor,
    TeamInboxRunnerStage,
)
from .team.models import TeamMessage
from .team.routing import route_inbox_messages
from .team.recovery import handle_task_command
from .team.tools import render_lead_inbox_event


def _check_memory_consolidation(
    memory_state: MemoryCLIState,
    known_memory_secrets: tuple[str, ...],
) -> None:
    """在有效 Agent 回合后执行一次可失败但不影响答案的 Memory 整理检查。"""
    try:
        consolidation_notice = maybe_prepare_memory_consolidation(
            memory_state,
            known_memory_secrets,
        )
    except Exception as error:
        notify_error(ErrorContext(stage="Memory", error=error))
        console.print(
            "Memory 自动整理检查失败，本轮回答不受影响。",
            style="yellow",
        )
    else:
        if consolidation_notice:
            console.print(consolidation_notice, style="dim", markup=False)


def _run_agent_turn(
    query: str,
    history: list[dict],
    *,
    permission_mode: PermissionMode,
    memory_state: MemoryCLIState,
    prompt_cache: PromptCacheState,
    known_memory_secrets: tuple[str, ...],
    session: SessionController | None = None,
) -> str:
    """执行一个串行 Agent 回合，并复用交互与自动请求共同的 Memory 收尾。"""
    completed = False
    if session is not None:
        for warning in session.prepare_turn(history):
            console.print(warning, style="yellow", markup=False)

    def checkpoint(messages: list[dict], event: CheckpointEvent) -> None:
        nonlocal completed
        if session is not None:
            session.checkpoint(messages, event)
        completed = event == "completed"

    answer = agent_loop(
        query,
        history,
        permission_mode=permission_mode,
        memory_enabled=memory_state.enabled,
        prompt_cache=prompt_cache,
        checkpoint=checkpoint,
    )
    if completed:
        _check_memory_consolidation(memory_state, known_memory_secrets)
    return answer


def _render_scheduled_query(trigger: CronTrigger) -> str:
    """将可信调度元数据与保存的任务正文组合为一个新的用户回合。"""
    if not isinstance(trigger, CronTrigger):
        raise TypeError("定时 Agent 只能接收 CronTrigger")
    return (
        "<scheduled-cron-event>\n"
        "这是 Harness 根据已登记 CronJob 自动生成的定时用户回合。\n"
        f"CronJob ID：{trigger.cron_job_id}\n"
        f"计划时间槽：{trigger.scheduled_slot}\n"
        f"计划时区：{trigger.timezone}\n"
        "自动交付使用 strict 权限，注册成功不代表执行成功。\n"
        "</scheduled-cron-event>\n\n"
        "以下是计划中保存的任务正文：\n"
        f"{trigger.prompt}"
    )


def _deliver_scheduled_trigger(
    trigger: CronTrigger,
    history: list[dict],
    memory_state: MemoryCLIState,
    prompt_cache: PromptCacheState,
    known_memory_secrets: tuple[str, ...],
    session: SessionController | None = None,
) -> None:
    """在 Queue Processor 已持有 Agent 锁时交付一个定时回合。"""
    console.print(
        "\n[Cron] 正在交付定时任务 "
        f"{trigger.cron_job_id}（{trigger.scheduled_slot}）。",
        style="magenta",
        markup=False,
    )
    answer = _run_agent_turn(
        _render_scheduled_query(trigger),
        history,
        permission_mode=SCHEDULED_AGENT_PERMISSION_MODE,
        memory_state=memory_state,
        prompt_cache=prompt_cache,
        known_memory_secrets=known_memory_secrets,
        session=session,
    )
    console.print(
        f"\n[Cron] {trigger.cron_job_id} 回合结果：\n{answer}",
        markup=False,
    )


def _deliver_team_inbox(
    messages: list[TeamMessage],
    history: list[dict],
    memory_state: MemoryCLIState,
    prompt_cache: PromptCacheState,
    known_memory_secrets: tuple[str, ...],
    session: SessionController | None = None,
) -> None:
    """在 Inbox Processor 已持有 Agent 锁时交付一个自动团队消息回合。"""
    if session is not None:
        session.ensure_available()
    # Processor 已取得 Agent 锁并消费邮箱；此处仅路由一次，不再次读取邮箱。
    routed = route_inbox_messages(
        messages, recipient=TEAM_LEAD_NAME, registry=TEAM_PROTOCOL_REGISTRY
    )
    if not (routed.deliverable_messages or routed.resolved_responses or routed.errors):
        return
    inbox_event = render_lead_inbox_event(routed)
    console.print(
        f"\n[Agent Team] 正在向 Lead 交付 {len(messages)} 条团队消息。",
        style="magenta",
        markup=False,
    )
    answer = _run_agent_turn(
        inbox_event,
        history,
        permission_mode=TEAM_INBOX_AGENT_PERMISSION_MODE,
        memory_state=memory_state,
        prompt_cache=prompt_cache,
        known_memory_secrets=known_memory_secrets,
        session=session,
    )
    console.print(
        f"\n[Agent Team] Lead 自动回合结果：\n{answer}",
        markup=False,
    )


def _report_cron_runner_error(
    stage: CronRunnerStage,
    error: Exception,
) -> None:
    """将后台 Cron 故障接入统一 Error Hook，并说明 Runner 会继续工作。"""
    notify_error(ErrorContext(stage=stage, error=error))
    console.print(
        f"{stage} 单次执行失败，后台 Runner 将继续轮询。",
        style="yellow",
        markup=False,
    )


def _report_team_inbox_runner_error(
    stage: TeamInboxRunnerStage,
    error: Exception,
) -> None:
    """将团队邮箱后台故障接入 Error Hook，并保持后续轮询存活。"""
    notify_error(ErrorContext(stage=stage, error=error))
    console.print(
        f"{stage} 单次执行失败，Lead Inbox Processor 将继续轮询。",
        style="yellow",
        markup=False,
    )


def _handle_cli_query(
    query: str,
    history: list[dict],
    memory_state: MemoryCLIState,
    prompt_cache: PromptCacheState,
    known_memory_secrets: tuple[str, ...],
    session: SessionController | None = None,
) -> None:
    """在主线程持有 Agent 锁时处理一条非空、非退出输入。"""
    if session is not None:
        session.ensure_available()
    query_parts = query.split(maxsplit=1)
    if query_parts[0].casefold() == "/task":
        handle_task_command(query, TEAMMATE_MANAGER)
        return  # 手工维护不调用模型，也不写入主会话历史或触发 Memory 学习
    if query_parts[0].casefold() == "/btw":
        if len(query_parts) != 2 or not query_parts[1].strip():
            console.print("用法：/btw <question>", style="yellow", markup=False)
            return
        try:
            btw_prompt_context = build_prompt_runtime_context(
                TOOLS,
                TOOL_HANDLERS,
                SKILL_REGISTRY,
                workspace=WORKDIR,
                memory_enabled=memory_state.enabled,
            )
            btw_prompt_lookup = get_prompt_assembly(
                btw_prompt_context,
                prompt_cache,
            )
            answer, isolation_report = run_isolated_btw_query(
                query_parts[1],
                history,
                btw_prompt_lookup.assembly,
            )
        except Exception as error:
            console.print(
                f"/btw 执行失败：{type(error).__name__}：{error}",
                style="red",
                markup=False,
            )
        else:
            console.print("\n" + answer, markup=False)
            console.print(
                format_btw_isolation_report(isolation_report),
                style="dim",
                markup=False,
            )
        return

    if query_parts[0].casefold() == "/memory":
        try:
            output = handle_memory_command(
                query,
                memory_state,
                known_memory_secrets,
            )
        except Exception as error:
            console.print(
                f"Memory 命令执行失败：{type(error).__name__}：{error}",
                style="red",
                markup=False,
            )
        else:
            console.print(output, style="cyan", markup=False)
        return

    answer = _run_agent_turn(
        query,
        history,
        permission_mode="ask",
        memory_state=memory_state,
        prompt_cache=prompt_cache,
        known_memory_secrets=known_memory_secrets,
        session=session,
    )
    rprint("\n" + answer)


def _stop_cron_runners(
    polling_runner: CronPollingRunner,
    queue_processor: CronQueueProcessor,
) -> None:
    """先阻止新 Tick，再有界等待两个 daemon 线程完成软停止。"""
    polling_runner.request_stop()
    queue_processor.request_stop()
    polling_stopped = polling_runner.join(CRON_RUNNER_JOIN_TIMEOUT_SECONDS)
    processor_stopped = queue_processor.join(CRON_RUNNER_JOIN_TIMEOUT_SECONDS)
    if not polling_stopped:
        console.print(
            "Cron Scheduler 未在退出时限内停止；不会继续阻塞 CLI。",
            style="yellow",
            markup=False,
        )
    if not processor_stopped:
        console.print(
            "Cron Queue Processor 未在退出时限内停止；"
            "正在执行的 Agent 回合将由进程退出终止。",
            style="yellow",
            markup=False,
        )


def _stop_team_inbox_processor(processor: LeadInboxProcessor) -> None:
    """请求停止 Lead Inbox Processor，并进行一次有界等待。"""
    processor.request_stop()
    if not processor.join(TEAM_INBOX_RUNNER_JOIN_TIMEOUT_SECONDS):
        console.print(
            "Lead Inbox Processor 未在退出时限内停止；"
            "正在执行的自动 Agent 回合将由进程退出终止。",
            style="yellow",
            markup=False,
        )


def _stop_background_runners(
    polling_runner: CronPollingRunner,
    queue_processor: CronQueueProcessor,
    team_inbox_processor: LeadInboxProcessor,
    session: SessionController,
) -> None:
    """先关闭全部新增工作入口，再有界等待；停止不经过模型审批。"""
    # 先关 spawn 入口，防止仍在结束中的 Lead 回合继续创建成员。
    MCP_CONNECTION_SERVICE.request_stop()
    TEAMMATE_MANAGER.request_stop_all()
    polling_runner.request_stop()
    queue_processor.request_stop()
    team_inbox_processor.request_stop()
    try:
        _stop_team_inbox_processor(team_inbox_processor)
    finally:
        try:
            _stop_cron_runners(polling_runner, queue_processor)
        finally:
            if not TEAMMATE_MANAGER.join_all(TEAMMATE_JOIN_TIMEOUT_SECONDS):
                console.print(
                    "仍有 Teammate 未在退出时限内结束；同步模型或工具调用无法立即中断。"
                    "未确认全部 Task 已释放，请在下次启动时核验任务状态。",
                    style="yellow", markup=False,
                )
            # 线程退出不等于业务清理成功；释放失败会留在成员错误快照中。
            for snapshot in TEAMMATE_MANAGER.list_all():
                if snapshot.status == "failed":
                    console.print(
                        f"Teammate {snapshot.spec.name!r} 失败：{snapshot.error}",
                        style="yellow", markup=False,
                    )
            # 有界停止可能超时；只有所有工作线程都结束才释放进程间会话所有权。
            stopped = (
                polling_runner.join(0.01), queue_processor.join(0.01),
                team_inbox_processor.join(0.01), TEAMMATE_MANAGER.join_all(0.01),
            )
            jobs_finished = all(job.status in TERMINAL_JOB_STATUSES for job in JOB_REGISTRY.list_jobs())
            session.release_safe = all(stopped) and jobs_finished
            # 只有自动 Lead 回合停止后，才可安全地串行修改 MCP 注册表。
            if stopped[1] and stopped[2]:
                cleanup_errors = MCP_CONNECTION_SERVICE.close_all()
                for message in cleanup_errors:
                    console.print(message, style="yellow", markup=False)
                if cleanup_errors:
                    session.release_safe = False
            else:
                session.release_safe = False
                console.print("Lead 后台回合尚未退出，未并发清理 MCP 连接；可能存在残留连接或线程，请核验。", style="yellow")
            if not session.release_safe:
                console.print("仍有后台工作未结束，会话占用锁将保留到进程退出。", style="yellow")


def main(argv: list[str] | None = None) -> int:
    """兼容直接调用；模块入口优先走轻量 startup，避免预先加载运行时。"""
    from .sessions.startup import main as startup_main
    return startup_main(argv)


def run_cli(session: SessionController, history: list[dict]) -> int:
    """使用已取得所有权、校验并保存好的历史启动交互和后台工作。"""
    rprint(
        "\n"
        f"[bold cyan]MiniClaudeCode v{__version__}[/bold cyan]\n"
        "[dim]────────────────────────────────────────[/dim]\n"
        "[white]编程智能体 · 多轮对话 · [/white][yellow]Ask 权限模式[/yellow]\n"
        "\n"
        "[dim]工作目录：[/dim]当前目录\n"
        "[yellow]安全提示：[/yellow]权限规则不等于操作系统级沙箱\n"
        "[dim]退出命令：[/dim][cyan]exit[/cyan] / [cyan]quit[/cyan]\n"
        "[dim]旁路问答：[/dim][cyan]/btw <question>[/cyan]\n"
        "[dim]Memory 命令：[/dim][cyan]/memory status|list|on|off|delete <name>[/cyan]\n"
        "[dim]Memory 整理：[/dim][cyan]/memory consolidate (show|apply|cancel)[/cyan]\n"
        "[dim]遗留 Task 恢复：[/dim][cyan]/task recover [标题关键词][/cyan][dim]（列表选择并确认）[/dim]\n"
        "[dim]Cron 自动交付：[/dim][yellow]Strict 权限模式[/yellow]\n"
        "[dim]团队邮箱自动交付：[/dim][yellow]Strict 权限模式[/yellow]\n"
        "[dim]────────────────────────────────────────[/dim]"
    )
    reset_todo_state()
    memory_state = MemoryCLIState()  # 当前 CLI 的 Memory 开关、计数与待审核整理计划
    prompt_cache = PromptCacheState()  # 用户回合与定时回合共享的 Prompt 组装缓存
    known_memory_secrets: tuple[str, ...]
    if isinstance(API_KEY, str) and API_KEY:
        known_memory_secrets = (API_KEY,)
    else:
        known_memory_secrets = ()

    if session.store.workspace != WORKDIR:
        raise ValueError("会话工作目录与运行时 WORKDIR 不一致，请在目标目录启动新进程")
    session.secrets = known_memory_secrets
    session.checkpoint(history, "completed")  # 客户端配置加载后补充显式密钥脱敏
    console.print(f"当前会话：{session.snapshot.session_id}", style="dim", markup=False)

    agent_lock = Lock()  # 串行保护 history、Memory、Prompt 缓存与完整 Agent 回合

    def deliver_scheduled_trigger(trigger: CronTrigger) -> None:
        """绑定当前 CLI 会话状态；Agent 锁由 Queue Processor 在外层持有。"""
        _deliver_scheduled_trigger(
            trigger,
            history,
            memory_state,
            prompt_cache,
            known_memory_secrets,
            session,
        )

    def deliver_team_inbox(messages: list[TeamMessage]) -> None:
        """绑定当前 CLI 会话状态；Agent 锁由 Inbox Processor 在外层持有。"""
        _deliver_team_inbox(
            messages,
            history,
            memory_state,
            prompt_cache,
            known_memory_secrets,
            session,
        )

    def report_background_error(stage: CronRunnerStage | TeamInboxRunnerStage, error: Exception) -> None:
        """存储错误停止所有新增回合；普通后台错误保持原有报告行为。"""
        if isinstance(error, SessionPersistenceError):
            polling_runner.request_stop()
            queue_processor.request_stop()
            team_inbox_processor.request_stop()
            TEAMMATE_MANAGER.request_stop_all()
            console.print(str(error), style="red", markup=False)
        elif stage in ("TeamInboxPoller", "TeamInboxDelivery"):
            _report_team_inbox_runner_error(stage, error)
        else:
            _report_cron_runner_error(stage, error)

    polling_runner = CronPollingRunner(
        CRON_SCHEDULER,
        error_callback=report_background_error,
    )
    queue_processor = CronQueueProcessor(
        CRON_TRIGGER_QUEUE,
        agent_lock,
        deliver_scheduled_trigger,
        error_callback=report_background_error,
    )
    team_inbox_processor = LeadInboxProcessor(
        TEAM_MESSAGE_BUS,
        agent_lock,
        deliver_team_inbox,
        error_callback=report_background_error,
    )

    session.release_safe = False  # 从启动后台线程起，必须经过停止确认才能释放占用锁
    try:
        queue_processor.start()
        polling_runner.start()
    except Exception as error:
        notify_error(ErrorContext(stage="CronScheduler", error=error))
        console.print(
            f"Cron Runner 启动失败：{type(error).__name__}：{error}",
            style="red",
            markup=False,
        )
        _stop_background_runners(polling_runner, queue_processor, team_inbox_processor, session)
        return 1

    try:
        team_inbox_processor.start()
    except Exception as error:
        notify_error(ErrorContext(stage="TeamInboxPoller", error=error))
        console.print(
            "Lead Inbox Processor 启动失败："
            f"{type(error).__name__}：{error}",
            style="red",
            markup=False,
        )
        _stop_background_runners(polling_runner, queue_processor, team_inbox_processor, session)
        return 1

    console.print(
        "Cron Scheduler、Queue Processor 与 Lead Inbox Processor 已启动。",
        style="dim",
        markup=False,
    )
    try:
        while not session.failed:
            try:
                query = console.input("\n[bold cyan]>[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                rprint("\n[dim]正在停止后台工作并退出 MiniClaudeCode。[/dim]")
                break
            if query.casefold() in ["exit", "quit"]:
                rprint("[dim]正在停止后台工作并退出 MiniClaudeCode。[/dim]")
                break
            if not query:
                continue

            # console.input 必须在锁外；只有实际处理共享会话状态时才串行化。
            with agent_lock:
                _handle_cli_query(
                    query,
                    history,
                    memory_state,
                    prompt_cache,
                    known_memory_secrets,
                    session,
                )
    except SessionPersistenceError as error:
        console.print(str(error), style="red", markup=False)
    except KeyboardInterrupt:
        console.print("\n回合已中断；保留最近成功检查点。", style="yellow")
    finally:
        _stop_background_runners(polling_runner, queue_processor, team_inbox_processor, session)
        # 不与尚未结束的后台回合争抢或并发保存；该回合已有阶段检查点。
        if agent_lock.acquire(blocking=False):
            try:
                if not session.failed:
                    session.checkpoint(
                        history, "interrupted" if session.snapshot.turn_in_progress else "completed",
                    )
            except SessionPersistenceError as error:
                console.print(str(error), style="red", markup=False)
            finally:
                agent_lock.release()
        else:
            console.print("后台回合尚未结束，退出时保留最近成功检查点。", style="yellow")
    return 1 if session.failed else 0
