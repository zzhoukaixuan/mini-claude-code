"""Memory 存储模块：定义记忆数据结构，并负责 Frontmatter、注册表、原子文件写入与通用校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from ..config import (
    MAX_MEMORY_BODY_LENGTH,
    MAX_MEMORY_CANDIDATES_PER_TURN,
    MAX_MEMORY_DESCRIPTION_LENGTH,
    MAX_MEMORY_EVIDENCE_LENGTH,
    MAX_MEMORY_FILE_BYTES,
    MAX_MEMORY_FILES,
    MAX_MEMORY_NAME_LENGTH,
    MEMORY_DIR,
    MEMORY_INDEX,
    MEMORY_NAME_PATTERN,
    MEMORY_SECRET_PATTERNS,
    VALID_MEMORY_SCOPES,
    VALID_MEMORY_TYPES,
    WORKDIR,
    MemoryAdmissionBehavior,
    MemoryPersistenceStatus,
    MemoryScope,
    MemoryType,
)
from ..storage.atomic import atomic_replace_text


@dataclass(frozen=True)
class MemoryMetadata:
    name: str  # Memory 注册表中的稳定名称
    description: str  # 用于召回判断的简短记忆描述
    type: MemoryType  # 记忆所属的语义类别

def parse_memory_frontmatter(text: str) -> tuple[MemoryMetadata, str]:
    """解析并校验 Memory 文件的 YAML frontmatter 与 Markdown 正文。"""
    if not isinstance(text, str):
        raise ValueError("Memory 文件必须是字符串类型")
    file_size = len(text.encode("utf-8"))
    if file_size > MAX_MEMORY_FILE_BYTES:
        raise ValueError(f"Memory 文件大小为 {file_size} 字节，超过上限 {MAX_MEMORY_FILE_BYTES} 字节")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Memory 文件必须以独立的 '---' 行开始")
    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            closing_index = index
            break
    if closing_index is None:
        raise ValueError("Memory 文件缺少结束 frontmatter 的 '---' 行")
    frontmatter_text = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1:]).strip()
    try:
        metadata_raw = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise ValueError(f"Memory frontmatter 不是合法 YAML：{e}") from e
    if not isinstance(metadata_raw, dict):
        raise ValueError("Memory frontmatter 必须是 YAML 映射")

    normalized_metadata: dict[str, str] = {}
    for field_name in ("name", "description", "type"):
        value = metadata_raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Memory frontmatter 字段 {field_name!r} 必须是非空字符串")
        normalized_metadata[field_name] = value.strip()
    name = normalized_metadata["name"]
    description = normalized_metadata["description"]
    memory_type = normalized_metadata["type"]
    if len(name) > MAX_MEMORY_NAME_LENGTH:
        raise ValueError(f"Memory 名称长度不能超过 {MAX_MEMORY_NAME_LENGTH} 个字符")
    if MEMORY_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"Memory 名称 {name!r} 必须使用小写 kebab-case")
    if "\n" in description or "\r" in description:
        raise ValueError("Memory description 必须是单行文本")
    if len(description) > MAX_MEMORY_DESCRIPTION_LENGTH:
        raise ValueError(f"Memory 描述长度不能超过 {MAX_MEMORY_DESCRIPTION_LENGTH} 个字符")
    if memory_type not in VALID_MEMORY_TYPES:
        allowed_types = ", ".join(VALID_MEMORY_TYPES)
        raise ValueError(f"未知的 Memory 类型 {memory_type!r}，允许的类型为：{allowed_types}")
    if not body:
        raise ValueError("Memory 正文不能为空")
    if len(body) > MAX_MEMORY_BODY_LENGTH:
        raise ValueError(f"Memory 正文长度不能超过 {MAX_MEMORY_BODY_LENGTH} 个字符")
    return MemoryMetadata(name, description, memory_type), body

@dataclass(frozen=True)
class MemoryDefinition:
    metadata: MemoryMetadata  # 从 YAML Frontmatter 解析出的记忆元数据
    body: str  # 去除 Frontmatter 后的记忆正文
    raw_content: str  # Memory 文件未经拆分的完整文本
    file_path: Path  # Memory 文件的规范化路径

def memory_file_path(name: str, memory_dir: Path | None = None) -> Path:
    """根据 Memory 名称生成经过工作区边界检查的文件路径。"""
    if not isinstance(name, str):
        raise ValueError("Memory 名称必须是字符串类型")
    if len(name) > MAX_MEMORY_NAME_LENGTH:
        raise ValueError(f"Memory 名称长度不能超过 {MAX_MEMORY_NAME_LENGTH} 个字符")
    if MEMORY_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"Memory 名称 {name!r} 必须使用小写 kebab-case")
    directory = MEMORY_DIR if memory_dir is None else memory_dir
    if not isinstance(directory, Path):
        raise ValueError("Memory 目录必须是 pathlib.Path 类型")
    memory_root = directory.resolve()
    if not memory_root.is_relative_to(WORKDIR):
        raise ValueError(f"Memory 目录 {memory_root} 不在工作目录 {WORKDIR} 内")
    unresolved_path = memory_root / f"memory-{name}.md"
    resolved_path = unresolved_path.resolve()
    if not resolved_path.is_relative_to(memory_root):
        raise ValueError(f"Memory 文件路径 {resolved_path} 不在 Memory 目录 {memory_root} 内")
    return resolved_path

def scan_memories(memory_dir: Path = MEMORY_DIR) -> dict[str, MemoryDefinition]:
    """扫描并校验 Memory 文件，完整构建记忆注册表。"""
    local_registry: dict[str, MemoryDefinition] = {}
    if not isinstance(memory_dir, Path):
        raise ValueError("Memory 目录必须是 pathlib.Path 类型")
    memory_root = memory_dir.resolve()
    if not memory_root.is_relative_to(WORKDIR):
        raise ValueError(f"Memory 目录 {memory_root} 不在工作目录 {WORKDIR} 内")
    if not memory_root.exists():
        return local_registry
    if not memory_root.is_dir():
        raise ValueError(f"Memory 路径 {memory_root} 不是目录")
    try:
        directory_entries = sorted(memory_root.iterdir(), key=lambda path: (path.name.casefold(), path.name))
    except OSError as e:
        raise ValueError(f"无法扫描 Memory 目录 {memory_root}：{type(e).__name__}：{e}") from e
    memory_files: list[tuple[Path, Path]] = []
    for entry in directory_entries:
        if entry.name.casefold() == MEMORY_INDEX.name.casefold():
            continue
        if entry.suffix.casefold() != ".md":
            continue
        resolved_entry = entry.resolve()
        if not resolved_entry.is_relative_to(memory_root):
            raise ValueError(f"Memory 文件 {entry} 的实际路径 {resolved_entry} 不在 Memory 目录内")
        if not resolved_entry.is_file():
            continue
        memory_files.append((entry, resolved_entry))
    if len(memory_files) > MAX_MEMORY_FILES:
        raise ValueError(f"Memory 文件数量为 {len(memory_files)}，超过上限 {MAX_MEMORY_FILES}")
    for source_entry, resolved_path in memory_files:
        try:
            file_size = resolved_path.stat().st_size
            if file_size > MAX_MEMORY_FILE_BYTES:
                raise ValueError(f"Memory 文件 {source_entry} 大小为 {file_size} 字节，超过上限 {MAX_MEMORY_FILE_BYTES} 字节")
            with open(resolved_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
        except (OSError, UnicodeError) as e:
            raise ValueError(f"无法读取 Memory 文件 {source_entry}：{type(e).__name__}：{e}") from e
        try:
            metadata, body = parse_memory_frontmatter(raw_content)
        except ValueError as e:
            raise ValueError(f"解析 Memory 文件失败 {source_entry}：{e}") from e
        expected_filename = f"memory-{metadata.name}.md"
        if source_entry.name != expected_filename:
            raise ValueError(f"Memory 文件名 {source_entry.name!r} 与元数据名称 {metadata.name!r} 不匹配；预期文件名为 {expected_filename!r}")
        existing_memory = local_registry.get(metadata.name)
        if existing_memory is not None:
            raise ValueError(f"存在重复的 Memory 名称 {metadata.name!r}：{existing_memory.file_path} 与 {resolved_path}")
        local_registry[metadata.name] = MemoryDefinition(metadata=metadata, body=body, raw_content=raw_content, file_path=resolved_path)
    return local_registry

def serialize_memory(metadata: MemoryMetadata, body: str) -> str:
    """将 Memory 元数据与正文序列化为规范的 Markdown 文档。"""
    if not isinstance(metadata, MemoryMetadata):
        raise ValueError("Memory 元数据必须是 MemoryMetadata 类型")
    if not isinstance(body, str):
        raise ValueError("Memory 正文必须是字符串类型")
    normalized_body = body.strip()
    if not normalized_body:
        raise ValueError("Memory 正文不能为空")
    try:
        frontmatter = yaml.safe_dump(
            {
                "name": metadata.name,
                "description": metadata.description,
                "type": metadata.type,
            },
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
    except yaml.YAMLError as e:
        raise ValueError(f"无法序列化 Memory frontmatter：{e}") from e
    content = f"---\n{frontmatter}\n---\n\n{normalized_body}\n"
    # 直接构造 dataclass 不会自动执行字段校验，因此在写入前复用解析器验证最终文档。
    parsed_metadata, parsed_body = parse_memory_frontmatter(content)
    if parsed_metadata != metadata:
        raise ValueError("Memory 元数据在序列化后发生了规范化变化")
    if parsed_body != normalized_body:
        raise ValueError("Memory 正文在序列化后发生了意外变化")
    return content

def rebuild_memory_index(memory_dir: Path = MEMORY_DIR) -> Path:
    """根据当前有效的 Memory 文件原子重建派生索引。"""
    registry = scan_memories(memory_dir)
    index_lines = []
    for name in sorted(registry, key=lambda value: (value.casefold(), value)):
        memory = registry[name]
        index_lines.append(f"- [{memory.metadata.name}]({memory.file_path.name}) — {memory.metadata.description}")

    index_content = "\n".join(index_lines)
    if index_content:
        index_content += "\n"

    index_path = memory_dir.resolve() / MEMORY_INDEX.name
    atomic_replace_text(index_path, index_content, allowed_root=memory_dir)
    return index_path

def write_memory_file(name: str, memory_type: MemoryType, description: str, body: str, memory_dir: Path = MEMORY_DIR) -> MemoryDefinition:
    """校验并原子写入一条新 Memory，随后重建索引。"""
    metadata = MemoryMetadata(name=name, description=description, type=memory_type)
    raw_content = serialize_memory(metadata, body)
    target_path = memory_file_path(name, memory_dir)
    registry = scan_memories(memory_dir)
    if name in registry or target_path.exists():
        raise ValueError(f"Memory 名称 {name!r} 已存在，不允许静默覆盖")
    if len(registry) >= MAX_MEMORY_FILES:
        raise ValueError(f"Memory 文件数量已达到上限 {MAX_MEMORY_FILES}，无法继续新增")
    atomic_replace_text(
        target_path,
        raw_content,
        allowed_root=memory_dir,
        replace_existing=False,
    )
    rebuild_memory_index(memory_dir)
    return MemoryDefinition(
        metadata=metadata,
        body=body.strip(),
        raw_content=raw_content,
        file_path=target_path,
    )

def render_memory_list(memory_dir: Path = MEMORY_DIR) -> str:
    """按稳定顺序渲染可供用户查看的 Memory 摘要列表。"""
    registry = scan_memories(memory_dir)
    if not registry:
        return "当前没有已保存的 Memory。"
    lines = []
    for name in sorted(registry, key=lambda value: (value.casefold(), value)):
        memory = registry[name]
        lines.append(
            f"- {memory.metadata.name} "
            f"[{memory.metadata.type}] — "
            f"{memory.metadata.description}"
        )
    return "\n".join(lines)

def delete_memory_file(name: str, memory_dir: Path = MEMORY_DIR) -> Path:
    """根据已注册的稳定名称删除一条 Memory，并重建派生索引。"""
    target_path = memory_file_path(name, memory_dir)
    registry = scan_memories(memory_dir)
    memory = registry.get(name)
    if memory is None:
        raise ValueError(f"Memory {name!r} 不存在")
    if memory.file_path != target_path:
        raise ValueError(f"Memory {name!r} 的注册路径与预期路径不一致，拒绝删除")
    try:
        target_path.unlink()
    except OSError as error:
        raise ValueError(f"无法删除 Memory {name!r}：{type(error).__name__}：{error}") from error
    try:
        rebuild_memory_index(memory_dir)
    except Exception as error:
        raise RuntimeError(f"Memory {name!r} 文件已经删除，但索引重建失败：{type(error).__name__}：{error}") from error
    return target_path

@dataclass(frozen=True)
class MemoryCandidate:
    metadata: MemoryMetadata  # 候选记忆的名称、描述和类别
    body: str  # 准备接受准入检查的候选记忆正文
    evidence: str  # 支持持久化该候选记忆的当前对话证据
    scope: MemoryScope  # 候选记忆仅限当前任务还是允许持久化

@dataclass(frozen=True)
class MemoryAdmissionDecision:
    behavior: MemoryAdmissionBehavior  # 准入策略决定接受还是拒绝候选记忆
    reason: str  # 准入决定的可读原因
    rule_id: str  # 命中的稳定准入规则标识

    def __post_init__(self) -> None:
        if self.behavior not in ("accept", "reject"):
            raise ValueError(f"未知的 Memory 准入结果：{self.behavior!r}")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Memory 准入原因必须是非空字符串")
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("Memory 准入规则 ID 必须是非空字符串")

@dataclass(frozen=True)
class MemoryPersistenceResult:
    name: str  # 尝试持久化的候选记忆名称
    status: MemoryPersistenceStatus  # 写入、拒绝或失败的最终状态
    reason: str  # 持久化结果的可读说明
    rule_id: str  # 产生该结果的稳定规则标识
    file_path: Path | None = None  # 成功写入后的 Memory 文件路径

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Memory 持久化结果名称必须是非空字符串")
        if self.status not in ("written", "rejected", "failed"):
            raise ValueError(f"未知的 Memory 持久化状态：{self.status!r}")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Memory 持久化结果原因必须是非空字符串")
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("Memory 持久化结果规则 ID 必须是非空字符串")
        if self.status == "written" and not isinstance(self.file_path, Path):
            raise ValueError("写入成功的 Memory 持久化结果必须包含文件路径")
        if self.status != "written" and self.file_path is not None:
            raise ValueError("未写入的 Memory 持久化结果不能包含文件路径")

@dataclass(frozen=True)
class MemoryConsolidationItem:
    metadata: MemoryMetadata  # 整理后目标记忆的元数据
    body: str  # 整理后目标记忆的正文
    source_names: tuple[str, ...]  # 生成该目标记忆所依据的原始 Memory 名称

@dataclass(frozen=True)
class MemoryConsolidationRemoval:
    name: str  # 整理计划建议移除的原始 Memory 名称
    reason: str  # 建议移除该 Memory 的原因

@dataclass(frozen=True)
class MemoryConsolidationPlan:
    items: tuple[MemoryConsolidationItem, ...]  # 待写入或更新的整理后记忆
    removals: tuple[MemoryConsolidationRemoval, ...]  # 待删除的冗余原始记忆

@dataclass(frozen=True)
class MemoryConsolidationCommitResult:
    before_count: int  # 提交整理计划前的 Memory 文件数量
    after_count: int  # 提交整理计划后的 Memory 文件数量
    backup_dir: Path  # 本次整理提交前创建的备份目录

@dataclass
class MemoryCLIState:
    enabled: bool = True  # 当前 CLI 会话是否启用 Memory 召回与持久化
    pending_plan: MemoryConsolidationPlan | None = None  # 等待用户审核的整理计划
    pending_registry: dict[str, MemoryDefinition] | None = None  # 生成待审核计划时使用的 Memory 快照
    turns_since_consolidation_check: int = 0  # 距离上次自动整理检查的有效 Agent 回合数
    last_consolidation_attempt_at: float | None = None  # 最近一次真实整理尝试的单调时钟时间

def parse_memory_candidates(text: str) -> list[MemoryCandidate]:
    """严格解析 Memory 提取模型返回的候选列表，不执行持久化。"""
    if not isinstance(text, str):
        raise ValueError("Memory 候选结果必须是字符串")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Memory 候选结果不是合法 JSON：{e}") from e
    if not isinstance(payload, dict):
        raise ValueError("Memory 候选结果顶层必须是 JSON 对象")
    if set(payload) != {"candidates"}:
        raise ValueError("Memory 候选结果顶层只能包含 'candidates' 字段")
    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, list):
        raise ValueError("Memory 候选结果的 'candidates' 必须是列表")
    if len(raw_candidates) > MAX_MEMORY_CANDIDATES_PER_TURN:
        raise ValueError(f"单轮 Memory 候选数量不能超过 {MAX_MEMORY_CANDIDATES_PER_TURN}")
    field_names = ("name", "type", "description", "body", "evidence", "scope")
    required_fields = set(field_names)
    candidates: list[MemoryCandidate] = []
    seen_names: set[str] = set()
    for candidate_index, raw_candidate in enumerate(raw_candidates):
        if not isinstance(raw_candidate, dict):
            raise ValueError(f"第 {candidate_index} 条 Memory 候选必须是 JSON 对象")
        actual_fields = set(raw_candidate)
        if actual_fields != required_fields:
            missing_fields = sorted(required_fields - actual_fields)
            extra_fields = sorted(actual_fields - required_fields)
            details = []
            if missing_fields:
                details.append(f"缺少字段：{', '.join(missing_fields)}")
            if extra_fields:
                details.append(f"多余字段：{', '.join(extra_fields)}")
            raise ValueError(
                f"第 {candidate_index} 条 Memory 候选字段无效；"
                + "；".join(details)
            )
        normalized_fields: dict[str, str] = {}
        for field_name in field_names:
            value = raw_candidate[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"第 {candidate_index} 条 Memory 候选的字段 {field_name!r} 必须是非空字符串")
            normalized_fields[field_name] = value.strip()
        name = normalized_fields["name"]
        memory_type = normalized_fields["type"]
        description = normalized_fields["description"]
        body = normalized_fields["body"]
        evidence = " ".join(normalized_fields["evidence"].split())
        scope = normalized_fields["scope"]
        if memory_type not in VALID_MEMORY_TYPES:
            allowed_types = ", ".join(VALID_MEMORY_TYPES)
            raise ValueError(f"第 {candidate_index} 条 Memory 候选类型 {memory_type!r} 无效；允许的类型为：{allowed_types}")
        if scope not in VALID_MEMORY_SCOPES:
            allowed_scopes = ", ".join(VALID_MEMORY_SCOPES)
            raise ValueError(f"第 {candidate_index} 条 Memory 候选范围 {scope!r} 无效；允许的范围为：{allowed_scopes}")
        if len(evidence) > MAX_MEMORY_EVIDENCE_LENGTH:
            raise ValueError(f"第 {candidate_index} 条 Memory 候选证据长度不能超过 {MAX_MEMORY_EVIDENCE_LENGTH} 个字符")
        if name in seen_names:
            raise ValueError(f"同一批结果中存在重复的 Memory 候选名称 {name!r}")
        metadata = MemoryMetadata(name=name, description=description, type=cast(MemoryType, memory_type))
        # 复用最终文件序列化器校验名称、描述、类型、正文和 UTF-8 大小，但不写文件。
        serialize_memory(metadata, body)
        seen_names.add(name)
        candidates.append(MemoryCandidate(metadata=metadata,body=body,evidence=evidence,scope=cast(MemoryScope, scope)))
    return candidates

def render_memory_selection_catalog(registry: dict[str, MemoryDefinition]) -> tuple[str, list[str]]:
    """渲染只包含名称与描述的 Memory 选择目录及其稳定索引。"""
    if not isinstance(registry, dict):
        raise ValueError("Memory 注册表必须是字典")
    if len(registry) > MAX_MEMORY_FILES:
        raise ValueError(f"Memory 注册表数量不能超过 {MAX_MEMORY_FILES}")
    if any(not isinstance(name, str) for name in registry):
        raise ValueError("Memory 注册表中的每个键都必须是字符串")
    ordered_names = sorted(registry, key=lambda value: (value.casefold(), value))
    catalog_lines = []
    for index, name in enumerate(ordered_names):
        memory = registry[name]
        if not isinstance(memory, MemoryDefinition):
            raise ValueError(f"Memory 注册表条目 {name!r} 类型无效")
        if name != memory.metadata.name:
            raise ValueError(f"Memory 注册表键 {name!r} 与元数据名称 {memory.metadata.name!r} 不一致")
        catalog_lines.append(f"{index}: {name} — {memory.metadata.description}")
    return "\n".join(catalog_lines), ordered_names

def normalize_memory_match_text(text: str) -> str:
    """统一大小写与空白，供证据和重复描述执行确定性匹配。"""
    if not isinstance(text, str):
        raise ValueError("Memory 匹配文本必须是字符串")
    return " ".join(text.casefold().split())

def contains_memory_secret(text: str, known_secrets: tuple[str, ...] = ()) -> bool:
    """判断候选文本是否包含已知秘密值或常见秘密格式。"""
    from mini_claude_code.mcp_secrets import active_mcp_secrets
    if not isinstance(text, str):
        raise ValueError("Memory 秘密检查文本必须是字符串")
    if not isinstance(known_secrets, tuple):
        raise ValueError("Memory 已知秘密集合必须是元组")
    for secret in known_secrets + active_mcp_secrets():
        if not isinstance(secret, str):
            raise ValueError("Memory 已知秘密集合中的每一项都必须是字符串")
        if secret and secret in text:
            return True
    return any(pattern.search(text) is not None for pattern in MEMORY_SECRET_PATTERNS)
