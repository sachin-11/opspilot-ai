"""AnthropicProvider: structured output, repair, redaction, and error mapping (no network)."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from opspilot_api.investigations.anthropic_provider import AnthropicProvider
from opspilot_api.investigations.providers import (
    FakeProvider,
    ProviderConfigurationError,
    RetryableProviderError,
    StructuredOutputError,
    UnsupportedProvider,
    create_provider,
)
from opspilot_api.investigations.schemas import HypothesisSet, TriageResult

TRIAGE = {
    "category": "outage",
    "impact_assessment": "Checkout errors for some users.",
    "urgency": "high",
    "affected_component": "payments-worker",
    "initial_questions": ["What changed?"],
    "evidence_needed": ["error logs"],
}
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class ScriptedMessages:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _reply(text: str, *, tokens: tuple[int, int] = (100, 20), stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
        stop_reason=stop_reason,
    )


def _provider(*replies: Any) -> tuple[AnthropicProvider, ScriptedMessages]:
    messages = ScriptedMessages(*replies)
    provider = AnthropicProvider(
        "claude-haiku-5-5",
        SecretStr("sk-ant-test-key"),
        client=SimpleNamespace(messages=messages),
    )
    return provider, messages


def test_returns_validated_output_and_usage() -> None:
    provider, messages = _provider(_reply(json.dumps(TRIAGE), tokens=(321, 45)))
    result = provider.generate("triage", TriageResult, {"incident": {"title": "5xx spike"}})
    assert isinstance(result.output, TriageResult)
    assert result.output.urgency == "high"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (321, 45)
    assert (result.usage.provider, result.usage.model) == ("anthropic", "claude-haiku-5-5")
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "temperature" not in call and "thinking" not in call
    assert "TriageResult" in call["system"] and "JSON Schema" in call["system"]


def test_markdown_fences_are_tolerated() -> None:
    provider, _ = _provider(_reply("```json\n" + json.dumps(TRIAGE) + "\n```"))
    assert provider.generate("triage", TriageResult, {}).output.model_dump()["category"] == "outage"


def test_context_is_redacted_and_fenced_as_untrusted() -> None:
    provider, messages = _provider(_reply(json.dumps(TRIAGE)))
    context = {"incident": {"title": "login failed password=hunter2", "api_key": "sk-live-1"}}
    provider.generate("triage", TriageResult, context)
    user = messages.calls[0]["messages"][0]["content"]
    assert "untrusted DATA" in user and "<untrusted_incident_data>" in user
    assert "hunter2" not in user and "sk-live-1" not in user
    assert "sk-ant-test-key" not in user and "sk-ant-test-key" not in messages.calls[0]["system"]


def test_one_repair_attempt_sums_usage() -> None:
    provider, messages = _provider(
        _reply("{}", tokens=(100, 5)), _reply(json.dumps(TRIAGE), tokens=(130, 40))
    )
    result = provider.generate("triage", TriageResult, {})
    assert (result.usage.input_tokens, result.usage.output_tokens) == (230, 45)
    assert "failed schema validation" in messages.calls[1]["messages"][0]["content"]


def test_second_invalid_reply_raises_structured_output_error() -> None:
    provider, messages = _provider(_reply("not json"), _reply("still not json"))
    with pytest.raises(StructuredOutputError):
        provider.generate("triage", TriageResult, {})
    assert len(messages.calls) == 2


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_truncation_are_not_parsed(stop_reason: str) -> None:
    provider, messages = _provider(_reply(json.dumps(TRIAGE), stop_reason=stop_reason))
    with pytest.raises(StructuredOutputError):
        provider.generate("triage", TriageResult, {})
    assert len(messages.calls) == 1


def test_evidence_uuid_citations_round_trip() -> None:
    evidence_id = str(uuid4())
    body = {
        "hypotheses": [
            {
                "description": "Connection pool exhaustion",
                "supporting_evidence_ids": [evidence_id],
                "contradicting_evidence_ids": [],
                "confidence": 0.6,
                "missing_evidence": [],
                "suggested_validation": "Check pool metrics.",
            }
        ]
    }
    provider, _ = _provider(_reply(json.dumps(body)))
    result = provider.generate("hypotheses", HypothesisSet, {"evidence_ids": [evidence_id]})
    assert isinstance(result.output, HypothesisSet)
    assert str(result.output.hypotheses[0].supporting_evidence_ids[0]) == evidence_id


def _status_error(status: int) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        "boom sk-ant-test-key", response=httpx2.Response(status, request=REQUEST), body=None
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(429), RetryableProviderError),
        (_status_error(500), RetryableProviderError),
        (_status_error(529), RetryableProviderError),
        (anthropic.APIConnectionError(request=REQUEST), RetryableProviderError),
        (anthropic.APITimeoutError(request=REQUEST), RetryableProviderError),
        (_status_error(401), ProviderConfigurationError),
        (_status_error(400), ProviderConfigurationError),
        (_status_error(404), ProviderConfigurationError),
    ],
)
def test_api_errors_map_to_provider_taxonomy(error: Exception, expected: type[Exception]) -> None:
    provider, _ = _provider(error)
    with pytest.raises(expected) as raised:
        provider.generate("triage", TriageResult, {})
    assert "sk-ant-test-key" not in str(raised.value)


def test_create_provider_wires_anthropic_and_fails_loudly_without_a_key() -> None:
    created = create_provider("anthropic", "claude-haiku-5-5", api_key=SecretStr("sk-ant-x"))
    assert isinstance(created, AnthropicProvider)
    with pytest.raises(ProviderConfigurationError):
        create_provider("anthropic", "claude-haiku-5-5")
    assert isinstance(create_provider("fake", "deterministic-v1"), FakeProvider)
    assert isinstance(create_provider("mystery", "m"), UnsupportedProvider)
