"""Zero-downtime cloud cutover toolkit.

Building blocks for migrating a stateful service from a source cloud (AWS)
to a target cloud (GCP) without downtime or data loss:

- ``stores``     storage adapters with fault injection (SQLite stands in for RDS / Cloud SQL)
- ``dual_write`` dual-write repository with write-mode state machine and reconciliation queue
- ``backfill``   resumable, checkpointed, version-guarded historical backfill
- ``verifier``   merge-join consistency verifier (missing / extra / mismatched rows)
- ``traffic``    SLO-gated traffic-shift controller with automated rollback
- ``gitops``     renders Istio VirtualService weights for each phase (committed, synced by Argo CD)
- ``simulate``   end-to-end cutover simulation with fault injection
"""

__version__ = "0.1.0"
