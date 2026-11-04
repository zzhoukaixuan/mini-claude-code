# MiniClaudeCode

面向 Harness Engineering 的 Coding Agent CLI：将模型决策与工具执行、权限、上下文管理、持久化和多 Agent 协作分开。

当前版本 **19.1.0rc1**，为早期发布候选，尚未发布到 PyPI。主要开发与验证环境为 Windows + Python 3.11。
没有操作系统级沙箱；请在可恢复的工作区使用，并检查副作用工具的授权提示。

## 安装与配置

需要 Python 3.11+；Worktree 功能还需要 Git 和已有提交。

```powershell
git clone https://github.com/zzhoukaixuan/mini-claude-code.git
cd mini-claude-code
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[mcp]"
miniclaude configure
miniclaude doctor
```

Linux/macOS 的环境激活命令为 `source .venv/bin/activate`。不需要 MCP 时可以使用 `python -m pip install -e .`。
若找不到命令，请激活安装环境，或使用 `python -m mini_claude_code`。

`configure` 交互保存 API 地址、Key 和模型 ID 到用户目录 `~/.mini-claude-code/config.json`，Key 输入不回显。
配置是本地明文、不是加密凭据库，请勿分享。无需在每个工作区创建 `.env`。
当前支持 **Anthropic Messages 兼容协议**；OpenRouter 地址填写 `https://openrouter.ai/api`。
请填写自己账户与地区可用的模型。configure/doctor 不发起真实模型调用、不验证余额或模型可用性。

配置优先级：显式 `--env-file` → 完整进程环境配置 → 用户级配置 → 无用户配置时兼容工作区 `.env`。
地址/Key/模型必须来自完整配置；仅 `MODEL_ID` / `FALLBACK_MODEL_ID` 可单独覆盖用户配置。
环境配置使用 `OPENROUTER_BASE_URL`、`OPENROUTER_API_KEY`、`MODEL_ID`。修改后重启进程。

## 使用

切换到要操作的项目目录后运行：

```powershell
miniclaude ask --permission-mode strict --no-memory "阅读 README.md，概括项目结构"
miniclaude chat
miniclaude chat --continue
miniclaude chat --resume
miniclaude --help
```

- `ask`：一次性任务，不保存聊天 Session，不启动后台调度，不开放 MCP 连接管理；Memory、Task 等仍有各自的持久化规则。
- `chat`：多轮会话，支持任务、后台 Job、Cron、团队通信与审批、Worktree、MCP。
- `--continue`：恢复当前工作目录最近会话；`--resume`：按编号选择，也可以提供完整会话 ID。
- `doctor [--json]`：只读诊断依赖和配置，不测试外部服务。

Session 位于工作目录 `.sessions/`。恢复会话不重放工具、不恢复运行中的队友/Job/审批，也不回滚磁盘文件。
当前进程固定工作目录与模型连接，尚不支持同进程多工作区。

## MCP：Streamable HTTP

```powershell
miniclaude mcp add maps
miniclaude mcp list
miniclaude mcp show maps
miniclaude mcp test maps
miniclaude chat
miniclaude mcp remove maps
```

`add` 交互录入服务地址与认证；不要把真实 Key 写入仓库或聊天示例。
`test` 会真实连接服务并发现工具。chat 内连接与工具调用仍受权限控制，服务端声明不能自动授予权限。
仅实现 Streamable HTTP，不支持 stdio 或 OAuth 自动登录。超时不代表远端未执行，不自动重放副作用工具。

## Harness 能力与边界

- 文件工具、行范围读取、原子写入和编辑前版本核验。
- 权限策略、Pre/Post Tool Hook、结构化结果配对和模型错误恢复。
- Todo、Skill 按需加载、上下文压缩、跨会话 Memory。
- Task DAG、跨进程文件锁、后台 Job、Cron 定义持久化。
- 团队邮箱、计划审批、任务认领与人工恢复、Git Worktree 隔离。

Worktree 只作用于已绑定并通过核验的 Task；不自动初始化 Git 或合并成员成果。
邮箱投递仍是 at-most-once；后台执行状态不能完整跨进程恢复；权限与路径检查不能替代 OS 沙箱。

### 安全与已知限制

- 本工具以当前用户权限执行；Shell 黑名单不能证明任意命令安全，只在可信、可恢复的工作区使用。
- MCP 默认 HTTPS，禁止自动重定向；HTTP 仅显式放行字面回环地址。外部描述和结果可能含提示注入，只读声明不是授权。
- MCP 当前仅支持文本结果，不支持旧式 SSE、OAuth 自动登录、多模态及 structuredContent；配置不热加载，也不自动重连。
- 配置以受限权限明文保存：Windows 当前用户 DACL，POSIX 目录 700/文件 600。不要分享配置、`.env`、会话、记忆、任务输出和未经审查的日志。
- 已知凭据的精确替换仅保护部分日志和持久化内容，不保证抵御编码、分片或主动外泄。
- 会话缺失工具结果时补充“执行状态未知”的占位符，不自动重放；也不恢复文件读取版本，编辑前需重新读取。
- durable Cron 仅保存定义，后台执行、触发队列和结果不会完整恢复；退出期间的软停止不保证立即中断请求或撤销副作用。
- 邮箱处理失败不自动重投；遗留任务认领需人工核验恢复，恢复不撤销磁盘修改。
- doctor 不验证密钥有效性、服务在线状态、目录可写性或权限策略完整性；本地 Prompt 缓存不代表 Provider 缓存命中。

## 目录说明

普通使用者只需阅读本页的安装、配置和使用步骤；`tests/` 供开发者验证修改，无需手动运行其中的文件来使用产品。

```text
mini-claude-code/
├── mini_claude_code/   产品源码：CLI、配置与 agent 运行时
├── tests/             离线产品测试与模拟工具，不安装到用户环境
├── README.md          使用说明、目录介绍和安全边界
├── pyproject.toml     依赖、版本、许可证元数据与 miniclaude 安装入口
├── LICENSE            MIT 许可证
└── .gitignore         排除密钥、运行记录及本地构建产物
```

开发者可从 `mini_claude_code/cli.py` → `backend.py` → `agent/core/agent_loop.py` 阅读主调用链。
聊天的会话入口位于 `agent/sessions/startup.py`；`agent/runtime.py` 负责工具、权限和后台组件的组装。
工具执行遵循 tool_use → PreToolUse/权限 → Handler → PostToolUse → tool_result 配对流程。
`agent/` 下的 `context`、`memory`、`tasks`、`jobs`、`scheduler`、`team`、`worktrees` 和 `mcp`
分别承载上下文、记忆、持久任务、后台作业、定时调度、团队协作、工作区隔离和外部工具接入。
帮助和配置管理不启动 Agent；chat 退出时有界等待并清理后台组件。当前仍有进程级状态，不是可重入的多工作区 Runtime。

## 开发与验证

```powershell
python tests/run_tests.py
```

测试使用模拟模型，不消耗真实 API 额度。发行包测试验证 sdist、wheel、许可证和独立安装；
临时环境离线复用本机依赖，不等同于全新机器上的联网安装验证。
本仓库测试是产品发布测试子集，不声称提供完整任务成功率或基准评测成绩。

需要构建发行包的维护者可以使用标准构建工具（普通使用者无需执行）：

```powershell
python -m pip install build
python -m build
```

产物位于被 Git 忽略的 `dist/`，构建不等于上传 PyPI。wheel 只安装产品包；源码发行包可能包含测试，
两者均应包含 README 元数据及 LICENSE。发布前仍需检查内容，勿在源码中放入真实凭据。

## 许可证与致谢

本项目采用 [MIT License](LICENSE)，Copyright (c) 2026 zzhoukaixuan。
学习与设计参考了 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)，
感谢 shareAI Lab 提供的教学内容。本仓库不包含教材、课程快照或私人学习笔记。
