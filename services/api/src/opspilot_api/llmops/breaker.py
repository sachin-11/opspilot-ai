"""Provider circuit breakers with shared state and an injectable clock.

States: closed -> open (on threshold) -> half_open (after the open window) -> closed (probes
succeed) or open (a probe fails). Configuration/authentication errors are surfaced to the caller
and never counted as provider health failures. State lives in a shared store so every API and
worker process sees the same breaker; the Redis store uses WATCH/MULTI compare-and-set.
"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

State = Literal["closed", "open", "half_open"]
Outcome = Literal["success", "retryable_failure", "timeout", "rate_limited", "config_error"]


class CircuitOpen(Exception):
    code = "provider_circuit_open"


@dataclass(frozen=True)
class BreakerConfig:
    consecutive_failures: int = 5
    window_seconds: float = 60.0
    min_requests: int = 10
    error_rate: float = 0.5
    timeout_rate: float = 0.4
    latency_threshold_ms: float | None = None
    open_seconds: float = 30.0
    half_open_probes: int = 2
    probes_to_close: int = 2


@dataclass
class BreakerState:
    state: State = "closed"
    opened_at: float = 0.0
    consecutive_failures: int = 0
    probes_issued: int = 0
    probe_successes: int = 0
    window: list[tuple[float, str, float]] = field(default_factory=list)  # (ts, outcome, ms)
    version: int = 0


class BreakerStore(Protocol):
    def load(self, key: str) -> BreakerState: ...
    def compare_and_set(self, key: str, expected_version: int, new: BreakerState) -> bool: ...


class InMemoryBreakerStore:
    def __init__(self) -> None:
        self._data: dict[str, BreakerState] = {}

    def load(self, key: str) -> BreakerState:
        current = self._data.get(key)
        return BreakerState() if current is None else _clone(current)

    def compare_and_set(self, key: str, expected_version: int, new: BreakerState) -> bool:
        current = self._data.get(key)
        if (current.version if current else 0) != expected_version:
            return False
        stored = _clone(new)
        stored.version = expected_version + 1
        self._data[key] = stored
        return True


class RedisBreakerStore:
    """Shared across processes. Requires a ``redis.Redis``-compatible client."""

    def __init__(self, client: Any, prefix: str = "opspilot:breaker:") -> None:
        self._client, self._prefix = client, prefix

    def load(self, key: str) -> BreakerState:
        raw = self._client.get(self._prefix + key)
        if not raw:
            return BreakerState()
        data = json.loads(raw)
        data["window"] = [tuple(x) for x in data["window"]]
        return BreakerState(**data)

    def compare_and_set(self, key: str, expected_version: int, new: BreakerState) -> bool:
        import redis  # local import keeps the unit tests free of a live Redis

        name = self._prefix + key
        with self._client.pipeline() as pipe:
            try:
                pipe.watch(name)
                raw = pipe.get(name)
                current = json.loads(raw)["version"] if raw else 0
                if current != expected_version:
                    return False
                stored = _clone(new)
                stored.version = expected_version + 1
                pipe.multi()
                pipe.set(name, json.dumps(asdict(stored)), ex=3600)
                pipe.execute()
                return True
            except redis.WatchError:
                return False


def _clone(state: BreakerState) -> BreakerState:
    return BreakerState(**{**asdict(state), "window": [tuple(x) for x in state.window]})


TransitionHook = Callable[[str, State, State, str], None]  # key, old, new, reason


class CircuitBreaker:
    def __init__(
        self,
        key: str,
        store: BreakerStore,
        config: BreakerConfig | None = None,
        *,
        clock: Callable[[], float],
        on_transition: TransitionHook | None = None,
    ) -> None:
        self.key, self.store, self.config = key, store, config or BreakerConfig()
        self.clock, self._hook = clock, on_transition

    def _mutate(self, fn: Callable[[BreakerState], tuple[State, str] | None]) -> BreakerState:
        for _ in range(20):  # CAS retry under contention
            state = self.store.load(self.key)
            before = state.state
            change = fn(state)
            if self.store.compare_and_set(self.key, state.version, state):
                if change and self._hook and change[0] != before:
                    self._hook(self.key, before, change[0], change[1])
                return state
        raise RuntimeError("circuit breaker state update contention")

    def current_state(self) -> State:
        state = self.store.load(self.key)
        if state.state == "open" and self.clock() - state.opened_at >= self.config.open_seconds:
            return "half_open"
        return state.state

    def allow(self) -> bool:
        """Reserve permission for one call. In half_open only a bounded number may probe."""
        allowed = False

        def step(s: BreakerState) -> tuple[State, str] | None:
            nonlocal allowed
            now = self.clock()
            if s.state == "open" and now - s.opened_at >= self.config.open_seconds:
                s.state, s.probes_issued, s.probe_successes = "half_open", 0, 0
                allowed_change: tuple[State, str] | None = ("half_open", "open window elapsed")
            else:
                allowed_change = None
            if s.state == "closed":
                allowed = True
            elif s.state == "half_open" and s.probes_issued < self.config.half_open_probes:
                s.probes_issued += 1
                allowed = True
            return allowed_change

        self._mutate(step)
        return allowed

    def guard(self) -> None:
        if not self.allow():
            raise CircuitOpen(f"circuit open for {self.key}")

    def record(self, outcome: Outcome, latency_ms: float = 0.0) -> None:
        if outcome == "config_error":
            return  # surfaced to the caller; not a temporary provider-health failure

        def step(s: BreakerState) -> tuple[State, str] | None:
            now, cfg = self.clock(), self.config
            s.window = [e for e in s.window if now - e[0] <= cfg.window_seconds]
            s.window.append((now, outcome, latency_ms))
            failed = outcome != "success"
            s.consecutive_failures = s.consecutive_failures + 1 if failed else 0
            if s.state == "half_open":
                if failed:
                    s.state, s.opened_at = "open", now
                    return ("open", f"probe failed: {outcome}")
                s.probe_successes += 1
                if s.probe_successes >= cfg.probes_to_close:
                    s.state, s.consecutive_failures, s.window = "closed", 0, []
                    return ("closed", "probes succeeded")
                return None
            if s.state == "closed":
                reason = self._trip_reason(s, outcome)
                if reason:
                    s.state, s.opened_at = "open", now
                    return ("open", reason)
            return None

        self._mutate(step)

    def _trip_reason(self, s: BreakerState, outcome: Outcome) -> str | None:
        cfg = self.config
        if outcome == "rate_limited":
            return "provider rate limiting"
        if s.consecutive_failures >= cfg.consecutive_failures:
            return f"{s.consecutive_failures} consecutive failures"
        n = len(s.window)
        if n >= cfg.min_requests:
            errors = sum(1 for _, o, _ in s.window if o != "success")
            timeouts = sum(1 for _, o, _ in s.window if o == "timeout")
            if errors / n >= cfg.error_rate:
                return f"error rate {errors / n:.2f} over window"
            if timeouts / n >= cfg.timeout_rate:
                return f"timeout rate {timeouts / n:.2f} over window"
            if cfg.latency_threshold_ms is not None:
                mean = sum(ms for _, _, ms in s.window) / n
                if mean >= cfg.latency_threshold_ms:
                    return f"mean latency {mean:.0f}ms over window"
        return None
