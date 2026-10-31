"""Redis token-bucket rate limiting (atomic Lua) with explicit fail-open/fail-closed behavior.

Keys are namespaced, hashed, and expire when idle, so key creation is bounded by the activity
window. Read-only endpoints fail open if Redis is unavailable (availability over throttling);
high-risk mutations fail closed. Internal key names are never returned to clients.
"""

import hashlib
import logging
import time
from dataclasses import dataclass

from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import RedisError

from opspilot_api.auth.context import RateLimitedError
from opspilot_api.config import Settings

logger = logging.getLogger(__name__)
RATE_ALLOWED = Counter("opspilot_rate_limit_allowed_total", "Allowed requests", ["rule"])
RATE_DENIED = Counter("opspilot_rate_limit_denied_total", "Denied requests", ["rule"])

# KEYS[1]=bucket, ARGV: capacity, refill_per_second, now_ms, cost
_LUA = """
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + math.max(0, now - ts) / 1000 * rate)
local allowed = 0
local retry = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry = math.ceil((cost - tokens) / rate)
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 60)
return {allowed, retry}
"""


@dataclass(frozen=True)
class Rule:
    name: str
    capacity: int
    per_minute: int  # sustained refill
    fail_closed: bool = False


RULES: dict[str, Rule] = {
    rule.name: rule
    for rule in (
        Rule("auth_failure", 20, 20),
        Rule("api_key", 120, 120),
        Rule("webhook", 300, 600),
        Rule("investigation_start", 6, 6, fail_closed=True),
        Rule("llm_operation", 10, 10, fail_closed=True),
        Rule("knowledge_search", 60, 120),
        Rule("repository_search", 60, 120),
        Rule("remediation_create", 6, 6, fail_closed=True),
        Rule("approval_attempt", 10, 10, fail_closed=True),
        Rule("ws_ticket", 20, 30),
        Rule("ws_connect", 20, 30),
        Rule("invitation", 10, 10, fail_closed=True),
        Rule("invitation_accept", 10, 10, fail_closed=True),
        Rule("mutation_default", 120, 240),
    )
}


class RateLimiter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self._script = redis.register_script(_LUA)

    @staticmethod
    def _key(rule: str, identity: str) -> str:
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return f"opspilot:rl:{rule}:{digest}"

    async def check(self, rule_name: str, identity: str, cost: int = 1) -> None:
        """Raise :class:`RateLimitedError` when the bucket is empty."""
        if not self.settings.AUTH_RATE_LIMIT_ENABLED:
            return
        rule = RULES[rule_name]
        scale = self.settings.AUTH_RATE_LIMIT_MULTIPLIER
        capacity = max(1, int(rule.capacity * scale))
        rate = max(0.001, rule.per_minute * scale / 60)
        try:
            allowed, retry = await self._script(
                keys=[self._key(rule.name, identity)],
                args=[capacity, rate, int(time.time() * 1000), cost],
            )
        except RedisError as exc:
            logger.warning("rate limit state unavailable", extra={"rule": rule.name})
            if rule.fail_closed:
                raise RateLimitedError(
                    "rate limiting is unavailable; try again shortly", 5
                ) from exc
            return
        if int(allowed) == 1:
            RATE_ALLOWED.labels(rule.name).inc()
            return
        RATE_DENIED.labels(rule.name).inc()
        raise RateLimitedError("too many requests", max(1, int(retry)))
