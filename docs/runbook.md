# Cutover runbook

| Step | Action | Go / no-go gate | Rollback |
|---|---|---|---|
| 0 | Landing zone applied (`profile=full`), HA VPN up, BGP routes learned | All 4 tunnels `ESTABLISHED` | `terraform destroy` (nothing live yet) |
| 1 | Enable dual-write (`DUAL_SOURCE_PRIMARY`) | Secondary-failure rate below 1%, reconcile queue draining | Mode back to `SOURCE_ONLY` |
| 2 | Backfill to the high-water key | Checkpoint `done`; verifier `CONSISTENT` | Truncate target, restart from step 1 |
| 3 | Shadow traffic (Istio mirror 100%) | p99 < 50 ms, errors < 1% on GCP | Remove the mirror (git revert) |
| 4 | Canary 5 → 25 → 50 → 100% of reads | Same SLOs on every window | Automatic: weight 0, source still primary |
| 5 | Write cutover (`DUAL_TARGET_PRIMARY`) | Verifier `CONSISTENT`; rollback window opens (e.g. 7 days) | Mode back to `DUAL_SOURCE_PRIMARY`, then shift reads back |
| 6 | `TARGET_ONLY`, decommission AWS | Window closed, final verify, final snapshot of the source | Restore from the snapshot (last resort) |

## Drills

```bash
# Locally (no cloud):
python -m cutover.simulate --inject-fault canary-25                     # latency regression
python -m cutover.simulate --inject-fault canary-5 --fault-kind errors  # error spike

# Against real Cloud SQL (after terraform apply):
gcloud run jobs execute cutover-drill --region asia-south1 --wait \
  --args=--target,postgres,--inject-fault,canary-25,--out,/reports/rollback-drill
```

Each run writes `report.md` and the rendered VirtualService for each phase to the reports bucket.
