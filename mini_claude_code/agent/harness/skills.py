"""Skill 模块：解析 SKILL.md、扫描安全注册表、渲染目录并按名称渐进式加载正文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import (
    MAX_SKILL_DESCRIPTION_LENGTH,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_LENGTH,
    MAX_SKILLS,
    SKILL_NAME_PATTERN,
    SKILLS_DIR,
    WORKDIR,
)


_SKILL_REGISTRY: dict[str, "SkillDefinition"] = {}

@dataclass(frozen=True)
class SkillMetadata:
    name: str  # Skill 注册表中的稳定名称
    description: str  # 用于渐进式披露和选择 Skill 的简短描述

def parse_skill_frontmatter(text: str) -> tuple[SkillMetadata, str]:
    """解析 Skill 文档的 YAML Frontmatter，返回元数据和正文。"""
    if not isinstance(text, str):
        raise ValueError("输入必须是字符串")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError('Skill 文档必须以 YAML Frontmatter 分隔符 "---" 开头')
    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            closing_index = index
            break
    if closing_index is None:
        raise ValueError('Skill 文档缺少 Frontmatter 结束标记 "---"')
    frontmatter_text = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1:]).strip()
    try:
        metadata_raw = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise ValueError(f"Skill Frontmatter 不是合法 YAML：{e}") from e
    if not isinstance(metadata_raw, dict):
        raise ValueError("Skill Frontmatter 必须是 YAML 映射")
    normalized_metadata: dict[str, str] = {}
    for field_name in ("name", "description"):
        value = metadata_raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Skill Frontmatter 字段 {field_name!r} 必须是非空字符串")
        normalized_metadata[field_name] = value.strip()
    name = normalized_metadata["name"]
    description = normalized_metadata["description"]
    if len(name) > MAX_SKILL_NAME_LENGTH:
        raise ValueError(f"Skill 名称长度不能超过 {MAX_SKILL_NAME_LENGTH} 个字符")
    if SKILL_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"Skill 名称 {name!r} 必须使用小写 kebab-case")
    if "\n" in description or "\r" in description:
        raise ValueError("Skill description 必须是单行文本")
    if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
        raise ValueError(f"Skill description 长度不能超过 {MAX_SKILL_DESCRIPTION_LENGTH} 个字符")
    if not body:
        raise ValueError("Skill 主体不能为空")
    return SkillMetadata(name, description), body

@dataclass(frozen=True)
class SkillDefinition:
    name: str  # Skill 注册表中的稳定名称
    description: str  # 展示给主模型的 Skill 简短描述
    content: str  # SKILL.md 去除 Frontmatter 后的完整正文
    manifest_path: Path  # 该 Skill 对应的 SKILL.md 规范化路径

def scan_skills(skills_dir: Path = SKILLS_DIR) -> dict[str, SkillDefinition]:
    """扫描 Skill 根目录下的清单并完整构建注册表。"""
    local_registry: dict[str, SkillDefinition] = {}
    if not isinstance(skills_dir, Path):
        raise ValueError("Skill 目录必须是 pathlib.Path 类型")
    skills_root = skills_dir.resolve()
    if not skills_root.is_relative_to(WORKDIR):
        raise ValueError(f"Skill 根目录 {skills_root} 不在工作目录内：{WORKDIR}")
    if not skills_root.exists():
        return local_registry
    if not skills_root.is_dir():
        raise ValueError(f"Skill 根目录不是目录：{skills_root}")
    try:
        skill_directories = sorted(skills_root.iterdir(), key=lambda path: (path.name.casefold(), path.name))
    except OSError as e:
        raise ValueError(f"无法扫描 Skill 根目录 {skills_root}：{type(e).__name__}：{e}") from e
    for skill_directory in skill_directories:
        if not skill_directory.is_dir():
            continue
        manifest_path = (skill_directory / "SKILL.md").resolve()
        if not manifest_path.is_relative_to(skills_root):
            raise ValueError(f"Skill 清单 {manifest_path} 不在 Skill 根目录 {skills_root} 内")
        if not manifest_path.is_file():
            continue
        try:
            file_size = manifest_path.stat().st_size
            if file_size > MAX_SKILL_FILE_BYTES:
                raise ValueError(f"Skill 清单 {manifest_path} 大小为 {file_size} 字节，超过上限 {MAX_SKILL_FILE_BYTES} 字节")
            with open(manifest_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
        except (OSError, UnicodeError) as e:
            raise ValueError(f"无法读取 Skill 清单 {manifest_path}：{type(e).__name__}：{e}") from e
        actual_size = len(raw_content.encode("utf-8"))
        if actual_size > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"Skill 清单 {manifest_path} 大小为 {actual_size} 字节，超过上限 {MAX_SKILL_FILE_BYTES} 字节")

        try:
            metadata, _ = parse_skill_frontmatter(raw_content)
        except ValueError as e:
            raise ValueError(f"解析 Skill 清单失败 {manifest_path}：{e}") from e
        existing_skill = local_registry.get(metadata.name)
        if existing_skill is not None:
            raise ValueError(f"存在重复的 Skill 名称 {metadata.name!r}：{existing_skill.manifest_path} 与 {manifest_path}")
        if len(local_registry) >= MAX_SKILLS:
            raise ValueError(f"Skill 数量不能超过 {MAX_SKILLS} 个")
        local_registry[metadata.name] = SkillDefinition(name=metadata.name, description=metadata.description, content=raw_content, manifest_path=manifest_path)
    return local_registry

def render_skill_catalog(registry: dict[str, SkillDefinition]) -> str:
    """渲染 Skill 目录。"""
    catalog = []
    for skill in sorted(registry.values(), key=lambda skill: skill.name):
        catalog.append(f"- {skill.name}：{skill.description}")
    if not catalog:
        return "当前没有可用的 Skill。"
    return "可用 Skill 目录：\n" + "\n".join(catalog)

def configure_skill_registry(registry: dict[str, SkillDefinition]) -> None:
    """由运行时组装层注入已扫描的 Skill 注册表。"""
    if not isinstance(registry, dict):
        raise TypeError("Skill 注册表必须是字典")
    if any(not isinstance(name, str) or not isinstance(skill, SkillDefinition) for name, skill in registry.items()):
        raise ValueError("Skill 注册表必须由名称和 SkillDefinition 组成")
    global _SKILL_REGISTRY
    _SKILL_REGISTRY = registry

def run_load_skill(name: str) -> str:
    """运行加载 Skill。"""
    if not isinstance(name, str):
        return "Skill 加载失败：Skill 名称必须是字符串类型"
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return f"Skill 加载失败：Skill 名称长度不能超过 {MAX_SKILL_NAME_LENGTH} 个字符"
    if SKILL_NAME_PATTERN.fullmatch(name) is None:
        return f"Skill 加载失败：Skill 名称 {name!r} 必须使用小写 kebab-case"
    skill = _SKILL_REGISTRY.get(name)
    if skill is None:
        return f"Skill 加载失败：未找到名为 '{name}' 的 Skill，请从系统提示中的可用 Skill 目录选择名称。"
    return skill.content
