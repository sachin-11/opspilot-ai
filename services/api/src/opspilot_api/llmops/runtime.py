"""Governed provider: budget, circuit breaker, usage, and tracing around an existing provider.

This wraps a ``ModelProvider`` and plugs into the existing ``ProviderRouter`` unchanged, so retry
and fallback keep their current semantics:

- an open circuit raises ``RetryableProviderError`` (the router then tries its configured fallback);
- a hard budget cap raises a non-retryable ``ProviderError`` (no silent switch to another model);
- configuration/authentication errors are surfaced and never counted as provider health failures.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opspilot_api.investigations.providers import (
    ModelProvider,
    ModelResult,
    OutputT,
    ProviderConfigurationError,
    ProviderError,
    RetryableProviderError,
    StructuredOutputError,
)
from opspilot_api.llmops.breaker import CircuitBreaker
from opspilot_api.llmops.budget import BudgetExceeded, InMemoryBudgetStore
from opspilot_api.llmops.tracing import CompositeTracer, TraceRecord, content_hash
from opspilot_api.llmops.usage import PricingRate, UsageEvent, UsageLedger, estimate_cost


class BudgetBlocked(ProviderError):
    """Non-retryable on purpose: the router must not fall back to an unapproved model."""

    code = "budget_exceeded"


class CircuitOpenError(RetryableProviderError):
    code = "provider_circuit_open"


@dataclass(frozen=True)
class CallScope:
    workspace_id: str
    workflow: str
    scope_keys: tuple[str, ...]
    investigation_id: str | None = None
    remediation_id: str | None = None
    user_id: str | None = None
    routing_decision_id: str | None = None
    routing_policy_version: str | None = None
    prompt_version_id: str | None = None
    max_input_tokens: int = 50_000
    max_output_tokens: int = 10_000


class GovernedProvider(ModelProvider):
    def __init__(
        self,
        inner: ModelProvider,
        *,
        scope: CallScope,
        budget: InMemoryBudgetStore,
        breaker: CircuitBreaker,
        ledger: UsageLedger,
        tracer: CompositeTracer,
        rate: PricingRate | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.inner, self.scope, self.budget = inner, scope, budget
        self.breaker, self.ledger, self.tracer, self.rate = breaker, ledger, tracer, rate
        self.clock = clock
        self.provider_name, self.model_name = inner.provider_name, inner.model_name

    def _max_cost(self) -> float:
        if self.rate is None:
            return 0.0  # unknown rate: nothing can be priced, so nothing can be reserved honestly
        return (
            self.scope.max_input_tokens * self.rate.input_per_million
            + self.scope.max_output_tokens * self.rate.output_per_million
        ) / 1_000_000

    def generate(
        self, prompt_name: str, schema: type[OutputT], context: dict[str, Any]
    ) -> ModelResult:
        node = prompt_name
        trace = TraceRecord(
            trace_id=str(uuid.uuid4()),
            workspace_id=self.scope.workspace_id,
            workflow=self.scope.workflow,
            node=node,
            final_status="started",
            investigation_id=self.scope.investigation_id,
            remediation_id=self.scope.remediation_id,
            prompt_version_id=self.scope.prompt_version_id,
            prompt_hash=content_hash(prompt_name),
            routing_policy_version=self.scope.routing_policy_version,
            provider=self.provider_name,
            model=self.model_name,
        )
        if self.rate is None and self.provider_name != "fake":
            # Fail loudly: without a rate nothing can be reserved or priced honestly.
            raise ProviderConfigurationError(
                f"no pricing rate configured for {self.provider_name}/{self.model_name}"
            )
        if not self.breaker.allow():
            trace.final_status, trace.error_category = "failed", "circuit_open"
            self.tracer.emit(trace)
            raise CircuitOpenError(f"circuit open for {self.provider_name}")
        try:
            reservation = self.budget.reserve(
                list(self.scope.scope_keys), self._max_cost(), now=self.clock()
            )
        except BudgetExceeded as exc:
            trace.final_status, trace.error_category = "failed", "budget_exceeded"
            self.tracer.emit(trace)
            raise BudgetBlocked(str(exc)) from exc
        started = self.clock()
        try:
            result = self.inner.generate(prompt_name, schema, context)
        except ProviderConfigurationError:
            self.budget.release(reservation.id)
            self._fail(trace, "configuration_error", started, health=None)
            raise
        except StructuredOutputError:
            self.budget.release(reservation.id)
            self._fail(trace, "invalid_structured_output", started, health="success")
            raise
        except RetryableProviderError:
            self.budget.release(reservation.id)
            self._fail(trace, "retryable_provider_error", started, health="retryable_failure")
            raise
        latency_ms = (self.clock() - started) * 1000
        self.breaker.record("success", latency_ms)
        usage = result.usage
        cost = estimate_cost(self.rate, usage.input_tokens, usage.output_tokens)
        self.budget.reconcile(reservation.id, cost)
        event = UsageEvent(
            id=str(uuid.uuid4()),
            idempotency_key=f"{trace.trace_id}:{node}",
            workspace_id=self.scope.workspace_id,
            workflow=self.scope.workflow,
            node_name=node,
            provider=usage.provider,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
            status="succeeded",
            occurred_at=datetime.now(UTC),
            pricing_rate_version=self.rate.version if self.rate else None,
            estimated_cost_usd=cost,
            user_id=self.scope.user_id,
            investigation_id=self.scope.investigation_id,
            remediation_id=self.scope.remediation_id,
            routing_decision_id=self.scope.routing_decision_id,
            trace_id=trace.trace_id,
        )
        self.ledger.append(event)
        trace.final_status, trace.structured_output_valid = "succeeded", True
        trace.input_tokens, trace.output_tokens = usage.input_tokens, usage.output_tokens
        trace.latency_ms, trace.estimated_cost_usd = latency_ms, cost
        self.tracer.emit(trace)
        return result

    def _fail(self, trace: TraceRecord, category: str, started: float, health: str | None) -> None:
        latency_ms = (self.clock() - started) * 1000
        if health == "retryable_failure":
            self.breaker.record("retryable_failure", latency_ms)
        elif health == "success":
            self.breaker.record("success", latency_ms)  # the provider answered; output was bad
        trace.final_status, trace.error_category, trace.latency_ms = "failed", category, latency_ms
        self.tracer.emit(trace)
