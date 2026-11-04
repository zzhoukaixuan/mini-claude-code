"""Teammate 注册表：在线程锁内保存不可变快照并执行受控状态迁移。"""

from __future__ import annotations

import threading
from dataclasses import replace

from ..config import (
    ACTIVE_TEAMMATE_STATUSES,
    MAX_ACTIVE_TEAMMATES,
    MAX_TEAMMATE_RECORDS,
)
from .models import TeammateSnapshot, TeammateSpec, validate_team_member_name


class TeammateNotFoundError(LookupError):
    """请求的 Teammate 名称不在当前进程注册表中。"""


class DuplicateTeammateError(ValueError):
    """请求注册的 Teammate 名称已经被当前进程占用。"""


class InvalidTeammateTransitionError(ValueError):
    """请求的 Teammate 状态迁移不符合生命周期。"""


class TeammateRegistry:
    """保存当前 CLI 进程中的 Teammate，并原子执行生命周期迁移。"""

    def __init__(
        self,
        *,
        max_records: int = MAX_TEAMMATE_RECORDS,
        max_active: int = MAX_ACTIVE_TEAMMATES,
    ) -> None:
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_records <= 0
        ):
            raise ValueError("Teammate Registry max_records 必须是正整数")
        if (
            isinstance(max_active, bool)
            or not isinstance(max_active, int)
            or max_active <= 0
        ):
            raise ValueError("Teammate Registry max_active 必须是正整数")
        if max_active > max_records:
            raise ValueError("Teammate Registry max_active 不能超过 max_records")

        self._max_records = max_records  # 当前进程最多保留的 Teammate 快照数
        self._max_active = max_active  # starting/running/idle Teammate 并发上限
        self._snapshots: dict[str, TeammateSnapshot] = {}  # 按稳定名称索引的最新快照
        self._lock = threading.Lock()  # 保护名称占用、状态判断与快照替换事务

    def _require_snapshot_locked(self, name: str) -> TeammateSnapshot:
        """校验名称，并取得持锁状态下的最新 Teammate 快照。"""
        normalized_name = validate_team_member_name(
            name,
            field_name="Teammate name",
        )
        snapshot = self._snapshots.get(normalized_name)
        if snapshot is None:
            raise TeammateNotFoundError(f"Teammate {normalized_name!r} 不存在")
        return snapshot

    @staticmethod
    def _require_source_status(
        snapshot: TeammateSnapshot,
        target_status: str,
        allowed_source_statuses: frozenset[str],
    ) -> None:
        """拒绝终态回退、重复完成和其他未声明的状态跳转。"""
        if snapshot.status not in allowed_source_statuses:
            allowed_text = ", ".join(sorted(allowed_source_statuses))
            raise InvalidTeammateTransitionError(
                f"Teammate {snapshot.spec.name!r} 当前状态为 {snapshot.status!r}，"
                f"不能迁移到 {target_status!r}；允许的来源状态：{allowed_text}"
            )

    def register(self, spec: TeammateSpec) -> TeammateSnapshot:
        """以 starting 状态原子占用一个此前未注册的 Teammate 名称。"""
        if not isinstance(spec, TeammateSpec):
            raise TypeError("Teammate Registry 只能注册 TeammateSpec")
        with self._lock:
            if spec.name in self._snapshots:
                raise DuplicateTeammateError(
                    f"Teammate 名称 {spec.name!r} 已经被注册"
                )
            if len(self._snapshots) >= self._max_records:
                raise ValueError(
                    f"Teammate 注册记录已经达到上限 {self._max_records}"
                )
            active_count = sum(
                snapshot.status in ACTIVE_TEAMMATE_STATUSES
                for snapshot in self._snapshots.values()
            )
            if active_count >= self._max_active:
                raise ValueError(
                    f"活跃 Teammate 数量已经达到上限 {self._max_active}"
                )

            snapshot = TeammateSnapshot(spec=spec, status="starting")
            self._snapshots[spec.name] = snapshot
            return snapshot

    def mark_running(self, name: str, started_at: float) -> TeammateSnapshot:
        """将 starting Teammate 标记为 Runner 已经开始执行。"""
        with self._lock:
            snapshot = self._require_snapshot_locked(name)
            self._require_source_status(
                snapshot,
                "running",
                frozenset({"starting"}),
            )
            updated = replace(
                snapshot,
                status="running",
                started_at=started_at,
            )
            self._snapshots[snapshot.spec.name] = updated
            return updated

    def mark_idle(self, name: str) -> TeammateSnapshot:
        """将 running 成员标记为空闲；不结束其生命周期，也不保存任务完成结果。"""
        with self._lock:
            snapshot = self._require_snapshot_locked(name)
            self._require_source_status(snapshot, "idle", frozenset({"running"}))
            updated = replace(snapshot, status="idle")
            self._snapshots[snapshot.spec.name] = updated
            return updated

    def mark_resumed(self, name: str) -> TeammateSnapshot:
        """将 idle 成员恢复为 running；保留首次 started_at，不重置生命周期计时。"""
        with self._lock:
            snapshot = self._require_snapshot_locked(name)
            self._require_source_status(snapshot, "running", frozenset({"idle"}))
            updated = replace(snapshot, status="running")
            self._snapshots[snapshot.spec.name] = updated
            return updated

    def mark_completed(
        self,
        name: str,
        result: str,
        finished_at: float,
    ) -> TeammateSnapshot:
        """将 running Teammate 迁移为不可逆的 completed。"""
        with self._lock:
            snapshot = self._require_snapshot_locked(name)
            self._require_source_status(
                snapshot,
                "completed",
                frozenset({"running"}),
            )
            updated = replace(
                snapshot,
                status="completed",
                finished_at=finished_at,
                result=result,
                error=None,
            )
            self._snapshots[snapshot.spec.name] = updated
            return updated

    def mark_stopped(self, name: str, finished_at: float) -> TeammateSnapshot:
        """Runner 同意关闭并结束执行后，由 Manager 记录 stopped，不伪造完成结果。"""
        with self._lock:
            snapshot = self._require_snapshot_locked(name)
            self._require_source_status(
                snapshot, "stopped", frozenset({"running", "idle"}),
            )
            updated = replace(
                snapshot, status="stopped", finished_at=finished_at,
                result=None, error=None,
            )
            self._snapshots[snapshot.spec.name] = updated
            return updated

    def mark_failed(
        self,
        name: str,
        error: str,
        finished_at: float,
    ) -> TeammateSnapshot:
        """记录线程启动前失败或 running/idle Teammate 的异常终态。"""
        with self._lock:
            snapshot = self._require_snapshot_locked(name)
            self._require_source_status(
                snapshot,
                "failed",
                frozenset({"starting", "running", "idle"}),
            )
            updated = replace(
                snapshot,
                status="failed",
                finished_at=finished_at,
                result=None,
                error=error,
            )
            self._snapshots[snapshot.spec.name] = updated
            return updated

    def get(self, name: str) -> TeammateSnapshot:
        """返回指定 Teammate 的不可变最新快照。"""
        with self._lock:
            return self._require_snapshot_locked(name)

    def list_all(self) -> list[TeammateSnapshot]:
        """按创建时间和名称稳定返回所有 Teammate 快照的独立列表。"""
        with self._lock:
            return sorted(
                self._snapshots.values(),
                key=lambda snapshot: (
                    snapshot.spec.created_at,
                    snapshot.spec.name,
                ),
            )
