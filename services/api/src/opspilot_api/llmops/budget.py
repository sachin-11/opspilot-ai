"""Atomic budget reservations.

Flow: estimate the maximum call cost -> ``reserve`` (atomic, all scopes or none) -> call the
provider -> ``reconcile`` with actual usage (unused reservation is released) -> stale reservations
are expired by ``expire_stale``. A hard cap blocks new calls; completed results stay readable
because nothing here touches stored results. A budget failure never switches models.

``InMemoryBudgetStore`` serializes with a lock and defines the semantics. The PostgreSQL store
(``service.py``) provides the same guarantee with a single conditional ``UPDATE ... WHERE
spent + reserved + :amount <= hard_limit`` per scope inside one transaction.
"""

import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

Scope = Literal[
    "investigation", "remediation", "daily_workspace", "monthly_workspace", "user", "workflow"
]
Status = Literal["reserved", "reconciled", "released", "expired"]


class BudgetError(Exception):
    code = "budget_error"


class BudgetExceeded(BudgetError):
    code = "budget_exceeded"

    def __init__(self, scope_key: str, limit: float, would_be: float) -> None:
        super().__init__(f"hard budget cap for {scope_key} would be exceeded")
        self.scope_key, self.limit, self.would_be = scope_key, limit, would_be


@dataclass
class Budget:
    scope_key: str  # e.g. "daily_workspace:<workspace>:2026-10-31"
    scope: Scope
    hard_limit_usd: float
    soft_fraction: float = 0.8
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    unmetered_calls: int = 0

    @property
    def remaining_usd(self) -> float:
        return self.hard_limit_usd - self.spent_usd - self.reserved_usd


@dataclass
class Reservation:
    id: str
    scope_keys: tuple[str, ...]
    amount_usd: float
    expires_at: float
    status: Status = "reserved"
    actual_usd: float | None = None
    late: bool = False
    cost_unavailable: bool = False


@dataclass(frozen=True)
class BudgetEvent:
    kind: Literal["soft_warning", "hard_block", "override", "expired", "reconciled", "late_charge"]
    scope_key: str
    detail: str


class EventSink(Protocol):
    def __call__(self, event: BudgetEvent) -> None: ...


@dataclass
class InMemoryBudgetStore:
    budgets: dict[str, Budget] = field(default_factory=dict)
    reservations: dict[str, Reservation] = field(default_factory=dict)
    events: list[BudgetEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_budget(self, budget: Budget) -> None:
        self.budgets[budget.scope_key] = budget

    def _emit(self, event: BudgetEvent) -> None:
        self.events.append(event)

    def reserve(
        self, scope_keys: list[str], amount_usd: float, *, now: float, ttl_seconds: float = 900
    ) -> Reservation:
        if amount_usd < 0:
            raise BudgetError("reservation amount must be non-negative")
        with self._lock:  # all scopes are checked, then all are charged, as one step
            applicable = [self.budgets[k] for k in scope_keys if k in self.budgets]
            for b in applicable:
                would_be = b.spent_usd + b.reserved_usd + amount_usd
                if would_be > b.hard_limit_usd + 1e-12:
                    self._emit(BudgetEvent("hard_block", b.scope_key, f"{would_be:.6f}"))
                    raise BudgetExceeded(b.scope_key, b.hard_limit_usd, would_be)
            for b in applicable:
                before = b.spent_usd + b.reserved_usd
                b.reserved_usd += amount_usd
                threshold = b.hard_limit_usd * b.soft_fraction
                if before < threshold <= b.spent_usd + b.reserved_usd:
                    self._emit(BudgetEvent("soft_warning", b.scope_key, f"threshold {threshold}"))
            reservation = Reservation(
                id=str(uuid.uuid4()),
                scope_keys=tuple(b.scope_key for b in applicable),
                amount_usd=amount_usd,
                expires_at=now + ttl_seconds,
            )
            self.reservations[reservation.id] = reservation
            return reservation

    def reconcile(self, reservation_id: str, actual_usd: float | None) -> Reservation:
        """Idempotent. ``None`` means the cost is unknown: the reserved ceiling is kept as spend
        (an upper bound, not a guess of the real cost) and the call is flagged unmetered."""
        with self._lock:
            res = self.reservations[reservation_id]
            if res.status in ("reconciled", "released"):
                return res
            charge = res.amount_usd if actual_usd is None else actual_usd
            for key in res.scope_keys:
                b = self.budgets[key]
                if res.status == "reserved":
                    b.reserved_usd = max(0.0, b.reserved_usd - res.amount_usd)
                b.spent_usd += charge
                b.unmetered_calls += 1 if actual_usd is None else 0
            if res.status == "expired":
                res.late = True
                self._emit(BudgetEvent("late_charge", ",".join(res.scope_keys), f"{charge:.6f}"))
            res.status = "reconciled"
            res.actual_usd = charge
            res.cost_unavailable = actual_usd is None
            return res

    def release(self, reservation_id: str) -> Reservation:
        """The call never happened (for example it failed before reaching the provider)."""
        with self._lock:
            res = self.reservations[reservation_id]
            if res.status != "reserved":
                return res
            for key in res.scope_keys:
                b = self.budgets[key]
                b.reserved_usd = max(0.0, b.reserved_usd - res.amount_usd)
            res.status = "released"
            return res

    def expire_stale(self, *, now: float) -> list[Reservation]:
        with self._lock:
            expired = [
                r
                for r in self.reservations.values()
                if r.status == "reserved" and r.expires_at <= now
            ]
            for res in expired:
                for key in res.scope_keys:
                    b = self.budgets[key]
                    b.reserved_usd = max(0.0, b.reserved_usd - res.amount_usd)
                res.status = "expired"
                self._emit(BudgetEvent("expired", ",".join(res.scope_keys), res.id))
            return expired

    def raise_limit(
        self, scope_key: str, new_limit_usd: float, *, approver: str, reason: str
    ) -> None:
        """High-risk override; the caller must audit it (the service layer does)."""
        if not reason.strip() or not approver:
            raise BudgetError("an override needs an approver and a reason")
        with self._lock:
            self.budgets[scope_key].hard_limit_usd = new_limit_usd
            self._emit(BudgetEvent("override", scope_key, f"{approver}: {reason[:80]}"))

    def reconciliation_report(self, ledger_total_by_scope: dict[str, float]) -> dict[str, float]:
        """Drift between budget counters and the append-only usage ledger (counter - ledger)."""
        with self._lock:
            return {
                k: round(b.spent_usd - ledger_total_by_scope.get(k, 0.0), 8)
                for k, b in self.budgets.items()
            }
