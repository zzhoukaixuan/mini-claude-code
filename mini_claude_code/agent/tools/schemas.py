"""工具 Schema 模块：集中声明主模型可见的工具名称、中文说明和参数 JSON Schema。"""

from __future__ import annotations

from ..config import (
    MAX_PROTOCOL_PAYLOAD_LENGTH,
    TEAM_REQUEST_ID_PATTERN,
    CRON_ID_PATTERN,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    DEFAULT_SHELL_TIMEOUT_SECONDS,
    FILE_VERSION_PATTERN,
    JOB_ID_PATTERN,
    MAX_COMPACT_FOCUS_CHARS,
    MAX_CRON_EXPRESSION_LENGTH,
    MAX_CRON_PROMPT_LENGTH,
    MAX_JOB_TIMEOUT_SECONDS,
    MAX_TASK_DEPENDENCIES,
    MAX_TASKS,
    MAX_TASK_DESCRIPTION_LENGTH,
    MAX_TASK_RESULT_LENGTH,
    MAX_TASK_SUBJECT_LENGTH,
    MAX_TEAMMATE_PROMPT_LENGTH,
    MAX_TEAMMATE_ROLE_LENGTH,
    MAX_TEAM_MEMBER_NAME_LENGTH,
    MAX_TEAM_MESSAGE_CONTENT_LENGTH,
    MAX_TODO_CONTENT_LENGTH,
    MAX_TODOS,
    MIN_JOB_TIMEOUT_SECONDS,
    TASK_ID_PATTERN,
    TEAM_MEMBER_NAME_PATTERN,
    VALID_CRON_TIMEZONES,
    VALID_STATUS,
    MAX_WORKTREE_NAME_LENGTH,
    WORKTREE_NAME_PATTERN,
)

RESPOND_SHUTDOWN_TOOL = {  # 队友专属的关闭响应工具
    "name": "respond_shutdown",
    "description": (
        "回应已收到且通过 Harness 核验的关闭请求。approved 表示同意停止，rejected 表示拒绝并说明原因。"
        "批准响应发送成功后，Harness 会补齐本批工具结果并安全退出，不再执行工作工具。"
        "只能回复当前队友收到的请求，不能冒充其他成员；发送失败不代表已确认关闭。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "pattern": TEAM_REQUEST_ID_PATTERN.pattern,
                           "description": "已收到的关闭请求 request_id。"},
            "decision": {"type": "string", "enum": ["approved", "rejected"],
                         "description": "批准停止或拒绝停止。"},
            "reason": {"type": "string", "minLength": 1,
                       "maxLength": MAX_TEAM_MESSAGE_CONTENT_LENGTH,
                       "description": "关闭决定的理由，拒绝时说明仍需继续的原因。"},
        },
        "required": ["request_id", "decision", "reason"],
        "additionalProperties": False,
    },
}

SUBMIT_PLAN_TOOL = {  # 仅由队友 Runner 注入，不能加入 Lead 的工具列表
    "name": "submit_plan",
    "description": (
        "以当前队友身份向 Lead 提交工作计划，调用任何文件工具前必须提交并获得批准。"
        "计划不得超出当前工具能力；只读工作只能调查，隔离 Task 有文件写工具时可规划修改及可行的验证。"
        "计划应说明目标、调查范围和预期交付物；提交成功不等于获批。"
        "Harness 会等待审批，期间不要重复提交；被拒绝后根据理由修改再提交。"
        "提交新计划会使旧批准失效；不要在同一批次同时请求工作工具。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string", "minLength": 1,
                "maxLength": MAX_PROTOCOL_PAYLOAD_LENGTH,
                "description": "待审批的完整工作计划，说明范围、步骤、预期交付和验证限制，不能只包含空白。",
            },
        },
        "required": ["plan"],
        "additionalProperties": False,
    },
}

TOOLS = [  # 暴露给主模型的结构化工具 Schema
    {
        "name": "worktree_status",
        "description": (
            "Lead 只读查询已有工作树的目录、分支、文件修改、独有提交、绑定 Task 与清理阻碍。"
            "Task 完成后先查询再决定保留或请求移除；结果不是删除许可，移除时会重新核验。"
            "目录已移除或核验失败会返回错误，不能据此认为工作树干净。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {
                "type": "string", "minLength": 1, "maxLength": MAX_WORKTREE_NAME_LENGTH,
                "pattern": "^(?:" + WORKTREE_NAME_PATTERN.pattern + ")$",
                "description": "实际存在的工作树名称，不是目录路径或分支全名。",
            }},
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "worktree_keep",
        "description": (
            "Lead 核验 Task 绑定并持久记录本次保留工作树的决定。"
            "不修改工作文件、不提交、不合并，也不永久禁止后续移除。"
            "任务完成默认保留；需要明确留档时调用，不要反复记录相同决定。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {
                "type": "string", "pattern": TASK_ID_PATTERN.pattern,
                "description": "已绑定且工作树仍存在的 Task ID。",
            }},
            "required": ["task_id"], "additionalProperties": False,
        },
    },
    {
        "name": "worktree_remove",
        "description": (
            "Lead 显式请求移除 completed Task 的工作树目录和 Git 登记。"
            "仅在用户要求或授权清理时使用，不因任务完成而自动删除。"
            "strict 拒绝、ask 人工批准、trusted 允许；即使获准也须通过锁内检查。"
            "存在修改、未跟踪/忽略文件、相对源 HEAD 的独有提交或其他阻碍时拒绝；"
            "不强删、不自动合并、不删除 Git 分支，保留 Task 结果和历史绑定。"
            "失败可能已部分或全部移除，查看 removed 和诊断信息，勿盲目重试。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {
                "type": "string", "pattern": TASK_ID_PATTERN.pattern,
                "description": "要清理隔离目录的 completed Task ID，不能指定任意路径。",
            }},
            "required": ["task_id"], "additionalProperties": False,
        },
    },
    {
        "name": "worktree_create",
        "description": (
            "为一个未绑定工作树的 pending Task 创建独立 Git Worktree 和分支，并绑定该 Task。"
            "仅由 Lead 使用：先创建 Task，再调用本工具，成功后才授权队友认领；不要提前 task_claim。"
            "这是有副作用的操作：strict 拒绝，ask 需要人工批准，trusted 允许。"
            "从启动目录所属仓库的当前 HEAD 创建，不自动 git init、提交或复制未提交/未跟踪文件；"
            "管理目录位于 Git 工作树根目录下，可能在启动目录之外。"
            "已有目录或分支拒绝复用；失败可能留下部分成果，先核验错误中的目录、分支及 Task 状态，勿盲目重试。"
            "成功仅表示创建、核验和绑定完成；队友认领后须再次核验隔离目录并获得当前计划批准，才能写入文件。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string", "pattern": TASK_ID_PATTERN.pattern,
                    "description": "已存在且尚未被认领、尚未绑定 Worktree 的 pending Task ID。",
                },
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": MAX_WORKTREE_NAME_LENGTH,
                    "pattern": "^(?:" + WORKTREE_NAME_PATTERN.pattern + ")$",
                    "description": (
                        "新工作树名称，以英文字母或数字开头，仅含英文字母、数字、下划线和连字符；"
                        "不能是 Windows 保留设备名。不要填写路径或分支前缀。"
                    ),
                },
            },
            "required": ["task_id", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_shutdown",
        "description": (
            "以 Lead 身份请求活跃队友在安全边界停止。请求成功仅表示已发送，不代表队友同意或线程退出。"
            "队友可以拒绝；批准响应可从邮箱取得，stopped 表示 Runner 已停止执行而非任务完成。"
            "不会强制中断正在运行的模型或工具调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "minLength": 1,
                              "maxLength": MAX_TEAM_MEMBER_NAME_LENGTH,
                              "description": "要请求关闭的活跃队友名称。"},
                "reason": {"type": "string", "minLength": 1,
                           "maxLength": MAX_PROTOCOL_PAYLOAD_LENGTH,
                           "description": "请求停止的具体原因。"},
            },
            "required": ["recipient", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "review_plan",
        "description": (
            "以 Lead 身份回复队友的计划审批请求。根据邮箱中的请求 ID、计划与用户目标"
            "决定批准或拒绝；拒绝时说明需要怎样调整。普通 send_message 不构成审批。"
            "发送成功只表示响应已投递；队友消费并匹配后才恢复执行或修改计划。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string", "pattern": TEAM_REQUEST_ID_PATTERN.pattern,
                    "description": "收到的计划审批请求的准确 request_id，不是 message_id。",
                },
                "decision": {
                    "type": "string", "enum": ["approved", "rejected"],
                    "description": "approved 表示批准，rejected 表示拒绝并要求调整。",
                },
                "reason": {
                    "type": "string", "minLength": 1,
                    "maxLength": MAX_TEAM_MESSAGE_CONTENT_LENGTH,
                    "description": "审批理由；拒绝时给出可执行的修改要求。",
                },
            },
            "required": ["request_id", "decision", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "shell",
        "description": (
            "以当前工作目录作为初始目录，通过 Windows cmd.exe 执行命令。"
            "仅用于运行测试、构建、Git、包管理器，"
            "或其他专用工具无法完成的操作。"
            "耗时较长且可以独立运行的命令可以显式选择后台执行；"
            "Harness 不会根据命令文本自动改为后台执行。"
            "不要用 type、echo 重定向、copy、dir 等命令代替"
            " read_file、write_file、edit_file 或 glob。"
            "该工具始终被视为有副作用，可能需要用户批准。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "命令将由 Windows Command Prompt（cmd.exe）解释。"
                        "请使用 cmd.exe 语法，不要使用 Bash 专有语法或仅 Unix 环境提供的命令。"
                        ),
                    },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "是否将命令派发为后台 Job；省略时为 false。"
                        "只对耗时较长且可以独立运行的命令设为 true。"
                    ),
                },
                "timeout": {
                    "type": "number",
                    "minimum": MIN_JOB_TIMEOUT_SECONDS,
                    "maximum": MAX_JOB_TIMEOUT_SECONDS,
                    "description": (
                        "命令执行超时秒数；前台省略时使用 "
                        f"{DEFAULT_SHELL_TIMEOUT_SECONDS:g} 秒，"
                        "后台省略时使用 "
                        f"{DEFAULT_JOB_TIMEOUT_SECONDS:g} 秒。"
                    ),
                },
                },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "job_status",
        "description": (
            "按稳定 Job ID 查询当前 CLI 进程中一个后台 Job 的最新状态和结果。"
            "可能的状态包括 queued、running、succeeded、failed 和 cancelled。"
            "该工具不会等待 Job 完成，也不会修改 Job；"
            "后台 Job 尚未接入持久化，CLI 进程退出后不能再查询。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "pattern": JOB_ID_PATTERN.pattern,
                    "description": "后台派发结果返回的完整 Job ID。",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "job_cancel",
        "description": (
            "向当前 Harness 创建且仍未结束的后台 Job 发送取消请求。"
            "它不能终止任意系统进程，也不能通过 PID 操作进程。"
            "返回的 cancel_requested 只表示请求是否被接受；"
            "Worker 确认进程退出前，返回的最新状态仍可能是 queued 或 running。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "pattern": JOB_ID_PATTERN.pattern,
                    "description": "要取消的完整 Job ID。",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "schedule_cron",
        "description": (
            "创建一条由五段式 Cron 表达式决定触发时间的未来 Agent 计划。"
            "只在用户明确要求按时间自动执行工作时使用；"
            "普通当前步骤使用 todo_write，跨会话工作事项使用 Task，"
            "已经开始运行的耗时命令使用后台 Job。"
            "注册成功只表示计划已经保存，并不表示任务已经开始或执行成功。"
            "该工具会产生未来副作用，在 Ask 模式下需要用户批准。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cron": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_CRON_EXPRESSION_LENGTH,
                    "description": (
                        "五段式 Cron 表达式，依次为分钟、小时、日期、月份、星期。"
                        "支持 *、*/N、单值、N-M 和逗号列表；星期 0 表示星期日。"
                    ),
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_CRON_PROMPT_LENGTH,
                    "description": (
                        "触发后作为一条新用户请求交给 Agent 的完整、自包含任务。"
                        "不要依赖创建计划时的临时对话上下文。"
                    ),
                },
                "recurring": {
                    "type": "boolean",
                    "description": (
                        "是否在每次匹配时重复触发；省略时为 true。"
                        "设为 false 时，在第一次匹配并登记触发后自动删除计划。"
                    ),
                },
                "durable": {
                    "type": "boolean",
                    "description": (
                        "是否将计划定义持久化；省略时为 true。"
                        "false 表示仅在当前 CLI 进程中有效，退出后丢失。"
                    ),
                },
                "timezone": {
                    "type": "string",
                    "enum": list(VALID_CRON_TIMEZONES),
                    "description": (
                        "解释 Cron 字段的时区；省略时为 local。"
                        "local 使用运行 CLI 的系统本地时区，UTC 使用协调世界时。"
                    ),
                },
            },
            "required": ["cron", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_crons",
        "description": (
            "列出当前工作区的 durable 计划与当前 CLI 的 session-only 计划。"
            "返回 Cron 表达式、时区、生命周期字段、最近触发槽以及限长 Prompt 预览。"
            "它只读取计划定义，不表示计划当前正在执行，也不列出历史执行结果。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_cron",
        "description": (
            "取消一条 CronJob，阻止它在未来继续产生新触发。"
            "已经进入内存队列或已经开始执行的触发不会被追溯撤回。"
            "应先通过 schedule_cron 返回值或 list_crons 获取真实的完整 ID。"
            "该工具会删除计划状态，在 Ask 模式下需要用户批准。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cron_job_id": {
                    "type": "string",
                    "pattern": CRON_ID_PATTERN.pattern,
                    "description": "要取消的完整 CronJob ID。",
                },
            },
            "required": ["cron_job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "读取一个已知文本文件的内容。"
            "查看源码、配置或文档时应优先使用此工具，"
            "不要使用 shell 的 type 命令代替。"
            "文件较长或已经知道目标行号时，应通过 start_line 和 limit 读取局部窗口，"
            "避免将无关内容载入上下文。"
            "返回结果始终包含覆盖整个文件的版本令牌；"
            "后续调用 edit_file 时必须原样传入该版本。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径。",
                    },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "开始读取的行号，从 1 开始计数；省略时从第 1 行开始。",
                    },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "从 start_line 开始最多读取的行数，必须为正整数；省略时读取到文件末尾。",
                    },
                },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "创建文本文件或整体覆盖已有文件。"
            "父目录不存在时会自动创建。"
            "如果只需要修改已有文件的一部分，请使用 edit_file。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径。",
                    },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容。",
                    },
                },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "对已有文本文件进行一次精确的局部替换。"
            "将第一个匹配的 old_text 替换为 new_text；"
            "找不到 old_text 时不会修改文件。"
            "调用前必须先使用 read_file 获取完整文件版本，并通过 expected_version 原样传入；"
            "如果文件在读取后发生变化，本工具会拒绝过期编辑并要求重新读取。"
            "局部修改应优先使用此工具，而不是 write_file 或 shell。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径。",
                    },
                "old_text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "要被精确匹配并替换的非空文本。",
                    },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本。",
                    },
                "expected_version": {
                    "type": "string",
                    "pattern": FILE_VERSION_PATTERN.pattern,
                    "description": "最近一次 read_file 返回的完整文件版本令牌，必须原样传入。",
                    },
                },
            "required": ["path", "old_text", "new_text", "expected_version"],
        },
    },
    {
        "name": "glob",
        "description": (
            "使用通配符查找当前工作目录内的文件和目录。"
            "不知道具体文件路径时应先使用此工具。"
            "支持 *、? 和 ** 等模式；该工具只返回路径，不读取文件内容。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "相对于当前工作目录的匹配模式，支持 *、? 和 **，不能越出工作目录。",
                    },
                },
            "required": ["pattern"],
        },
    },
    {
        "name": "todo_write",
        "description": (
            "创建或更新当前这一次用户任务的待办事项列表。"
            "当任务包含多个可独立跟踪的步骤时使用；简单的一步任务无需使用。"
            "每次调用都必须传入更新后的完整列表，新列表会整体替换旧列表。"
            "可以移除不再需要的 pending 项，但不能直接删除 in_progress 项。"
            "新事项不能直接设为 completed，completed 状态不能回退。"
            "开始处理某一项前，将其状态设为 in_progress；完成后，将其状态设为 completed。"
            "任何时刻最多只能有一项处于 in_progress 状态。"
            "该工具只记录和展示计划，不会执行待办事项，也不会修改文件。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": (
                        "要保存的完整待办事项列表。"
                        "每次调用都会整体替换当前列表；"
                        "请传入更新后仍需保留的全部事项。"
                    ),
                    "minItems": 1,
                    "maxItems": MAX_TODOS,
                    "items": {
                        "type": "object",
                        "description": "一个可独立跟踪的待办事项。",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": (
                                    "具体、可执行且可验证的单个步骤描述；"
                                    "去除首尾空白后不能为空。"
                                    "创建后应尽量保持内容不变，"
                                    "系统使用该内容识别同一个待办事项。"
                                ),
                                "minLength": 1,
                                "maxLength": MAX_TODO_CONTENT_LENGTH,
                            },
                            "status": {
                                "type": "string",
                                "description": (
                                    "待办事项的当前状态：pending 表示尚未开始，"
                                    "in_progress 表示正在处理，completed 表示已经完成。"
                                ),
                                "enum": list(VALID_STATUS),
                            },
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_create",
        "description": (
            "创建一条跨会话持久化的 pending Task。"
            "仅用于需要恢复、依赖追踪或后续协作的工作事项；"
            "当前会话中的普通执行步骤应使用 todo_write。"
            "blocked_by 中的每个 Task ID 都必须已经存在。"
            "该工具只记录任务事实，不会执行任务描述中的实际工作。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TASK_SUBJECT_LENGTH,
                    "description": "简短、明确且适合任务列表展示的标题，不能只包含空白。",
                },
                "description": {
                    "type": "string",
                    "maxLength": MAX_TASK_DESCRIPTION_LENGTH,
                    "description": "跨会话恢复任务所需的完整目标、范围、约束和完成条件。",
                },
                "blocked_by": {
                    "type": "array",
                    "maxItems": MAX_TASK_DEPENDENCIES,
                    "uniqueItems": True,
                    "description": "必须先完成的已有上游 Task ID；没有依赖时省略或传入空数组。",
                    "items": {
                        "type": "string",
                        "pattern": TASK_ID_PATTERN.pattern,
                    },
                },
            },
            "required": ["subject"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_list",
        "description": (
            "列出当前工作区全部持久 Task 的轻量摘要，"
            "包括 ID、标题、状态、owner 和直接依赖。"
            "恢复任务、选择可执行任务或准备声明依赖时应先调用；"
            "如需描述和结果正文，再使用 task_get。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_get",
        "description": (
            "按稳定 ID 获取一条持久 Task 的完整记录，"
            "包括描述、状态、owner、依赖和完成结果。"
            "跨会话继续任务或执行前需要完整约束时使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "pattern": TASK_ID_PATTERN.pattern,
                    "description": "由 task_create 或 task_list 返回的完整 Task ID。",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_update",
        "description": (
            "修改一条 pending Task 的标题、描述或直接依赖。"
            "至少提供 subject、description、blocked_by 中的一项；"
            "传入空 blocked_by 数组表示清除全部依赖。"
            "不能通过此工具修改状态、owner 或完成结果，也不能更新已经认领或完成的 Task。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "pattern": TASK_ID_PATTERN.pattern,
                    "description": "要更新的完整 Task ID。",
                },
                "subject": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TASK_SUBJECT_LENGTH,
                    "description": "更新后的简短标题；省略表示保持不变。",
                },
                "description": {
                    "type": "string",
                    "maxLength": MAX_TASK_DESCRIPTION_LENGTH,
                    "description": "更新后的完整描述；省略表示保持不变。",
                },
                "blocked_by": {
                    "type": "array",
                    "maxItems": MAX_TASK_DEPENDENCIES,
                    "uniqueItems": True,
                    "description": "更新后的完整直接依赖列表；省略表示保持不变。",
                    "items": {
                        "type": "string",
                        "pattern": TASK_ID_PATTERN.pattern,
                    },
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_claim",
        "description": (
            "以当前主 Agent 的 Harness 身份认领一条可开始的 pending Task，"
            "并将其状态原子更新为 in_progress。"
            "只有所有 blocked_by 依赖均已完成时才能成功。"
            "认领只记录责任归属，不会自动执行任务内容。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "pattern": TASK_ID_PATTERN.pattern,
                    "description": "要认领的完整 Task ID。",
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_complete",
        "description": (
            "在真实工作已经完成并核验后，将当前主 Agent 认领的 Task 标记为 completed。"
            "result 必须忠实记录实际产出、验证结果或可恢复结论；"
            "不得仅因为计划执行就提前完成。"
            "成功后会返回刚刚解除阻塞的直接下游 Task ID。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "pattern": TASK_ID_PATTERN.pattern,
                    "description": "要完成的完整 Task ID。",
                },
                "result": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TASK_RESULT_LENGTH,
                    "description": "真实完成结果和验证证据的简洁摘要，不能只包含空白。",
                },
            },
            "required": ["task_id", "result"],
            "additionalProperties": False,
        },
    },
    {
        "name": "spawn_teammate",
        "description": (
            "在后台线程中创建一个具有独立上下文、角色和收件箱的 Teammate。"
            "适合将需要独立上下文的调查或已绑定 Worktree 的持久 Task 交给后台队友；"
            "初始指令、普通消息及未绑定 Task 保持只读；已核验隔离 Task 在计划获批后可写文件，但不能执行 Shell。"
            "一次性的同步代码探索仍优先使用 delegate。"
            "队友会先通过 submit_plan 提交计划；Lead 收到请求后应使用 review_plan 回复。"
            "获批后才能调用本项工作的文件工具；不能执行 Shell 或继续创建队友。"
            "返回成功只表示线程已经启动，不表示任务完成；最终结果会由 Harness 投递到 Lead 邮箱。"
            "完成后成员进入有界空闲等待，可用 send_message 派发下一项工作；无需重复创建。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TEAM_MEMBER_NAME_LENGTH,
                    "pattern": TEAM_MEMBER_NAME_PATTERN.pattern,
                    "description": (
                        "本次 CLI 进程内唯一的 Teammate 名称。"
                        "必须使用以英文字母开头的小写 kebab-case；"
                        "不能使用 lead 或 Windows 保留设备名称。"
                    ),
                },
                "role": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TEAMMATE_ROLE_LENGTH,
                    "description": "该 Teammate 的明确职责和工作边界，不能只包含空白。",
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TEAMMATE_PROMPT_LENGTH,
                    "description": (
                        "交给 Teammate 的完整、自包含初始只读任务；修改任务通过已绑定 Worktree 的 task_ids 授权。"
                        "它看不到 Lead 的对话历史，因此必须写明目标、范围、约束和期望证据。"
                    ),
                },
                "task_ids": {
                    "type": ["array", "null"],
                    "maxItems": MAX_TASKS,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
                    "description": (
                        "允许该成员认领的 Task ID 白名单；需要文件修改的 Task 须先创建并绑定 Worktree。"
                        "省略、null 或空列表均不授权自动认领；多个成员可以共享候选范围。"
                        "完成初始工作后，空闲时尝试认领范围内依赖已满足的任务；"
                        "认领后仍须审批，不表示已执行。完成状态先保存，再发送结果通知。"
                    ),
                },
            },
            "required": ["name", "role", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_message",
        "description": (
            "以 Lead 身份向一个 Teammate 的文件收件箱提交下一项完整、自包含的工作指令。"
            "普通消息统一排队，不会中途改变当前已获批任务；空闲时唤醒成员重新提交计划。"
            "新工作使用独立上下文，不应依赖队友记得上一项工作的细节。审批和关闭请使用专用协议工具。"
            "发送方身份由 Harness 固定，模型不能伪造。"
            "提交成功只表示消息已经写入邮箱，不表示接收方已经读取或执行；"
            "Harness 会拒绝不存在或已经结束运行的接收方。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TEAM_MEMBER_NAME_LENGTH,
                    "pattern": TEAM_MEMBER_NAME_PATTERN.pattern,
                    "description": "由 spawn_teammate 创建并返回的准确 Teammate 名称。",
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_TEAM_MESSAGE_CONTENT_LENGTH,
                    "description": "需要补充给该 Teammate 的完整消息正文，不能只包含空白。",
                },
            },
            "required": ["recipient", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_inbox",
        "description": (
            "原子消费 Lead 当前收件箱中的全部团队消息，再逐条进行协议路由。"
            "结果分为 messages、resolvedResponses 和 errors，各组保留原有顺序。"
            "有效响应会更新请求状态；待处理请求和错误都不代表批准，关闭批准也不代表线程已退出。"
            "Teammate 的最终结论会由 Harness 自动投递到这里。"
            "空结果可能只表示队友仍在执行；不要在没有其他工作的情况下连续忙轮询。"
            "该工具只能访问 Lead 邮箱，不能读取其他成员的私有收件箱。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
      "name": "delegate",
      "description": (
          "启动一个具有独立上下文的只读子 Agent，"
          "用于跨多个文件进行代码探索、调用链追踪或结构分析。"
          "子 Agent 只能读取和查找文件，不能修改文件或执行命令，"
          "最终只返回简洁结论及必要的证据路径。"
          "任务描述必须完整且自包含；简单的一次读取无需使用此工具。"
      ),
      "input_schema": {
          "type": "object",
          "properties": {
              "description": {
                  "type": "string",
                  "minLength": 1,
                  "description": (
                      "交给子 Agent 的完整只读探索任务。"
                      "应说明目标、探索范围、需要回答的问题以及期望返回的证据；"
                      "不要引用只有父对话中才存在的信息。"
                  ),
              },
          },
          "required": ["description"],
          "additionalProperties": False,
      },
    },
    {
        "name": "load_skill",
        "description": (
            "按名称加载一个可用 Skill 的完整说明。"
            "仅当用户明确要求某项 Skill，或当前任务与系统提示中某项 Skill 的简介明确匹配时使用。"
            "返回完整的 SKILL.md 内容；不要猜测不存在的名称，也不要重复加载当前上下文中已经存在的同一 Skill。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "description": (
                        "要加载的 Skill 名称，必须与系统提示中“可用 Skill 目录”列出的名称完全一致。"
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compact",
        "description": (
            "将较早的会话历史总结为一条紧凑摘要，以释放上下文空间。"
            "仅在会话已经很长、旧信息影响继续工作，或用户明确要求压缩时使用；"
            "短对话无需调用，也不要连续重复调用。"
            "压缩前会先保存脱敏 Transcript，成功后当前任务将依据摘要继续。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "maxLength": MAX_COMPACT_FOCUS_CHARS,
                    "description": (
                        "可选的摘要重点，例如“保留尚未完成的修改和用户约束”；"
                        "省略时按默认结构保留目标、证据、变更、约束和剩余工作。"
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
]
