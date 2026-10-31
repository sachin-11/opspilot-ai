"""Append-only usage ledger and versioned pricing.

Cost is only estimated when both token counts and a rate are known; otherwise it is ``None`` (the
dashboard shows "unavailable"). A price change is a new rate version, and every usage event keeps
the version it was costed with, so history is never recomputed or rewritten.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PricingRate:
    version: str
    provider: str
    model: str
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None


def estimate_cost(
    rate: PricingRate | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None = None,
) -> float | None:
    if rate is None or input_tokens is None or output_tokens is None:
        return None
    cached = cached_input_tokens or 0
    if cached and rate.cached_input_per_million is None:
        return None  # cached tokens present but no cached rate: refuse to guess
    fresh = input_tokens - cached
    if fresh < 0:
        return None
    total = fresh * rate.input_per_million + output_tokens * rate.output_per_million
    if cached:
        total += cached * (rate.cached_input_per_million or 0.0)
    return round(total / 1_000_000, 10)


@dataclass(frozen=True)
class UsageEvent:
    id: str
    idempotency_key: str  # provider request id, or the internal usage id
    workspace_id: str
    workflow: str
    node_name: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    status: str
    occurred_at: datetime
    pricing_rate_version: str | None = None
    cached_input_tokens: int | None = None
    request_count: int = 1
    estimated_cost_usd: float | None = None
    user_id: str | None = None
    api_key_id: str | None = None
    incident_id: str | None = None
    investigation_id: str | None = None
    remediation_id: str | None = None
    routing_decision_id: str | None = None
    trace_id: str | None = None


@dataclass
class UsageLedger:
    """In-memory model of the append-only table (the SQL table has a unique idempotency index
    and database triggers that reject UPDATE and DELETE)."""

    _events: dict[str, UsageEvent] = field(default_factory=dict)

    def append(self, event: UsageEvent) -> tuple[UsageEvent, bool]:
        """Return (stored event, inserted). A repeated key returns the original, unchanged."""
        existing = self._events.get(event.idempotency_key)
        if existing is not None:
            return existing, False
        self._events[event.idempotency_key] = event
        return event, True

    def events(self) -> list[UsageEvent]:
        return list(self._events.values())

    def aggregate(self, key: str) -> dict[str, dict[str, Any]]:
        """Group without touching source rows. ``key`` is any UsageEvent attribute."""
        out: dict[str, dict[str, Any]] = {}
        for e in self._events.values():
            bucket = out.setdefault(
                str(getattr(e, key)),
                {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "cost_unavailable_requests": 0,
                },
            )
            bucket["requests"] += e.request_count
            bucket["input_tokens"] += e.input_tokens
            bucket["output_tokens"] += e.output_tokens
            if e.estimated_cost_usd is None:
                bucket["cost_unavailable_requests"] += e.request_count
            else:
                bucket["cost_usd"] += e.estimated_cost_usd
        return out
