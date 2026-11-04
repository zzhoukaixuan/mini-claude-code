"""Cron 存储模块：严格解析版本化注册表，并以文件锁和原子替换持久化。"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import (
    CRON_ID_PATTERN,
    CRON_LOCK_POLL_INTERVAL_SECONDS,
    CRON_LOCK_TIMEOUT_SECONDS,
    CRON_STORE_LOCK_PATH,
    CRON_STORE_PATH,
    CRON_STORE_VERSION,
    MAX_CRON_JOBS,
    MAX_CRON_STORE_BYTES,
    WORKDIR,
)
from ..storage.atomic import atomic_replace_text
from ..storage.locking import exclusive_file_lock
from .models import CronJob, cron_job_from_record, cron_job_to_record


def resolve_cron_store_path(store_path: Path = CRON_STORE_PATH) -> Path:
    """校验 Cron 存储路径、符号链接和工作区边界，但不创建文件。"""
    if not isinstance(store_path, Path):
        raise TypeError("Cron 存储路径必须是 pathlib.Path 类型")
    if store_path.is_symlink():
        raise ValueError(f"Cron 存储文件 {store_path} 不能是符号链接")

    resolved_path = store_path.resolve()
    if not resolved_path.is_relative_to(WORKDIR):
        raise ValueError(
            f"Cron 存储文件 {resolved_path} 不在工作目录 {WORKDIR} 内"
        )
    if resolved_path.exists() and not resolved_path.is_file():
        raise ValueError(f"Cron 存储路径 {resolved_path} 不是普通文件")

    resolved_parent = resolved_path.parent.resolve()
    if not resolved_parent.is_relative_to(WORKDIR):
        raise ValueError(
            f"Cron 存储目录 {resolved_parent} 不在工作目录 {WORKDIR} 内"
        )
    return resolved_parent / resolved_path.name


def generate_cron_id() -> str:
    """生成由固定前缀和 UUID4 hex 组成的稳定 CronJob ID。"""
    job_id = f"cron_{uuid.uuid4().hex}"
    if CRON_ID_PATTERN.fullmatch(job_id) is None:
        raise RuntimeError("Harness 生成了格式无效的 CronJob ID")
    return job_id


def _validated_registry(jobs: dict[str, CronJob]) -> dict[str, CronJob]:
    """校验内存注册表键值一致性，并返回按 ID 排序的独立快照。"""
    if not isinstance(jobs, dict):
        raise TypeError("Cron 注册表必须是字典")
    if len(jobs) > MAX_CRON_JOBS:
        raise ValueError(
            f"Cron 注册表包含 {len(jobs)} 条任务，超过上限 {MAX_CRON_JOBS}"
        )

    validated: dict[str, CronJob] = {}
    for job_id, job in jobs.items():
        if not isinstance(job_id, str):
            raise ValueError("Cron 注册表的键必须是字符串 ID")
        if not isinstance(job, CronJob):
            raise ValueError(f"Cron 注册表 {job_id!r} 的值不是 CronJob")
        if job_id != job.id:
            raise ValueError(
                f"Cron 注册表键 {job_id!r} 与记录 ID {job.id!r} 不一致"
            )
        validated[job_id] = job
    return dict(sorted(validated.items()))


def serialize_cron_registry(jobs: dict[str, CronJob]) -> str:
    """只选择 durable Job，并序列化为确定性的版本化 UTF-8 JSON 快照。"""
    registry = _validated_registry(jobs)
    durable_jobs = [
        cron_job_to_record(job)
        for job in registry.values()
        if job.durable
    ]
    try:
        payload = json.dumps(
            {
                "version": CRON_STORE_VERSION,
                "jobs": durable_jobs,
            },
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ) + "\n"
    except (TypeError, ValueError) as error:
        raise ValueError(f"无法序列化 Cron 注册表：{error}") from error

    payload_size = len(payload.encode("utf-8"))
    if payload_size > MAX_CRON_STORE_BYTES:
        raise ValueError(
            f"Cron 注册表序列化后为 {payload_size} 字节，"
            f"超过上限 {MAX_CRON_STORE_BYTES} 字节"
        )
    return payload


def parse_cron_registry_json(text: str) -> dict[str, CronJob]:
    """严格解析一份大小受限且具有明确版本的 durable Cron 注册表。"""
    if not isinstance(text, str):
        raise TypeError("Cron 注册表 JSON 必须是字符串")
    payload_size = len(text.encode("utf-8"))
    if payload_size > MAX_CRON_STORE_BYTES:
        raise ValueError(
            f"Cron 注册表 JSON 为 {payload_size} 字节，"
            f"超过上限 {MAX_CRON_STORE_BYTES} 字节"
        )
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Cron 注册表不是合法 JSON：{error}") from error
    if not isinstance(document, dict):
        raise ValueError("Cron 注册表顶层必须是 JSON 对象")

    expected_fields = {"version", "jobs"}
    actual_fields = set(document)
    missing_fields = expected_fields - actual_fields
    unknown_fields = actual_fields - expected_fields
    if missing_fields:
        formatted_fields = ", ".join(sorted(missing_fields))
        raise ValueError(f"Cron 注册表缺少顶层字段：{formatted_fields}")
    if unknown_fields:
        formatted_fields = ", ".join(
            sorted(repr(field) for field in unknown_fields)
        )
        raise ValueError(f"Cron 注册表包含未知顶层字段：{formatted_fields}")

    version = document["version"]
    records = document["jobs"]
    if type(version) is not int or version != CRON_STORE_VERSION:
        raise ValueError(
            f"Cron 注册表版本必须是 {CRON_STORE_VERSION}，实际为 {version!r}"
        )
    if not isinstance(records, list):
        raise ValueError("Cron 注册表 jobs 必须是数组")
    if len(records) > MAX_CRON_JOBS:
        raise ValueError(
            f"Cron 注册表包含 {len(records)} 条任务，超过上限 {MAX_CRON_JOBS}"
        )

    registry: dict[str, CronJob] = {}
    for index, record in enumerate(records):
        try:
            job = cron_job_from_record(record)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Cron 注册表第 {index + 1} 条记录无效：{error}"
            ) from error
        if not job.durable:
            raise ValueError(
                f"Cron 注册表中的 Job {job.id!r} 必须设置 durable=true"
            )
        if job.id in registry:
            raise ValueError(f"Cron 注册表包含重复 ID {job.id!r}")
        registry[job.id] = job
    return dict(sorted(registry.items()))


def load_durable_cron_jobs(
    store_path: Path = CRON_STORE_PATH,
) -> dict[str, CronJob]:
    """读取 durable Cron 快照；文件不存在表示尚未注册任何持久计划。"""
    resolved_path = resolve_cron_store_path(store_path)
    if not resolved_path.exists():
        return {}

    try:
        with open(resolved_path, "rb") as cron_file:
            payload = cron_file.read(MAX_CRON_STORE_BYTES + 1)
    except OSError as error:
        raise OSError(f"无法读取 Cron 注册表 {resolved_path}：{error}") from error
    if len(payload) > MAX_CRON_STORE_BYTES:
        raise ValueError(
            f"Cron 注册表 {resolved_path} 超过 {MAX_CRON_STORE_BYTES} 字节"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"Cron 注册表 {resolved_path} 不是合法 UTF-8：{error}"
        ) from error
    try:
        return parse_cron_registry_json(text)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Cron 注册表 {resolved_path} 无效：{error}") from error


def save_durable_cron_jobs(
    jobs: dict[str, CronJob],
    store_path: Path = CRON_STORE_PATH,
) -> Path:
    """原子替换 durable Cron 快照；多步骤事务仍必须由调用方持有文件锁。"""
    resolved_path = resolve_cron_store_path(store_path)
    payload = serialize_cron_registry(jobs)
    return atomic_replace_text(
        resolved_path,
        payload,
        allowed_root=WORKDIR,
        replace_existing=True,
    )


@contextmanager
def cron_store_lock(store_path: Path = CRON_STORE_PATH) -> Iterator[None]:
    """锁住指定 Cron 快照对应的完整读—判断—修改—保存事务。"""
    resolved_path = resolve_cron_store_path(store_path)
    lock_path = (
        CRON_STORE_LOCK_PATH
        if resolved_path == CRON_STORE_PATH.resolve()
        else resolved_path.with_suffix(".lock")
    )
    with exclusive_file_lock(
        lock_path,
        allowed_root=WORKDIR,
        timeout_seconds=CRON_LOCK_TIMEOUT_SECONDS,
        poll_interval_seconds=CRON_LOCK_POLL_INTERVAL_SECONDS,
    ):
        yield
