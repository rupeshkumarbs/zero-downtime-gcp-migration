"""Source/target consistency verifier.

Streams both stores in key order and merge-joins them, so memory use is
constant regardless of table size. Rows are compared by a SHA-256 digest of
(version, canonical JSON payload). The report is the gate for every cutover
phase: traffic does not move unless the data is provably identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .stores import SqliteStore


@dataclass
class VerificationReport:
    source_count: int = 0
    target_count: int = 0
    missing_count: int = 0
    extra_count: int = 0
    mismatched_count: int = 0
    # Bounded example keys for each drift category, for the runbook / incident ticket.
    missing_in_target: list[str] = field(default_factory=list)
    extra_in_target: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not (self.missing_count or self.extra_count or self.mismatched_count)

    def summary(self) -> str:
        return (
            f"source={self.source_count} target={self.target_count} "
            f"missing={self.missing_count} extra={self.extra_count} "
            f"mismatched={self.mismatched_count} -> {'CONSISTENT' if self.consistent else 'DRIFT'}"
        )


def verify(source: SqliteStore, target: SqliteStore, *, max_examples: int = 20) -> VerificationReport:
    report = VerificationReport()
    src, tgt = source.iter_all(), target.iter_all()
    s, t = next(src, None), next(tgt, None)

    while s is not None or t is not None:
        if t is None or (s is not None and s.key < t.key):
            report.source_count += 1
            report.missing_count += 1
            if len(report.missing_in_target) < max_examples:
                report.missing_in_target.append(s.key)
            s = next(src, None)
        elif s is None or t.key < s.key:
            report.target_count += 1
            report.extra_count += 1
            if len(report.extra_in_target) < max_examples:
                report.extra_in_target.append(t.key)
            t = next(tgt, None)
        else:
            report.source_count += 1
            report.target_count += 1
            if s.digest != t.digest:
                report.mismatched_count += 1
                if len(report.mismatched) < max_examples:
                    report.mismatched.append(s.key)
            s, t = next(src, None), next(tgt, None)
    return report
