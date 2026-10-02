"""SLO-gated traffic-shift controller with automated rollback.

The plan starts with a *shadow* phase (Istio request mirroring: the target
serves a copy of live traffic, responses are discarded) and then shifts real
traffic in canary steps. After every observation window the controller makes
one decision:

* PROMOTE  - window met min_requests and every SLO  -> next phase
* HOLD     - not enough traffic yet to judge         -> keep observing
* ROLLBACK - any SLO breached                        -> all traffic back to source

Rollback is cheap by construction: during the read cutover the source is
still the write primary, so sending traffic back loses nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class SLO:
    max_error_rate: float = 0.01
    max_p99_ms: float = 50.0


@dataclass(frozen=True)
class Phase:
    name: str
    target_weight: int  # % of live traffic served by the target cloud
    mirror: bool = False  # shadow traffic to the target (responses discarded)
    min_requests: int = 200


DEFAULT_PLAN: tuple[Phase, ...] = (
    Phase("shadow", 0, mirror=True),
    Phase("canary-5", 5),
    Phase("canary-25", 25),
    Phase("canary-50", 50),
    Phase("full", 100),
)


@dataclass
class WindowMetrics:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, ok: bool, latency_ms: float) -> None:
        if ok:
            self.latencies_ms.append(latency_ms)
        else:
            self.errors += 1

    @property
    def requests(self) -> int:
        return len(self.latencies_ms) + self.errors

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0

    @property
    def p99_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        return ordered[min(len(ordered) - 1, math.ceil(0.99 * len(ordered)) - 1)]


class Decision(str, Enum):
    PROMOTE = "PROMOTE"
    HOLD = "HOLD"
    ROLLBACK = "ROLLBACK"


class Status(str, Enum):
    PROGRESSING = "PROGRESSING"
    COMPLETED = "COMPLETED"
    ROLLED_BACK = "ROLLED_BACK"


class TrafficController:
    def __init__(self, plan: tuple[Phase, ...] = DEFAULT_PLAN, slo: SLO = SLO()):
        if not plan:
            raise ValueError("plan must have at least one phase")
        self.plan = plan
        self.slo = slo
        self.index = 0
        self.status = Status.PROGRESSING
        self.history: list[tuple[str, Decision, str]] = []

    @property
    def phase(self) -> Phase:
        return self.plan[self.index]

    @property
    def target_weight(self) -> int:
        return 0 if self.status is Status.ROLLED_BACK else self.phase.target_weight

    def evaluate(self, window: WindowMetrics) -> tuple[Decision, str]:
        if self.status is not Status.PROGRESSING:
            raise RuntimeError(f"controller is {self.status.value}")

        if window.error_rate > self.slo.max_error_rate:
            decision, reason = Decision.ROLLBACK, (
                f"error rate {window.error_rate:.2%} > {self.slo.max_error_rate:.2%}"
            )
        elif window.p99_ms > self.slo.max_p99_ms:
            decision, reason = Decision.ROLLBACK, (
                f"p99 {window.p99_ms:.1f}ms > {self.slo.max_p99_ms:.1f}ms"
            )
        elif window.requests < self.phase.min_requests:
            decision, reason = Decision.HOLD, (
                f"{window.requests}/{self.phase.min_requests} requests observed"
            )
        else:
            decision, reason = Decision.PROMOTE, (
                f"p99 {window.p99_ms:.1f}ms, errors {window.error_rate:.2%} within SLO"
            )

        self.history.append((self.phase.name, decision, reason))
        if decision is Decision.ROLLBACK:
            self.status = Status.ROLLED_BACK
        elif decision is Decision.PROMOTE:
            if self.index == len(self.plan) - 1:
                self.status = Status.COMPLETED
            else:
                self.index += 1
        return decision, reason
