"""Structured model output and public API contracts for remediation."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opspilot_api.remediation.models import (
    ApprovalAction,
    PolicyDecision,
    PullRequestStatus,
    RemediationStatus,
    RiskLevel,
)

AbstainReason = Literal[
    "no_safe_patch", "more_evidence_required", "outside_allowed_scope", "human_work_required"
]


class FileExplanation(BaseModel):
    path: str = Field(max_length=500)
    explanation: str = Field(max_length=1000)


class RiskAssessment(BaseModel):
    level: Literal["low", "medium", "high"]
    rationale: str = Field(max_length=1000)


class RemediationOutput(BaseModel):
    """What the model must return. Anything outside this shape is rejected."""

    abstain: bool = False
    abstain_reason: AbstainReason | None = None
    abstain_explanation: str | None = Field(default=None, max_length=1000)
    title: str = Field(default="", max_length=240)
    problem_statement: str = Field(default="", max_length=2000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=30)
    target_repository: str = Field(default="", max_length=320)
    base_commit_sha: str = Field(default="", max_length=64)
    files: list[FileExplanation] = Field(default_factory=list, max_length=50)
    unified_diff: str = Field(default="", max_length=2_000_000)
    expected_behavior_change: str = Field(default="", max_length=1500)
    risk_assessment: RiskAssessment | None = None
    verification_command_ids: list[str] = Field(default_factory=list, max_length=10)
    rollback_guidance: str = Field(default="", max_length=1500)
    known_limitations: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _abstention_or_patch(self) -> "RemediationOutput":
        if self.abstain:
            if self.abstain_reason is None:
                raise ValueError("abstain requires abstain_reason")
            return self
        required = {
            "title": self.title,
            "problem_statement": self.problem_statement,
            "target_repository": self.target_repository,
            "base_commit_sha": self.base_commit_sha,
            "unified_diff": self.unified_diff,
        }
        missing = [key for key, value in required.items() if not value.strip()]
        if missing or self.risk_assessment is None or not self.evidence_ids:
            raise ValueError("incomplete remediation output")
        return self


class UsageInfo(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    provider: str = "fake"
    model: str = "deterministic-v1"


class EvidenceContext(BaseModel):
    id: UUID
    title: str
    excerpt: str
    source_type: str


class GenerationContext(BaseModel):
    """Everything the generator may see. Source text is untrusted data, never instructions."""

    investigation_title: str
    root_cause: str
    evidence: list[EvidenceContext]
    repository_full_name: str
    base_commit_sha: str
    path_prefix: str | None
    code_files: dict[str, str]
    available_command_ids: list[str]
    human_feedback: str | None = None
    repair_errors: list[str] = Field(default_factory=list)


# --- API contracts ---------------------------------------------------------------------------
class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RemediationQueuedResponse(BaseModel):
    remediation_id: UUID
    status: RemediationStatus


class ActionResponse(BaseModel):
    remediation_id: UUID
    status: RemediationStatus
    version: int


class ReviewRequest(BaseModel):
    action: ApprovalAction
    comment: str = Field(min_length=1, max_length=2000)
    revision_id: UUID
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def _comment_not_blank(self) -> "ReviewRequest":
        if not self.comment.strip():
            raise ValueError("comment is required")
        return self


class RemediationSummary(ORMModel):
    id: UUID
    workspace_id: UUID
    incident_id: UUID
    investigation_id: UUID
    repository_id: UUID
    retry_of_id: UUID | None
    base_commit_sha: str
    status: RemediationStatus
    title: str
    risk_level: RiskLevel
    files_changed: int
    lines_added: int
    lines_deleted: int
    patch_hash: str | None
    policy_version: str | None
    policy_decision: PolicyDecision | None
    verification_state: str | None
    revision_count: int
    version: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    failed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    stale_reason: str | None


class RevisionResponse(ORMModel):
    id: UUID
    revision_number: int
    base_commit_sha: str
    patch_text: str
    patch_hash: str
    change_summary: str
    files_changed: int
    lines_added: int
    lines_deleted: int
    created_at: datetime


class PolicyEvaluationResponse(ORMModel):
    id: UUID
    revision_id: UUID
    policy_version: str
    decision: PolicyDecision
    risk_level: RiskLevel
    violations: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    evaluated_at: datetime


class CheckResponse(ORMModel):
    id: UUID
    sandbox_run_id: UUID
    check_type: str
    command_id: str
    display_name: str
    status: str
    exit_code: int | None
    duration_ms: int
    stdout_excerpt: str | None
    stderr_excerpt: str | None
    output_truncated: bool
    failing_tests: list[Any]


class SandboxRunResponse(ORMModel):
    id: UUID
    revision_id: UUID
    run_kind: str
    status: str
    sandbox_provider: str
    base_commit_sha: str
    patch_hash: str
    baseline_run_id: UUID | None
    cpu_limit: float
    memory_limit_mb: int
    disk_limit_mb: int
    timeout_seconds: int
    network_enabled: bool
    exit_code: int | None
    output_truncated: bool
    result_hash: str | None
    cleanup_verified: bool
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    checks: list[CheckResponse] = Field(default_factory=list)


class ApprovalResponse(ORMModel):
    id: UUID
    revision_id: UUID
    action: ApprovalAction
    actor_id: str | None
    comment: str
    approved_patch_hash: str
    approved_base_commit_sha: str
    approval_context_hash: str
    expires_at: datetime | None
    created_at: datetime


class PullRequestResponse(ORMModel):
    id: UUID
    remediation_id: UUID
    revision_id: UUID
    repository_id: UUID
    base_commit_sha: str
    patch_hash: str
    branch_name: str
    commit_sha: str | None
    provider_pull_request_id: str | None
    pull_request_number: int | None
    pull_request_url: str | None
    status: PullRequestStatus
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class StepResponse(ORMModel):
    id: UUID
    node_name: str
    display_name: str
    status: str
    attempt: int
    summary: str | None
    structured_output: dict[str, Any] | list[Any] | None
    error_code: str | None
    input_tokens: int
    output_tokens: int
    started_at: datetime
    completed_at: datetime | None


class VerificationComparison(BaseModel):
    state: Literal["passed", "partial", "failed", "not_run"]
    required_executed: bool
    new_failures: list[dict[str, Any]]
    existing_failures: list[dict[str, Any]]
    fixed: list[str]
    skipped: list[dict[str, str]]
    notes: list[str]


class VerificationResponse(BaseModel):
    remediation_id: UUID
    baseline: SandboxRunResponse | None
    patched: SandboxRunResponse | None
    comparison: VerificationComparison | None
    verification_hash: str | None


class ApprovalBindingResponse(BaseModel):
    """What an approval would bind to right now; shown before the human confirms."""

    workspace_id: UUID
    repository_id: UUID
    base_commit_sha: str
    revision_id: UUID
    patch_hash: str
    policy_version: str
    verification_hash: str
    expires_in_seconds: int


class RemediationDetail(RemediationSummary):
    problem_statement: str
    proposed_solution: str
    affected_components: list[Any]
    generator_provider: str
    generator_model: str
    generator_prompt_version: str
    structured_output: dict[str, Any]
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: float | None
    current_revision_id: UUID | None
    repository_full_name: str | None = None
    root_cause: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    revisions: list[RevisionResponse]
    policy_evaluations: list[PolicyEvaluationResponse]
    approvals: list[ApprovalResponse]
    steps: list[StepResponse]
    pull_request: PullRequestResponse | None
    approval_binding: ApprovalBindingResponse | None = None
    write_enabled: bool = False
    audit_events: list[dict[str, Any]] = Field(default_factory=list)


class PublishQueuedResponse(BaseModel):
    remediation_id: UUID
    status: RemediationStatus
