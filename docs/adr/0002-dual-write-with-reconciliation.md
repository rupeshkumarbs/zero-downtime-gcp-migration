# ADR-0002: Data movement - dual-write with reconciliation, plus checkpointed backfill

- **Status:** Accepted

## Options considered

| Option | Downtime | Rollback | Complexity | Verdict |
|---|---|---|---|---|
| Dump and restore in a maintenance window | Hours | Restore again | Low | Rejected: violates zero downtime |
| CDC only (DMS / Datastream into Cloud SQL) | None | Needs a second, reverse CDC pipeline | Medium | Used as the transport in production |
| **Application dual-write + backfill + verifier** | None | Built in: the old primary is never stale | Medium | **Chosen** as the control model |

## Decision

1. **Write modes** move one step at a time: `SOURCE_ONLY → DUAL_SOURCE_PRIMARY → DUAL_TARGET_PRIMARY → TARGET_ONLY`. A rollback is one step back. There is always exactly one system of record.
2. **The primary write is the commit.** If the secondary write fails, the key is parked on a reconciliation queue and the user's request still succeeds. Reconciliation copies the primary's *current* row, so it is idempotent.
3. **Every row carries a monotonically increasing version.** Upserts are version-guarded (`ON CONFLICT ... WHERE excluded.version > version`), so backfill, dual-write and reconciliation can run concurrently and be retried in any order.
4. **Backfill** uses keyset pagination with a checkpoint. It stops at the high-water key captured when dual-write started, because later rows are already dual-written. Transient errors are retried per row; a hard failure aborts the batch without advancing the checkpoint.
5. **Verifier:** a streaming merge-join over both stores comparing SHA-256 digests of (version, canonical JSON). Traffic does not move unless the report is `CONSISTENT`.

## Consequences

- The source stays a hot rollback target until the window closes (`DUAL_TARGET_PRIMARY`).
- In production the secondary write goes through a transactional outbox or CDC stream (Debezium/Datastream to Kafka) instead of a synchronous call. The semantics and the verifier gate stay the same.
