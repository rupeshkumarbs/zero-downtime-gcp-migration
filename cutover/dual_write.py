"""Dual-write repository with an explicit write-mode state machine.

Write modes move strictly forward during a migration and one step back on
rollback, so there is always exactly one system of record:

    SOURCE_ONLY -> DUAL_SOURCE_PRIMARY -> DUAL_TARGET_PRIMARY -> TARGET_ONLY

* In the DUAL_* modes the primary write must succeed (it is the commit); the
  secondary write is best-effort. A failed secondary write is parked on a
  reconciliation queue instead of failing the user request.
* DUAL_TARGET_PRIMARY keeps writing back to the source, so the source stays a
  hot rollback target until the rollback window closes.

In production the secondary write would go through a transactional outbox or
CDC stream (Debezium/Datastream -> Kafka); the semantics are the same.
"""

from __future__ import annotations

import itertools
from collections import deque
from enum import Enum

from .stores import Record, SqliteStore, StoreError


class Mode(str, Enum):
    SOURCE_ONLY = "SOURCE_ONLY"
    DUAL_SOURCE_PRIMARY = "DUAL_SOURCE_PRIMARY"
    DUAL_TARGET_PRIMARY = "DUAL_TARGET_PRIMARY"
    TARGET_ONLY = "TARGET_ONLY"


_ORDER = list(Mode)


class IllegalTransition(Exception):
    pass


class DualWriteRepository:
    def __init__(self, source: SqliteStore, target: SqliteStore, mode: Mode = Mode.SOURCE_ONLY):
        self.source = source
        self.target = target
        self.mode = mode
        self.reconcile_queue: deque[str] = deque()
        self._versions = itertools.count(1)
        self.secondary_failures = 0

    def seed_version(self, start: int) -> None:
        """Make new versions start above any version already present in the data."""
        self._versions = itertools.count(start)

    # -- mode transitions -------------------------------------------------
    def transition(self, new_mode: Mode) -> None:
        """Allow one step forward or one step back, nothing else."""
        step = _ORDER.index(new_mode) - _ORDER.index(self.mode)
        if abs(step) != 1:
            raise IllegalTransition(f"{self.mode.value} -> {new_mode.value}")
        if step < 0 and self.mode is Mode.TARGET_ONLY:
            raise IllegalTransition("source is decommissioned once TARGET_ONLY is reached")
        self.mode = new_mode

    def _roles(self) -> tuple[SqliteStore, SqliteStore | None]:
        return {
            Mode.SOURCE_ONLY: (self.source, None),
            Mode.DUAL_SOURCE_PRIMARY: (self.source, self.target),
            Mode.DUAL_TARGET_PRIMARY: (self.target, self.source),
            Mode.TARGET_ONLY: (self.target, None),
        }[self.mode]

    @property
    def primary(self) -> SqliteStore:
        return self._roles()[0]

    # -- data path --------------------------------------------------------
    def write(self, key: str, value: dict) -> float:
        """Commit to the primary; mirror to the secondary or park for reconciliation."""
        version = next(self._versions)
        primary, secondary = self._roles()
        latency = primary.upsert(key, value, version)  # StoreError propagates: request fails
        if secondary is not None:
            try:
                secondary.upsert(key, value, version)
            except StoreError:
                self.secondary_failures += 1
                self.reconcile_queue.append(key)
        return latency

    def read(self, key: str) -> tuple[Record | None, float]:
        return self.primary.get(key)

    def drain_reconcile_queue(self, max_items: int | None = None) -> tuple[int, int]:
        """Copy the primary's current row to the secondary for each parked key.

        Returns (repaired, still_pending). Copying the primary's *current* row
        (not the original payload) plus the version guard makes this idempotent.
        """
        _, secondary = self._roles()
        if secondary is None:
            self.reconcile_queue.clear()
            return 0, 0
        repaired = 0
        attempts = len(self.reconcile_queue) if max_items is None else min(max_items, len(self.reconcile_queue))
        for _ in range(attempts):
            key = self.reconcile_queue.popleft()
            try:
                record, _ = self.primary.get(key)
                if record is not None:
                    secondary.upsert(record.key, record.value, record.version)
                repaired += 1
            except StoreError:
                self.reconcile_queue.append(key)
        return repaired, len(self.reconcile_queue)
