"""End-to-end zero-downtime cutover simulation.

Runs the full playbook against two in-memory databases (AWS RDS -> GCP Cloud
SQL) while live traffic keeps flowing:

  1. seed       source holds the historical data (SOURCE_ONLY)
  2. dual-write new writes go to both clouds; target flakiness is parked on the
                reconciliation queue instead of failing requests
  3. backfill   resumable batches copied under live load, then reconcile + verify
  4. read cut   shadow -> canary 5/25/50 -> 100%, gated on SLOs; any breach rolls back
  5. write cut  target becomes primary, source keeps receiving reverse writes
                (rollback window), then final verify and source decommission

Usage:
    python -m cutover.simulate
    python -m cutover.simulate --inject-fault canary-25 --fault-kind latency
    python -m cutover.simulate --out out/  # writes report.md + rendered manifests
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .backfill import Checkpoint, backfill
from .dual_write import DualWriteRepository, Mode
from .gitops import render_virtualservice
from .stores import PostgresStore, SqliteStore, StoreError, postgres_dsn_from_env
from .traffic import DEFAULT_PLAN, Decision, Status, TrafficController, WindowMetrics
from .verifier import VerificationReport, verify

PHASE_NAMES = [p.name for p in DEFAULT_PLAN]


@dataclass
class SimulationResult:
    status: Status
    final_mode: Mode
    final_verification: VerificationReport
    timeline: list[str] = field(default_factory=list)
    manifests: list[tuple[str, str]] = field(default_factory=list)
    writes: int = 0
    secondary_failures: int = 0
    failed_requests: int = 0

    @property
    def zero_data_loss(self) -> bool:
        return self.final_verification.consistent


def _order(i: int, rng: random.Random) -> dict:
    return {
        "order_id": i,
        "amount": round(rng.uniform(5, 500), 2),
        "status": rng.choice(["NEW", "PAID", "SHIPPED", "DELIVERED"]),
    }


def _key(i: int) -> str:
    return f"order-{i:08d}"


def run(
    *,
    records: int = 5000,
    seed: int = 7,
    inject_fault: str | None = None,
    fault_kind: str = "latency",
    batch_size: int = 500,
    writes_per_tick: int = 25,
    requests_per_window: int = 500,
    target_dsn: str | None = None,
) -> SimulationResult:
    if inject_fault is not None and inject_fault not in PHASE_NAMES:
        raise ValueError(f"unknown phase {inject_fault!r}; choose from {PHASE_NAMES}")

    rng = random.Random(seed)
    source = SqliteStore("aws-rds", base_latency_ms=3.0, seed=seed)
    if target_dsn:
        target = PostgresStore("gcp-cloudsql", target_dsn, seed=seed + 1)  # real Cloud SQL
        target.reset()
    else:
        target = SqliteStore("gcp-cloudsql", base_latency_ms=2.5, seed=seed + 1)
    repo = DualWriteRepository(source, target)
    timeline: list[str] = []
    next_id = records
    stats = {"writes": 0, "failed_requests": 0}

    def log(msg: str) -> None:
        timeline.append(msg)

    def live_write() -> None:
        nonlocal next_id
        if rng.random() < 0.3:
            i, next_id = next_id, next_id + 1
        else:
            i = rng.randrange(next_id)
        try:
            repo.write(_key(i), _order(i, rng))
            stats["writes"] += 1
        except StoreError:
            stats["failed_requests"] += 1  # primary failed: client sees an error, nothing committed

    # 1. seed ---------------------------------------------------------------
    for i in range(records):
        repo.write(_key(i), _order(i, rng))
    log(f"[seed]       {records} orders in {source.name} (mode={repo.mode.value})")

    # 2 + 3. dual-write and backfill under live load --------------------------
    high_water_key = _key(next_id - 1)  # everything above this is dual-written from now on
    repo.transition(Mode.DUAL_SOURCE_PRIMARY)
    target.faults.error_rate = 0.02  # flaky target while it is being built up
    log(f"[dual-write] mode={repo.mode.value}; {target.name} injected 2% write errors")

    cp, retries = Checkpoint(), 0
    while not cp.done:
        try:
            backfill(source, target, batch_size=batch_size, checkpoint=cp, max_batches=1,
                     high_water_key=high_water_key)
        except StoreError:
            retries += 1  # checkpoint did not advance; the batch is retried
        for _ in range(writes_per_tick):
            live_write()
    target.faults.error_rate = 0.0
    log(
        f"[backfill]   copied {cp.copied} rows in {cp.batches} batches "
        f"({retries} batch retries) while serving {stats['writes']} live writes"
    )

    repaired, pending = repo.drain_reconcile_queue()
    log(f"[reconcile]  {repo.secondary_failures} failed mirror writes, {repaired} repaired, {pending} pending")
    report = verify(source, target)
    log(f"[verify]     {report.summary()}")
    if not report.consistent:
        log("[abort]      data drift before traffic shift - cutover not started")
        return SimulationResult(Status.ROLLED_BACK, repo.mode, report, timeline, [], stats["writes"],
                                repo.secondary_failures, stats["failed_requests"])

    # 4. read cutover -----------------------------------------------------------
    controller = TrafficController()
    manifests: list[tuple[str, str]] = []
    window = WindowMetrics()
    applied_phase = None
    while controller.status is Status.PROGRESSING:
        phase = controller.phase
        if phase.name != applied_phase:
            manifests.append((phase.name, render_virtualservice(phase)))
            applied_phase = phase.name
            if phase.name == inject_fault:
                if fault_kind == "latency":
                    target.faults.extra_latency_ms = 120.0
                else:
                    target.faults.error_rate = 0.05
                log(f"[fault]      injected {fault_kind} fault on {target.name} during {phase.name}")

        for _ in range(requests_per_window):
            if rng.random() < 0.2:
                live_write()
                continue
            key = _key(rng.randrange(next_id))
            to_target = phase.mirror or rng.random() * 100 < controller.target_weight
            if not to_target:
                source.get(key)
                continue
            try:
                _, latency = target.get(key)
                window.record(True, latency)
            except StoreError:
                window.record(False, 0.0)
                if not phase.mirror:
                    stats["failed_requests"] += 1  # mirrored failures never reach users

        decision, reason = controller.evaluate(window)
        if decision is Decision.HOLD:
            continue  # keep accumulating the same window
        label = f"{phase.name} ({'mirror' if phase.mirror else f'{phase.target_weight}% to GCP'})"
        log(f"[traffic]    {label:<22} {decision.value:<8} {reason}")
        window = WindowMetrics()

    if controller.status is Status.ROLLED_BACK:
        manifests.append(("rollback", render_virtualservice(controller.phase, rolled_back=True)))
        target.faults.extra_latency_ms, target.faults.error_rate = 0.0, 0.0
        log(f"[rollback]   100% traffic back on AWS; write primary unchanged ({repo.mode.value})")
        repo.drain_reconcile_queue()
        final = verify(source, target)
        log(f"[verify]     {final.summary()}")
        return SimulationResult(Status.ROLLED_BACK, repo.mode, final, timeline, manifests,
                                stats["writes"], repo.secondary_failures, stats["failed_requests"])

    # 5. write cutover ---------------------------------------------------------------
    repo.transition(Mode.DUAL_TARGET_PRIMARY)
    for _ in range(writes_per_tick * 10):
        live_write()
    repo.drain_reconcile_queue()
    check = verify(source, target)
    log(f"[write cut]  mode={repo.mode.value}; source kept in sync for rollback: {check.summary()}")

    repo.transition(Mode.TARGET_ONLY)
    final = verify(source, target)
    log(f"[complete]   mode={repo.mode.value}; {source.name} ready to decommission")
    return SimulationResult(Status.COMPLETED, repo.mode, final, timeline, manifests,
                            stats["writes"], repo.secondary_failures, stats["failed_requests"])


def _write_outputs(result: SimulationResult, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    manifest_dir = out / "manifests"
    manifest_dir.mkdir(exist_ok=True)
    for i, (name, yaml) in enumerate(result.manifests):
        (manifest_dir / f"{i:02d}-{name}.yaml").write_text(yaml, encoding="utf-8")
    lines = [
        "# Cutover report",
        "",
        f"- **Outcome:** {result.status.value}",
        f"- **Final write mode:** {result.final_mode.value}",
        f"- **Zero data loss:** {'yes' if result.zero_data_loss else 'NO'} ({result.final_verification.summary()})",
        f"- **Live writes during migration:** {result.writes}",
        f"- **Mirror writes parked & reconciled:** {result.secondary_failures}",
        f"- **User-facing failed requests:** {result.failed_requests}",
        "",
        "## Timeline",
        "",
        "```",
        *result.timeline,
        "```",
        "",
        f"Rendered {len(result.manifests)} VirtualService manifests to `manifests/`.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--inject-fault", choices=PHASE_NAMES, help="phase in which the target degrades")
    parser.add_argument("--fault-kind", choices=["latency", "errors"], default="latency")
    parser.add_argument("--out", type=Path, help="directory for report.md and rendered manifests")
    parser.add_argument(
        "--target", choices=["sqlite", "postgres"], default="sqlite",
        help="postgres = real target DB from TARGET_DSN or TARGET_DB_* env vars (Cloud Run)",
    )
    args = parser.parse_args(argv)

    dsn = None
    if args.target == "postgres":
        dsn = postgres_dsn_from_env()
        if not dsn:
            parser.error("--target postgres needs TARGET_DSN or TARGET_DB_HOST/TARGET_DB_PASSWORD")
    result = run(records=args.records, seed=args.seed, inject_fault=args.inject_fault,
                 fault_kind=args.fault_kind, target_dsn=dsn)
    print("\n".join(result.timeline))
    print(f"\nOutcome: {result.status.value} | zero data loss: {result.zero_data_loss}")
    if args.out:
        _write_outputs(result, args.out)
        print(f"Report written to {args.out / 'report.md'}")
    return 0 if result.zero_data_loss else 1


if __name__ == "__main__":
    sys.exit(main())
