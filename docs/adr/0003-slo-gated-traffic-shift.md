# ADR-0003: SLO-gated traffic shift with automated rollback, delivered through GitOps

- **Status:** Accepted

## Decision

- **Read cutover before write cutover.** Reads move first while the source stays the write primary, so sending traffic back loses nothing. Writes flip only after reads are fully on GCP and stable.
- **Phases:** `shadow` (Istio mirrors 100% of requests to GCP; responses are discarded) → `canary-5` → `canary-25` → `canary-50` → `full`.
- **Gate per observation window:** p99 latency below 50 ms and error rate below 1%, with at least 200 requests observed. The controller returns PROMOTE, HOLD (not enough data yet) or ROLLBACK (any breach). Rollback sets the GCP weight to 0 immediately.
- **GitOps delivery:** the controller never calls the cluster. It renders the VirtualService for the phase (`cutover/gitops.py`) and commits it, and Argo CD syncs it. Every traffic change is reviewable and revertible, which keeps deployments touchless.
- **After the migration:** Argo Rollouts plus Datadog analysis applies the same SLOs to every in-cluster release (`deploy/k8s/base/rollout.yaml`).

## Consequences

- Shadow traffic finds performance problems before any user is affected, but it doubles read load on the target. Size Cloud SQL for that.
- Mirrored writes must be idempotent or suppressed. Mirroring here is for reads; writes use dual-write (ADR-0002).
