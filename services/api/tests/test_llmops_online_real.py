"""Online sampling, review queue, dataset promotion, and the guarded real-model tier (mocked)."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from opspilot_api.llmops import dataset as ds
from opspilot_api.llmops import online, real_eval
from opspilot_api.llmops.eval_types import EvalCaseSpec
from opspilot_api.llmops.golden_ops import golden_cases
from opspilot_api.llmops.runner import MEASURED, run_evaluation
from opspilot_api.llmops.usage import PricingRate

ON = online.OnlineEvalConfig(enabled=True, sample_rate=0.1)
OPTED = online.WorkspaceEvalPolicy(opted_in=True)


def _sampled(
    cfg: online.OnlineEvalConfig,
    pol: online.WorkspaceEvalPolicy,
    n: int = 4000,
    risk: str = "low",
    sensitive: bool = False,
) -> int:
    return sum(
        online.should_sample(cfg, pol, unit_id=f"u{i}", risk=risk, sensitive=sensitive)
        for i in range(n)
    )


def test_online_sampling_is_off_by_default_and_needs_workspace_opt_in() -> None:
    assert online.OnlineEvalConfig().enabled is False
    assert _sampled(online.OnlineEvalConfig(), OPTED) == 0
    assert _sampled(ON, online.WorkspaceEvalPolicy(opted_in=False)) == 0
    assert _sampled(online.OnlineEvalConfig(enabled=True, sample_rate=0.0), OPTED) == 0


def test_sampling_is_deterministic_and_bounded() -> None:
    first = [
        online.should_sample(ON, OPTED, unit_id=f"u{i}", risk="low", sensitive=False)
        for i in range(500)
    ]
    assert first == [
        online.should_sample(ON, OPTED, unit_id=f"u{i}", risk="low", sensitive=False)
        for i in range(500)
    ]
    assert 0.07 < _sampled(ON, OPTED) / 4000 < 0.13
    capped = online.OnlineEvalConfig(enabled=True, sample_rate=0.9)  # asks for far too much
    assert online.effective_rate(capped) == online.MAX_SAMPLE_RATE
    assert _sampled(capped, OPTED) / 4000 < 0.13


def test_high_risk_and_sensitive_runs_are_excluded_unless_allowed() -> None:
    assert _sampled(ON, OPTED, risk="high") == 0 and _sampled(ON, OPTED, risk="critical") == 0
    assert _sampled(ON, OPTED, sensitive=True) == 0
    allow_cfg = online.OnlineEvalConfig(enabled=True, sample_rate=0.1, allow_high_risk=True)
    allow_ws = online.WorkspaceEvalPolicy(opted_in=True, allow_high_risk=True)
    assert _sampled(allow_cfg, allow_ws, risk="high") > 0
    assert (
        _sampled(allow_cfg, OPTED, risk="high") == 0
    )  # both the platform and workspace must allow


def test_production_data_is_redacted_before_evaluation() -> None:
    clean = online.redact_for_evaluation(
        {"log": "login failed password=hunter2xyz9 user ops@example.com", "n": 3}
    )
    assert "hunter2xyz9" not in json.dumps(clean) and "ops@example.com" not in json.dumps(clean)
    with pytest.raises(ds.ProductionCaseRejected):  # redaction cannot clean a bare provider key
        online.redact_for_evaluation({"note": "sk-live-AbCdEf1234567890xyz"})


def test_failures_and_low_confidence_enter_the_review_queue() -> None:
    assert online.needs_review(passed=False, safety_failed=False, confidence=0.9)
    assert online.needs_review(passed=True, safety_failed=True, confidence=0.9)
    assert online.needs_review(passed=True, safety_failed=False, confidence=0.4)
    assert not online.needs_review(passed=True, safety_failed=False, confidence=0.9)
    assert not online.needs_review(passed=True, safety_failed=False, confidence=None)


def _prod_case(key: str, text: str) -> EvalCaseSpec:
    return EvalCaseSpec(
        case_key=key,
        suite="triage",
        split="test",
        input_data={
            "incident": {"title": text, "description": text, "severity": "sev2", "service": "x"}
        },
    )


def test_reviewed_cases_are_promoted_into_a_new_version_without_touching_the_old() -> None:
    existing = golden_cases()
    before = ds.dataset_hash(existing)
    promoted = online.promote_reviewed_cases(
        existing,
        [_prod_case("prod-1", "queue consumers stalled after config push")],
        authorized_by="user-1",
    )
    assert len(promoted) == len(existing) + 1 and ds.dataset_hash(existing) == before
    added = promoted[-1]
    assert added.source == "production_redacted" and added.split == "development"  # never test
    assert added.input_data["_provenance"]["authorized_by"] == "user-1"
    with pytest.raises(ds.DatasetError, match="authorizing"):
        online.promote_reviewed_cases(existing, [_prod_case("prod-2", "x y z")], authorized_by="")
    with pytest.raises(ds.DatasetError, match="already exists"):
        online.promote_reviewed_cases(
            existing, [_prod_case(existing[0].case_key, "dup")], authorized_by="u"
        )
    with pytest.raises(ds.ProductionCaseRejected):
        online.promote_reviewed_cases(
            existing, [_prod_case("prod-3", "contact ops@example.com now")], authorized_by="u"
        )


# ------------------------------------------------------------------ real-model tier (mocked)
RATE = PricingRate("test-rate", "anthropic", "any-model", 1.0, 5.0)


class FakeMessages:
    def __init__(
        self, payload: dict[str, Any] | str, tokens: tuple[int, int] = (1000, 200)
    ) -> None:
        self.payload, self.tokens = payload, tokens
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=self.tokens[0], output_tokens=self.tokens[1]),
        )


def _client(payload: dict[str, Any] | str, tokens: tuple[int, int] = (1000, 200)) -> Any:
    return SimpleNamespace(messages=FakeMessages(payload, tokens))


def _case(suite: str = "grounding") -> EvalCaseSpec:
    return next(
        c
        for c in golden_cases()
        if c.suite == suite and "secret" not in c.tags and "injection" not in c.tags
    )


def test_guard_requires_enable_credentials_and_manual_trigger() -> None:
    with pytest.raises(real_eval.RealEvalError, match="disabled"):
        real_eval.guard_environment({}, enabled=False, has_key=True)
    with pytest.raises(real_eval.RealEvalError, match="API key"):
        real_eval.guard_environment({}, enabled=True, has_key=False)
    with pytest.raises(real_eval.RealEvalError, match="workflow_dispatch"):
        real_eval.guard_environment(
            {"CI": "true", "GITHUB_EVENT_NAME": "push"}, enabled=True, has_key=True
        )
    with pytest.raises(real_eval.RealEvalError, match="pull requests"):
        real_eval.guard_environment(
            {"GITHUB_EVENT_NAME": "pull_request"}, enabled=True, has_key=True
        )
    real_eval.guard_environment(
        {"CI": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}, enabled=True, has_key=True
    )
    real_eval.guard_environment({}, enabled=True, has_key=True)  # a developer's own machine


def test_model_never_sees_grader_only_fields() -> None:
    case = next(c for c in golden_cases() if c.case_key.startswith("isolation-04"))
    message = real_eval.build_user_message(case)
    for hidden in (
        "valid_evidence_ids",
        "planted_secrets",
        "foreign_markers",
        "injected_tool_requests",
        "injection_canaries",
    ):
        assert hidden not in message
    assert "<untrusted_case_data>" in message  # evidence is delimited as untrusted data


def test_secrets_are_redacted_before_anything_is_sent() -> None:
    case = next(c for c in golden_cases() if c.case_key.startswith("isolation-08"))
    message = real_eval.build_user_message(case)
    assert "S3cretPw99" not in message and "postgres://reports" not in message


def test_unredactable_cases_are_refused_not_sent() -> None:
    case = EvalCaseSpec(
        case_key="unsafe-1",
        suite="grounding",
        split="development",
        input_data={"incident": {"title": "t", "description": "key sk-live-AbCdEf1234567890xyz"}},
    )
    client = _client({"answer": "x"})
    system = real_eval.AnthropicEvalSystem(client, model="m", rate=RATE, max_cost_usd=1.0)
    out = system(case)
    assert out.error_class == "refused_unredactable" and client.messages.calls == []
    assert system.refused == ["unsafe-1"]


def test_real_call_maps_structured_output_and_prices_from_usage() -> None:
    case = _case()
    ids = case.required_evidence
    client = _client({"abstained": False, "answer": "pool exhausted", "cited_evidence_ids": ids})
    system = real_eval.AnthropicEvalSystem(client, model="any-model", rate=RATE, max_cost_usd=1.0)
    out = system(case)
    assert out.cited_evidence_ids == ids and out.schema_valid
    assert out.estimated_cost_usd == pytest.approx((1000 * 1.0 + 200 * 5.0) / 1e6)
    sent = client.messages.calls[0]
    assert sent["model"] == "any-model" and sent["max_tokens"] == real_eval.MAX_OUTPUT_TOKENS
    assert "thinking" not in json.dumps(sent["messages"])
    assert system.spent_usd == pytest.approx(out.estimated_cost_usd or 0)


def test_reasoning_fields_and_malformed_output_are_not_accepted() -> None:
    case = _case()
    bad = real_eval.AnthropicEvalSystem(_client("not json"), model="m", rate=RATE, max_cost_usd=1.0)
    out = bad(case)
    assert out.schema_valid is False and out.error_class == "invalid_structured_output"
    assert "reasoning" not in real_eval.RealAnswer.model_fields
    assert "chain_of_thought" not in real_eval.RealAnswer.model_fields


def test_hard_cost_cap_stops_the_run_before_overspending() -> None:
    client = _client({"answer": "ok"}, tokens=(1000, 300))
    system = real_eval.AnthropicEvalSystem(client, model="m", rate=RATE, max_cost_usd=0.02)
    cases = [c for c in golden_cases() if c.suite == "triage"]
    with pytest.raises(real_eval.RealEvalBudgetExceeded):
        run_evaluation(cases, system, variant="real", metrics_source=MEASURED)
    assert system.spent_usd <= 0.02 and 0 < system.calls < len(cases)
