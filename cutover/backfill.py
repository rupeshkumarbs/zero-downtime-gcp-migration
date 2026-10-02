"""Resumable, checkpointed historical backfill.

Copies rows from source to target in key order using keyset pagination. Each
row is written with its original version, so the target's version guard keeps
any newer row that dual-write has already landed there. A failure mid-batch
leaves the checkpoint at the last fully copied batch, so a rerun resumes
without gaps; re-copying rows is harmless because upserts are idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass

from .stores import SqliteStore, StoreError


@dataclass
class Checkpoint:
    last_key: str | None = None
    copied: int = 0
    batches: int = 0
    done: bool = False


def _upsert_with_retry(target: SqliteStore, record, attempts: int) -> None:
    for attempt in range(1, attempts + 1):
        try:
            target.upsert(record.key, record.value, record.version)
            return
        except StoreError:
            if attempt == attempts:
                raise  # batch aborts; checkpoint stays at the previous batch


def backfill(
    source: SqliteStore,
    target: SqliteStore,
    *,
    batch_size: int = 500,
    checkpoint: Checkpoint | None = None,
    max_batches: int | None = None,
    row_attempts: int = 5,
    high_water_key: str | None = None,
) -> Checkpoint:
    """Copy up to ``max_batches`` batches (all remaining when None) and return the checkpoint.

    Transient target errors are retried per row (``row_attempts``); a row that
    still fails aborts the batch with ``StoreError`` and leaves the checkpoint
    untouched, so the caller can simply call again to resume.

    ``high_water_key`` is the largest key that existed when dual-write was
    switched on. Rows above it were created under dual-write and already live
    in the target, so the backfill stops there instead of chasing new inserts.
    """
    cp = checkpoint or Checkpoint()
    processed = 0
    while not cp.done and (max_batches is None or processed < max_batches):
        batch = source.scan(cp.last_key, batch_size)
        if high_water_key is not None:
            batch = [r for r in batch if r.key <= high_water_key]
        if not batch:
            cp.done = True
            break
        for record in batch:
            _upsert_with_retry(target, record, row_attempts)
        cp.last_key = batch[-1].key
        cp.copied += len(batch)
        cp.batches += 1
        processed += 1
    return cp
