"""Bounded structured-model provider abstraction with retryable fallback routing."""

import secrets
import time
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from opspilot_api.config import Settings
from opspilot_api.investigations.schemas import (
    CritiqueResult,
    Hypothesis,
    HypothesisSet,
    InvestigationPlan,
    InvestigationReport,
    PlanStep,
    ToolRequest,
    TriageResult,
    UsageInfo,
)
from opspilot_api.knowledge.schemas import HydePassage, QueryRewrite

OutputT = TypeVar("OutputT", bound=BaseModel)


class ProviderError(Exception):
    code = "provider_error"


class RetryableProviderError(ProviderError):
    code = "provider_temporarily_unavailable"


class ProviderConfigurationError(ProviderError):
    code = "provider_configuration_error"


class StructuredOutputError(ProviderError):
    code = "invalid_structured_output"


@dataclass(frozen=True)
class ModelResult:
    output: BaseModel
    usage: UsageInfo


class ModelProvider:
    provider_name: str
    model_name: str

    def generate(
        self, prompt_name: str, schema: type[OutputT], context: dict[str, Any]
    ) -> ModelResult:
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model_name: str = "deterministic-v1") -> None:
        self.provider_name = "fake"
        self.model_name = model_name
        self.calls = 0

    def generate(
        self, prompt_name: str, schema: type[OutputT], context: dict[str, Any]
    ) -> ModelResult:
        self.calls += 1
        if self.model_name == "retryable-failure":
            raise RetryableProviderError("simulated temporary provider timeout")
        if self.model_name == "authentication-failure":
            raise ProviderConfigurationError("simulated provider authentication failure")
        if self.model_name == "invalid-output":
            raise StructuredOutputError("simulated invalid structured output")
        evidence_ids = [UUID(value) for value in context.get("evidence_ids", [])]
        first = evidence_ids[:1]
        insufficient = bool(context.get("insufficient_evidence")) or not evidence_ids
        data: BaseModel
        if schema is TriageResult:
            data = TriageResult(
                category="service_degradation",
                impact_assessment="User-facing reliability may be degraded.",
                urgency="high",
                affected_component=str(context.get("service_name") or "unknown service"),
                initial_questions=["What changed before onset?"],
                evidence_needed=["incident timeline", "correlated events"],
            )
        elif schema is InvestigationPlan:
            incident = context.get("incident", {})
            query = " ".join(
                str(incident.get(key) or "") for key in ("title", "description")
            ).strip()
            data = InvestigationPlan(
                objective="Collect bounded incident, event, and relevant runbook evidence.",
                tool_requests=[
                    ToolRequest(tool_name="get_incident_context"),
                    ToolRequest(tool_name="get_incident_timeline", arguments={"limit": 50}),
                    ToolRequest(tool_name="search_ingested_events", arguments={"limit": 50}),
                    ToolRequest(tool_name="summarize_event_frequency"),
                    ToolRequest(tool_name="compare_incident_windows"),
                    ToolRequest(
                        tool_name="search_runbooks",
                        arguments={
                            "query": query or "incident troubleshooting",
                            "service_id": incident.get("service_id"),
                            "document_types": ["runbook", "troubleshooting"],
                            "top_k": 3,
                        },
                    ),
                ],
            )
        elif schema is HypothesisSet:
            data = HypothesisSet(
                hypotheses=[
                    Hypothesis(
                        description="Stored events indicate a repeated service failure pattern."
                        if not insufficient
                        else "Available data does not establish a root cause.",
                        supporting_evidence_ids=first,
                        contradicting_evidence_ids=[],
                        confidence=0.72 if not insufficient else 0.2,
                        missing_evidence=[]
                        if not insufficient
                        else ["additional operational telemetry"],
                        suggested_validation=(
                            "Verify the cited event pattern against service telemetry."
                        ),
                    )
                ]
            )
        elif schema is CritiqueResult:
            data = CritiqueResult(
                supported=not insufficient,
                insufficient_evidence=insufficient,
                concerns=["Do not infer beyond stored evidence."] if insufficient else [],
                recommended_hypothesis_index=0,
                confidence_adjustment=-0.1 if insufficient else 0,
            )
        elif schema is InvestigationReport:
            step = PlanStep(
                order=1,
                action=(
                    "Review the affected service and validate the cited failure pattern "
                    "before any change."
                ),
                rationale="Remediation is advisory and must be human-executed.",
                evidence_ids=first,
            )
            data = InvestigationReport(
                title="Incident investigation report",
                executive_summary="Evidence was reviewed using bounded, read-only tools.",
                root_cause="Insufficient evidence to identify a root cause."
                if insufficient
                else "The cited events support a repeated service failure pattern.",
                confidence=0.2 if insufficient else 0.7,
                impact="Service reliability may be reduced.",
                evidence_citations=first or [UUID(str(context["fallback_evidence_id"]))],
                alternative_hypotheses=["A dependency or deployment change may be contributing."],
                immediate_containment_steps=[step],
                remediation_plan=[step],
                verification_plan=[step],
                unknowns_and_assumptions=["No external telemetry was queried."],
                abstained=insufficient,
            )
        elif schema is QueryRewrite:
            words = [w for w in str(context.get("query", "")).split() if len(w) > 3]
            data = QueryRewrite(query=" ".join(words[:20]) or "troubleshooting")
        elif schema is HydePassage:
            data = HydePassage(
                passage=f"Runbook procedure for: {str(context.get('query', ''))[:400]}. "
                "Check dashboards, inspect logs, and follow the documented recovery steps."
            )
        else:
            raise ProviderConfigurationError(f"fake provider has no fixture for {schema.__name__}")
        parsed = schema.model_validate(data.model_dump())
        return ModelResult(
            output=parsed,
            usage=UsageInfo(
                input_tokens=120,
                output_tokens=80,
                estimated_cost_usd=0.0,
                provider=self.provider_name,
                model=self.model_name,
            ),
        )


class UnsupportedProvider(ModelProvider):
    def __init__(self, provider_name: str, model_name: str) -> None:
        self.provider_name = provider_name
        self.model_name = model_name

    def generate(
        self, prompt_name: str, schema: type[OutputT], context: dict[str, Any]
    ) -> ModelResult:
        raise ProviderConfigurationError(
            f"provider adapter {self.provider_name!r} is not installed"
        )


def create_provider(provider: str, model: str) -> ModelProvider:
    return FakeProvider(model) if provider == "fake" else UnsupportedProvider(provider, model)


class ProviderRouter:
    def __init__(
        self,
        settings: Settings,
        primary: ModelProvider | None = None,
        fallback: ModelProvider | None = None,
    ) -> None:
        self.settings = settings
        self.primary = primary or create_provider(
            settings.AI_PRIMARY_PROVIDER, settings.AI_PRIMARY_MODEL
        )
        self.fallback = fallback or (
            create_provider(settings.AI_FALLBACK_PROVIDER, settings.AI_FALLBACK_MODEL)
            if settings.AI_FALLBACK_PROVIDER and settings.AI_FALLBACK_MODEL
            else None
        )
        self.calls = 0

    def generate(
        self, prompt_name: str, schema: type[OutputT], context: dict[str, Any]
    ) -> ModelResult:
        if self.calls >= self.settings.AI_MAX_LLM_CALLS:
            raise ProviderError("maximum LLM call limit reached")
        last_error: RetryableProviderError | None = None
        for attempt in range(self.settings.AI_PROVIDER_RETRIES + 1):
            self.calls += 1
            try:
                return self._validated_call(self.primary, prompt_name, schema, context)
            except RetryableProviderError as exc:
                last_error = exc
                if attempt < self.settings.AI_PROVIDER_RETRIES:
                    time.sleep(min(1.0, 0.05 * (2**attempt)) + secrets.randbelow(20) / 1000)
        if self.fallback is not None and last_error is not None:
            if self.calls >= self.settings.AI_MAX_LLM_CALLS:
                raise ProviderError("maximum LLM call limit reached") from last_error
            self.calls += 1
            return self._validated_call(self.fallback, prompt_name, schema, context)
        raise cast(RetryableProviderError, last_error)

    def _validated_call(
        self,
        provider: ModelProvider,
        prompt_name: str,
        schema: type[OutputT],
        context: dict[str, Any],
    ) -> ModelResult:
        try:
            result = provider.generate(prompt_name, schema, context)
            schema.model_validate(result.output.model_dump())
            return result
        except ValidationError as exc:
            try:
                repair_context = {
                    **context,
                    "repair_validation_errors": exc.errors(include_url=False),
                }
                return provider.generate(prompt_name, schema, repair_context)
            except (ValidationError, StructuredOutputError) as repair_exc:
                raise StructuredOutputError(
                    "structured output failed validation and repair"
                ) from repair_exc
