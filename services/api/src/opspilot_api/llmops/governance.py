"""Governed provider wiring for the synchronous investigation worker.

Wraps the configured primary (and fallback) providers in ``GovernedProvider`` so every model call
writes an append-only usage event, updates the shared circuit breaker, and emits a trace. The
router keeps its existing retry and fallback behaviour. Budget limits stay with the workflow's own
per-investigation caps; no budget scope is attached here.
"""

import logging
import time
from uuid import UUID

from redis import Redis
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.investigations.providers import ModelProvider, ProviderRouter
from opspilot_api.llmops.breaker import BreakerStore, CircuitBreaker, RedisBreakerStore
from opspilot_api.llmops.budget import InMemoryBudgetStore
from opspilot_api.llmops.models import UsageEventRow
from opspilot_api.llmops.runtime import CallScope, GovernedProvider
from opspilot_api.llmops.tracing import CompositeTracer, build_tracer
from opspilot_api.llmops.usage import PricingRate, UsageEvent, UsageLedger
from opspilot_api.llmops.usage_pg import usage_row_values

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "investigation"


class SyncPostgresUsageLedger(UsageLedger):
    """In-process ledger that also inserts each new event into ``usage_events`` (idempotent).

    A storage failure is logged by error class and does not discard the model result that was
    already paid for; the investigation keeps its own token totals.
    """

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self._engine = engine

    def append(self, event: UsageEvent) -> tuple[UsageEvent, bool]:
        stored, inserted = super().append(event)
        if inserted:
            try:
                with Session(self._engine) as session:
                    session.execute(
                        pg_insert(UsageEventRow)
                        .values(**usage_row_values(event))
                        .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    )
                    session.commit()
            except Exception as exc:
                logger.error("usage event not stored", extra={"error_class": type(exc).__name__})
        return stored, inserted


class GovernanceFactory:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        *,
        ledger: UsageLedger | None = None,
        breaker_store: BreakerStore | None = None,
        tracer: CompositeTracer | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger if ledger is not None else SyncPostgresUsageLedger(engine)
        self.budget = InMemoryBudgetStore()
        langsmith_key = settings.LANGSMITH_API_KEY
        self.tracer = tracer or build_tracer(
            langsmith_enabled=settings.LANGSMITH_TRACING,
            langsmith_key=langsmith_key.get_secret_value() if langsmith_key else None,
        )
        self._redis: Redis | None = None
        if breaker_store is None:
            self._redis = Redis.from_url(
                settings.REDIS_URL.get_secret_value(),
                socket_timeout=settings.DEPENDENCY_TIMEOUT_SECONDS,
            )
            breaker_store = RedisBreakerStore(self._redis)
        self._breaker_store = breaker_store

    def close(self) -> None:
        if self._redis is not None:
            self._redis.close()

    def _rate(self, provider: ModelProvider) -> PricingRate | None:
        price_in = self.settings.AI_INPUT_PRICE_PER_MILLION
        price_out = self.settings.AI_OUTPUT_PRICE_PER_MILLION
        if price_in is None or price_out is None:
            return None
        return PricingRate(
            "settings", provider.provider_name, provider.model_name, price_in, price_out
        )

    def _govern(
        self, provider: ModelProvider, workspace_id: UUID, investigation_id: UUID
    ) -> GovernedProvider:
        scope = CallScope(
            workspace_id=str(workspace_id),
            workflow=WORKFLOW_NAME,
            scope_keys=(),
            investigation_id=str(investigation_id),
            max_input_tokens=self.settings.AI_MAX_INPUT_TOKENS,
            max_output_tokens=self.settings.AI_MAX_OUTPUT_TOKENS,
        )
        breaker = CircuitBreaker(provider.provider_name, self._breaker_store, clock=time.time)
        return GovernedProvider(
            provider,
            scope=scope,
            budget=self.budget,
            breaker=breaker,
            ledger=self.ledger,
            tracer=self.tracer,
            rate=self._rate(provider),
        )

    def router_for(self, workspace_id: UUID, investigation_id: UUID) -> ProviderRouter:
        base = ProviderRouter(self.settings)
        return ProviderRouter(
            self.settings,
            primary=self._govern(base.primary, workspace_id, investigation_id),
            fallback=(
                self._govern(base.fallback, workspace_id, investigation_id)
                if base.fallback is not None
                else None
            ),
        )
