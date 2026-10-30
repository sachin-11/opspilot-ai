"""Structured model outputs and public API contracts for investigations."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opspilot_api.investigations.models import (
    InvestigationStatus,
    InvestigationTrigger,
    ReviewAction,
)


class TriageResult(BaseModel):
    category: str = Field(max_length=100)
    impact_assessment: str = Field(max_length=1000)
    urgency: Literal["low", "medium", "high", "critical"]
    affected_component: str = Field(max_length=200)
    initial_questions: list[str] = Field(max_length=10)
    evidence_needed: list[str] = Field(max_length=10)


class ToolRequest(BaseModel):
    tool_name: Literal[
        "get_incident_context",
        "get_incident_timeline",
        "search_ingested_events",
        "summarize_event_frequency",
        "compare_incident_windows",
        "search_runbooks",
        "get_knowledge_chunk",
        "list_recent_changes",
        "rank_suspect_changes",
        "get_commit_summary",
        "get_commit_diff",
        "search_repository_code",
        "get_code_excerpt",
        "get_pull_request_context",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)


class InvestigationPlan(BaseModel):
    objective: str = Field(max_length=500)
    tool_requests: list[ToolRequest] = Field(min_length=1, max_length=12)


class Hypothesis(BaseModel):
    description: str = Field(max_length=1000)
    supporting_evidence_ids: list[UUID] = Field(max_length=20)
    contradicting_evidence_ids: list[UUID] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)
    missing_evidence: list[str] = Field(max_length=10)
    suggested_validation: str = Field(max_length=1000)


class HypothesisSet(BaseModel):
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=5)


class CritiqueResult(BaseModel):
    supported: bool
    insufficient_evidence: bool
    concerns: list[str] = Field(max_length=10)
    recommended_hypothesis_index: int | None = Field(default=None, ge=0, le=4)
    confidence_adjustment: float = Field(default=0, ge=-1, le=1)


class PlanStep(BaseModel):
    order: int = Field(ge=1, le=50)
    action: str = Field(max_length=1000)
    rationale: str = Field(max_length=1000)
    evidence_ids: list[UUID] = Field(max_length=20)


class InvestigationReport(BaseModel):
    title: str = Field(max_length=240)
    executive_summary: str = Field(max_length=3000)
    root_cause: str = Field(max_length=3000)
    confidence: float = Field(ge=0, le=1)
    impact: str = Field(max_length=2000)
    evidence_citations: list[UUID] = Field(min_length=1, max_length=30)
    alternative_hypotheses: list[str] = Field(max_length=5)
    immediate_containment_steps: list[PlanStep] = Field(max_length=10)
    remediation_plan: list[PlanStep] = Field(max_length=20)
    verification_plan: list[PlanStep] = Field(max_length=20)
    unknowns_and_assumptions: list[str] = Field(max_length=20)
    abstained: bool = False


class UsageInfo(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    provider: str
    model: str


class InvestigationCreate(BaseModel):
    trigger: InvestigationTrigger = InvestigationTrigger.MANUAL


class InvestigationQueuedResponse(BaseModel):
    investigation_id: UUID
    status: Literal["queued"]


class ReviewRequest(BaseModel):
    action: ReviewAction
    comment: str | None = Field(default=None, max_length=2000)
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def require_comment_for_negative_action(self) -> "ReviewRequest":
        if self.action in {
            ReviewAction.REVISION_REQUESTED,
            ReviewAction.REJECTED,
        } and (not self.comment or not self.comment.strip()):
            raise ValueError("comment is required for revision or rejection")
        return self


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StepResponse(ORMModel):
    id: UUID
    node_name: str
    display_name: str
    status: str
    attempt: int
    summary: str | None
    structured_output: dict[str, Any] | list[Any] | None
    error_code: str | None
    error_message: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    started_at: datetime
    completed_at: datetime | None


class EvidenceResponse(ORMModel):
    id: UUID
    evidence_type: str
    source_type: str
    source_id: str | None
    title: str
    excerpt: str
    metadata: dict[str, Any] = Field(validation_alias="evidence_metadata")
    content_hash: str
    collected_at: datetime


class ReviewResponse(ORMModel):
    id: UUID
    action: ReviewAction
    comment: str | None
    actor_id: str | None
    created_at: datetime


class InvestigationSummary(ORMModel):
    id: UUID
    workspace_id: UUID
    incident_id: UUID
    retry_of_id: UUID | None
    status: InvestigationStatus
    trigger: InvestigationTrigger
    prompt_version: str
    primary_provider: str
    primary_model: str
    fallback_provider: str | None
    fallback_model: str | None
    title: str | None
    executive_summary: str | None
    root_cause: str | None
    confidence: float | None
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: float | None
    revision_count: int
    version: int
    started_at: datetime | None
    review_requested_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    cancelled_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class InvestigationDetail(InvestigationSummary):
    steps: list[StepResponse]
    evidence: list[EvidenceResponse]
    reviews: list[ReviewResponse]
    report: InvestigationReport | None


class ActionResponse(BaseModel):
    investigation_id: UUID
    status: InvestigationStatus
    version: int


class FakeScenario(BaseModel):
    category: str = "application_error"
    root_cause: str = "A repeated service error is the most likely cause."
    insufficient_evidence: bool = False
    contradictory: bool = False

    @field_validator("root_cause")
    @classmethod
    def bound_root_cause(cls, value: str) -> str:
        return value[:3000]
