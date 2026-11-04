# 产品架构

产品入口 `mini_claude_code.cli` → `backend` → `agent.sessions.startup` / `agent.core.agent_loop`。
运行时位于 `mini_claude_code/agent/`。配置在用户目录，WORKDIR 是启动目录，
目录迁移不迁移或删除用户的 Memory、Task、Session、Cron 和 Worktree。

## 分层与生命周期

| 层 | 职责 | 生命周期 |
| --- | --- | --- |
| cli/configuration/config_store | 参数、配置选择与私有存储 | 进程启动与独立管理命令 |
| agent/sessions | 会话选择、占用锁、检查点与恢复修复 | 当前聊天会话 |
| agent/runtime | 工具/Policy/Team/Job/Cron/MCP 依赖组装 | 当前进程 |
| agent/core | 模型请求、工具批次、恢复、结果配对 | Agent 回合 |
| agent/harness/context/memory | 权限、Hook、Todo、Skill、上下文与记忆 | 按各模块契约管理 |
| agent/tasks/jobs/scheduler/team/worktrees | 持久工作事实、异步执行与隔离 | 磁盘定义与进程执行分开 |
| agent/mcp | HTTP 连接、目录注册与外部工具适配 | 连接到显式断开/CLI 退出 |

工具路径：模型 tool_use → PreToolUse/权限 → Handler → PostToolUse → 配对 tool_result。
MCP 工具目录只在模型请求边界刷新；服务端声明不是权限；超时不自动重放副作用。

## 创建和退出

帮助、doctor 和配置管理不导入 Agent 运行配置；真正进入 ask/chat 才加载运行时。
当前 runtime/config 仍存在进程级对象与首次导入初始化；一次进程固定工作目录和模型连接。
chat 显式启动后台 Runner，退出时停止入口、有界等待，再清理 MCP 和会话占用。
ask 不创建 Session 或启动后台 Runner，不开放 MCP 连接管理。

当前尚未实现无全局状态的可重入 Runtime 容器。
未来多会话共进程/嵌入式运行需要额外生命周期重构；当前用新进程隔离。
