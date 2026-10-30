"""Ingress backpressure with honest status codes and observable activation/recovery.

Policy (evaluated before a signal is accepted; nothing is buffered in memory):
  * per-source or per-workspace rate above the configured limit        -> HTTP 429 + Retry-After
  * rate-limit state (Redis) unavailable                               -> HTTP 503 (fail closed)
  * broker unreachable (no durable acceptance possible)                -> HTTP 503
  * consumer lag above ``OBS_LAG_THRESHOLD`` (downstream unhealthy)    -> HTTP 503
  * workspace dead letters in the last 5 minutes at the threshold     -> HTTP 503 for that workspace
Pressure clears automatically as soon as the condition stops; transitions are published once.
"""

import logging
import time
from dataclasses import dataclass
from uuid import UUID

from redis import Redis
from redis.exceptions import RedisError

from opspilot_api.config import Settings
from opspilot_api.observability.bus import EventBus, Topics
from opspilot_api.observability.events import publish_observability_event
from opspilot_api.observability.telemetry import BACKPRESSURE_ACTIVATIONS, CONSUMER_LAG

logger = logging.getLogger(__name__)
RAW_GROUP = "opspilot-raw-consumer"
NORMALIZED_GROUP = "opspilot-normalized-consumer"
DEAD_LETTER_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status_code: int = 202
    reason: str | None = None
    retry_after: int = 1


def consumer_lag(bus: EventBus, topics: Topics) -> int | None:
    raw = bus.lag(RAW_GROUP, [topics.raw])
    normalized = bus.lag(NORMALIZED_GROUP, [topics.normalized])
    if raw is None or normalized is None:
        return None
    CONSUMER_LAG.labels(group=RAW_GROUP).set(raw)
    CONSUMER_LAG.labels(group=NORMALIZED_GROUP).set(normalized)
    return raw + normalized


class Backpressure:
    def __init__(self, settings: Settings, bus: EventBus, redis: Redis) -> None:
        self.settings = settings
        self.bus = bus
        self.redis = redis
        self.topics = Topics.from_settings(settings)

    def _rate_exceeded(self, key: str, limit: int) -> bool:
        window = int(time.time())
        redis_key = f"opspilot:obs:rl:{key}:{window}"
        pipe = self.redis.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, 3)
        count = int(pipe.execute()[0])
        return count > limit

    def _note(self, scope: str, workspace_id: UUID, reason: str | None) -> None:
        """Publish started/cleared exactly once per transition (state kept in Redis)."""
        key = f"opspilot:obs:bp:{scope}:{workspace_id}"
        try:
            if reason is not None:
                if self.redis.set(key, reason, nx=True, ex=60):
                    BACKPRESSURE_ACTIVATIONS.labels(reason=reason).inc()
                    publish_observability_event(
                        self.settings, workspace_id, "observability.backpressure_started",
                        {"reason": reason},
                    )  # fmt: skip
                else:
                    self.redis.expire(key, 60)
            elif self.redis.delete(key):
                publish_observability_event(
                    self.settings, workspace_id, "observability.backpressure_cleared", {}
                )
        except RedisError:
            logger.warning("backpressure state unavailable")

    def check(self, workspace_id: UUID, source_id: UUID, dead_letters_recent: int) -> Decision:
        try:
            if self._rate_exceeded(f"source:{source_id}", self.settings.OBS_SOURCE_RATE_PER_SECOND):
                self._note("rate", workspace_id, "source_rate_limit")
                return Decision(False, 429, "source_rate_limit", 1)
            if self._rate_exceeded(
                f"workspace:{workspace_id}", self.settings.OBS_WORKSPACE_RATE_PER_SECOND
            ):
                self._note("rate", workspace_id, "workspace_rate_limit")
                return Decision(False, 429, "workspace_rate_limit", 1)
            self._note("rate", workspace_id, None)
        except RedisError:
            BACKPRESSURE_ACTIVATIONS.labels(reason="rate_state_unavailable").inc()
            return Decision(False, 503, "rate_limit_state_unavailable", 5)
        if not self.bus.health():
            self._note("downstream", workspace_id, "broker_unavailable")
            return Decision(False, 503, "broker_unavailable", 5)
        lag = consumer_lag(self.bus, self.topics)
        if lag is not None and lag > self.settings.OBS_LAG_THRESHOLD:
            self._note("downstream", workspace_id, "consumer_lag")
            return Decision(False, 503, "consumer_lag", 10)
        if dead_letters_recent >= self.settings.OBS_DEAD_LETTER_RATE_THRESHOLD:
            self._note("downstream", workspace_id, "dead_letter_rate")
            return Decision(False, 503, "dead_letter_rate", 30)
        self._note("downstream", workspace_id, None)
        return Decision(True)

    def active_reasons(self, workspace_id: UUID) -> list[str]:
        reasons: list[str] = []
        try:
            for scope in ("rate", "downstream"):
                value = self.redis.get(f"opspilot:obs:bp:{scope}:{workspace_id}")
                if value:
                    reasons.append(value.decode() if isinstance(value, bytes) else str(value))
        except RedisError:
            reasons.append("rate_limit_state_unavailable")
        return reasons
