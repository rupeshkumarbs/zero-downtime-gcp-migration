"""Storage adapters used by the cutover engine.

``SqliteStore`` stands in for a managed relational database (Amazon RDS on the
source side, Cloud SQL on the target side). Every operation returns a simulated
latency in milliseconds and can be made to fail or slow down at runtime through
its ``faults`` profile, which is how the simulation injects incidents.

Writes are version-guarded (last-writer-wins on a monotonically increasing
version), which is what makes dual-write, backfill and reconciliation safe to
run concurrently and to retry.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterator


class StoreError(Exception):
    """Raised when a store operation fails (injected or real)."""


@dataclass
class FaultProfile:
    error_rate: float = 0.0
    extra_latency_ms: float = 0.0


@dataclass(frozen=True)
class Record:
    key: str
    value: dict
    version: int

    @property
    def digest(self) -> str:
        payload = json.dumps(self.value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{self.version}|{payload}".encode()).hexdigest()


class SqliteStore:
    """A key/value table with version-guarded upserts and keyset pagination."""

    def __init__(
        self,
        name: str,
        *,
        base_latency_ms: float = 2.0,
        seed: int = 0,
        path: str = ":memory:",
    ) -> None:
        self.name = name
        self.base_latency_ms = base_latency_ms
        self.faults = FaultProfile()
        self._rng = random.Random(seed)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " version INTEGER NOT NULL)"
        )

    def _io(self) -> float:
        if self.faults.error_rate and self._rng.random() < self.faults.error_rate:
            raise StoreError(f"{self.name}: injected failure")
        jitter = self._rng.expovariate(1 / max(self.base_latency_ms * 0.25, 0.01))
        return self.base_latency_ms + self.faults.extra_latency_ms + jitter

    def upsert(self, key: str, value: dict, version: int) -> float:
        """Write ``value`` unless the stored row already has a newer version."""
        latency = self._io()
        self._db.execute(
            "INSERT INTO records(key, value, version) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, version = excluded.version "
            "WHERE excluded.version > records.version",
            (key, json.dumps(value, sort_keys=True), version),
        )
        return latency

    def get(self, key: str) -> tuple[Record | None, float]:
        latency = self._io()
        row = self._db.execute(
            "SELECT key, value, version FROM records WHERE key = ?", (key,)
        ).fetchone()
        return (_to_record(row) if row else None), latency

    def scan(self, after_key: str | None, limit: int) -> list[Record]:
        """Keyset pagination in key order; not subject to fault injection."""
        if after_key is None:
            rows = self._db.execute(
                "SELECT key, value, version FROM records ORDER BY key LIMIT ?", (limit,)
            )
        else:
            rows = self._db.execute(
                "SELECT key, value, version FROM records WHERE key > ? ORDER BY key LIMIT ?",
                (after_key, limit),
            )
        return [_to_record(r) for r in rows]

    def iter_all(self, page_size: int = 1000) -> Iterator[Record]:
        after = None
        while True:
            page = self.scan(after, page_size)
            if not page:
                return
            yield from page
            after = page[-1].key

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def _to_record(row: tuple) -> Record:
    return Record(key=row[0], value=json.loads(row[1]), version=row[2])


class PostgresStore:
    """Same contract as ``SqliteStore`` backed by PostgreSQL (e.g. Cloud SQL).

    Latency is measured, not simulated; ``faults`` still adds injected
    latency/errors on top so rollback drills work against a real database.
    Requires ``psycopg`` (v3), installed in the container image.
    """

    def __init__(self, name: str, dsn: str, *, table: str = "cutover_records", seed: int = 0) -> None:
        import psycopg  # imported lazily so the local simulation stays dependency-free

        self.name = name
        self.faults = FaultProfile()
        self._rng = random.Random(seed)
        self._table = table
        self._db = psycopg.connect(dsn, autocommit=True)
        self._db.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL, version BIGINT NOT NULL)"
        )

    def reset(self) -> None:
        self._db.execute(f"TRUNCATE {self._table}")

    def _timed(self, sql: str, params: tuple) -> tuple[list[tuple], float]:
        if self.faults.error_rate and self._rng.random() < self.faults.error_rate:
            raise StoreError(f"{self.name}: injected failure")
        started = time.perf_counter()
        cur = self._db.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        return rows, (time.perf_counter() - started) * 1000 + self.faults.extra_latency_ms

    def upsert(self, key: str, value: dict, version: int) -> float:
        _, latency = self._timed(
            f"INSERT INTO {self._table} AS r (key, value, version) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, version = EXCLUDED.version "
            "WHERE EXCLUDED.version > r.version",
            (key, json.dumps(value, sort_keys=True), version),
        )
        return latency

    def get(self, key: str) -> tuple[Record | None, float]:
        rows, latency = self._timed(
            f"SELECT key, value, version FROM {self._table} WHERE key = %s", (key,)
        )
        return (_to_record(rows[0]) if rows else None), latency

    def scan(self, after_key: str | None, limit: int) -> list[Record]:
        if after_key is None:
            cur = self._db.execute(
                f"SELECT key, value, version FROM {self._table} ORDER BY key COLLATE \"C\" LIMIT %s", (limit,)
            )
        else:
            cur = self._db.execute(
                f"SELECT key, value, version FROM {self._table} WHERE key COLLATE \"C\" > %s "
                "ORDER BY key COLLATE \"C\" LIMIT %s",
                (after_key, limit),
            )
        return [_to_record(r) for r in cur.fetchall()]

    def iter_all(self, page_size: int = 1000) -> Iterator[Record]:
        after = None
        while True:
            page = self.scan(after, page_size)
            if not page:
                return
            yield from page
            after = page[-1].key

    def count(self) -> int:
        return self._db.execute(f"SELECT COUNT(*) FROM {self._table}").fetchone()[0]


def postgres_dsn_from_env(env: dict | None = None) -> str | None:
    """Build a DSN from TARGET_DB_* variables (set by Cloud Run / Secret Manager)."""
    import os

    env = env if env is not None else os.environ
    if "TARGET_DSN" in env:
        return env["TARGET_DSN"]
    host = env.get("TARGET_DB_HOST")
    if not host:
        return None
    return (
        f"host={host} port={env.get('TARGET_DB_PORT', '5432')} "
        f"dbname={env.get('TARGET_DB_NAME', 'orders')} user={env.get('TARGET_DB_USER', 'cutover')} "
        f"password={env['TARGET_DB_PASSWORD']} sslmode={env.get('TARGET_DB_SSLMODE', 'require')}"
    )
