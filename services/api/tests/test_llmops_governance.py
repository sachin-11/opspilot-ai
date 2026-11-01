"""Governance wiring for the investigation worker: usage events, breaker, and cost (no network)."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine

from opspilot_api.investigations.anthropic_provider import AnthropicProvider
from opspilot_api.investigations.providers import (
    FakeProvider,
    ProviderConfigurationError,
    ProviderRouter,
)
from opspilot_api.investigations.schemas import TriageResult
from opspilot_api.llmops.breaker import InMemoryBreakerStore
from opspilot_api.llmops.governance import GovernanceFactory
from opspilot_api.llmops.usage import UsageLedger

from .conftest import make_settings

TRIAGE = {
    "category": "outage",
    "impact_assessment": "Errors for some users.",
    "urgency": "high",
    "affected_component": "payments-worker",
    "initial_questions": ["What changed?"],
    "evidence_needed": ["logs"],
}


def _factory(**overrides: object) -> tuple[GovernanceFactory, UsageLedger, InMemoryBreakerStore]:
    ledger, store = UsageLedger(), InMemoryBreakerStore()
    engine = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")  # never connected
    factory = GovernanceFactory(
        make_settings(**overrides), engine, ledger=ledger, breaker_store=store
    )
    return factory, ledger, store


def test_governed_fake_call_writes_a_usage_event_scoped_to_the_investigation() -> None:
    factory, ledger, _ = _factory(AI_INPUT_PRICE_PER_MILLION=1, AI_OUTPUT_PRICE_PER_MILLION=5)
    workspace_id, investigation_id = uuid4(), uuid4()
    router = factory.router_for(workspace_id, investigation_id)
    result = router.generate("triage", TriageResult, {"incident": {"title": "x"}})
    assert isinstance(result.output, TriageResult)
    (event,) = ledger.events()
    assert event.workflow == "investigation" and event.node_name == "triage"
    assert event.workspace_id == str(workspace_id)
    assert event.investigation_id == str(investigation_id)
    assert (event.provider, event.status) == ("fake", "succeeded")
    assert (event.input_tokens, event.output_tokens) == (120, 80)
    assert event.estimated_cost_usd == pytest.approx((120 * 1 + 80 * 5) / 1_000_000)


def test_each_call_is_a_separate_idempotent_event() -> None:
    factory, ledger, _ = _factory()
    router = factory.router_for(uuid4(), uuid4())
    router.generate("triage", TriageResult, {})
    router.generate("triage", TriageResult, {})
    assert len(ledger.events()) == 2
    assert len({e.idempotency_key for e in ledger.events()}) == 2


def test_real_provider_without_prices_fails_loudly_instead_of_unpriced_spend() -> None:
    factory, ledger, _ = _factory(AI_PRIMARY_PROVIDER="anthropic", AI_PRIMARY_API_KEY="sk-ant-x")
    factory.settings.AI_INPUT_PRICE_PER_MILLION = None
    scripted = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_: pytest.fail("provider must not be called"))
    )
    inner = AnthropicProvider("claude-haiku-5-5", SecretStr("sk-ant-x"), client=scripted)
    governed = factory._govern(inner, uuid4(), uuid4())
    with pytest.raises(ProviderConfigurationError, match="no pricing rate"):
        governed.generate("triage", TriageResult, {})
    assert ledger.events() == []


def test_governed_anthropic_call_records_tokens_and_cost() -> None:
    factory, ledger, _ = _factory(AI_INPUT_PRICE_PER_MILLION=0.1, AI_OUTPUT_PRICE_PER_MILLION=0.5)
    calls: list[Any] = []

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(TRIAGE))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            stop_reason="end_turn",
        )

    inner = AnthropicProvider(
        "claude-haiku-5-5",
        SecretStr("sk-ant-x"),
        client=SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    router = ProviderRouter(
        factory.settings,
        primary=factory._govern(inner, uuid4(), uuid4()),
    )
    router.generate("triage", TriageResult, {})
    (event,) = ledger.events()
    assert (event.provider, event.model) == ("anthropic", "claude-haiku-5-5")
    assert (event.input_tokens, event.output_tokens) == (1000, 200)
    assert event.estimated_cost_usd == pytest.approx((1000 * 0.1 + 200 * 0.5) / 1_000_000)
    assert len(calls) == 1


def test_fake_provider_is_unchanged_by_governance() -> None:
    factory, _, _ = _factory()
    router = factory.router_for(uuid4(), uuid4())
    assert router.primary.provider_name == "fake" and router.fallback is None
    assert isinstance(FakeProvider(), FakeProvider)
