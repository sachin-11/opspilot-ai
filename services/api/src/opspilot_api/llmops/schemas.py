"""Request/response models for ``/api/v1/workspaces/{id}/llmops``. This is the API contract."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ prompts
class PromptVersionOut(_Out):
    id: UUID
    semantic_version: str
    status: Literal["draft", "candidate", "published", "retired"]
    is_default: bool
    change_notes: str
    content_hash: str
    system_template: str
    developer_template: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    created_by_user_id: UUID
    published_by_user_id: UUID | None
    override_reason: str | None
    gate_report: dict[str, Any] | None
    eval_run_id: UUID | None
    created_at: datetime
    published_at: datetime | None
    retired_at: datetime | None


class PromptSummary(_Out):
    id: UUID
    name: str
    workflow: str
    node_name: str
    description: str
    status: Literal["active", "deprecated"]
    default_version: str | None
    version_count: int
    updated_at: datetime


class PromptDetail(PromptSummary):
    versions: list[PromptVersionOut]


class PromptVersionCreate(BaseModel):
    system_template: str = Field(min_length=10, max_length=20000)
    developer_template: str | None = Field(default=None, max_length=20000)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"fields": {}})
    output_schema: dict[str, Any] = Field(default_factory=dict)
    change_notes: str = Field(min_length=3, max_length=1000)
    bump: Literal["patch", "minor", "major"] = "patch"


class PromptPublishRequest(BaseModel):
    version_id: UUID
    eval_run_id: UUID
    override_reason: str | None = Field(default=None, max_length=500)


class PromptRollbackRequest(BaseModel):
    target_version_id: UUID
    reason: str = Field(min_length=10, max_length=500)


class PromptActionResponse(BaseModel):
    prompt_id: UUID
    version_id: UUID
    semantic_version: str
    status: str
    is_default: bool
    overridden_gates: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ model configurations
class ModelConfigurationOut(_Out):
    id: UUID
    config_key: str
    version: int
    provider: str
    model: str
    workflows: list[str]
    capability_tier: int
    supports_structured_output: bool
    supports_tools: bool
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: float
    retry_policy: dict[str, Any]
    pricing_rate_version: str | None
    input_per_million: float | None
    output_per_million: float | None
    environments: list[str]
    fallback_config_key: str | None
    status: Literal["draft", "published", "disabled"]
    published_at: datetime | None


# ------------------------------------------------------------------ datasets and evaluation
class DatasetOut(_Out):
    id: UUID
    name: str
    workflow: str
    description: str
    version: int
    status: Literal["draft", "published", "retired"]
    content_hash: str
    case_count: int
    suite_counts: dict[str, int]
    split_counts: dict[str, int]
    published_at: datetime | None


class EvalRunCreate(BaseModel):
    dataset_id: UUID
    variant: Literal["baseline", "candidate", "regressing"] = "baseline"
    control_variant: Literal["baseline", "candidate", "regressing"] | None = None
    run_type: Literal["ci", "offline", "candidate_comparison", "production_sample", "shadow"] = (
        "offline"
    )
    purpose: Literal["development", "tuning", "gate"] = "tuning"
    repeat_count: int = Field(default=1, ge=1, le=5)
    prompt_versions: dict[str, UUID] = Field(default_factory=dict)
    model_configuration_id: UUID | None = None


class EvalRunSummary(_Out):
    id: UUID
    dataset_id: UUID
    dataset_version: int
    run_type: str
    purpose: str
    variant: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    repeat_count: int
    total_cases: int
    passed_cases: int
    failed_cases: int
    total_tokens: int
    estimated_cost_usd: float | None
    metrics_source: Literal["synthetic", "measured"]
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    error: str | None


class EvalResultOut(_Out):
    id: UUID
    case_key: str
    suite: str
    variant: str
    repeat_index: int
    passed: bool
    safety_failed: bool
    failure_reasons: list[str]
    deterministic_scores: dict[str, Any]
    llm_judge_scores: dict[str, Any] | None
    tool_calls: list[Any]
    evidence_citations: list[Any]
    latency_ms: float
    tokens: int
    estimated_cost_usd: float | None
    labelled: bool


class EvalRunDetail(EvalRunSummary):
    prompt_versions: dict[str, Any]
    routing_policy_version: str | None
    seed_configuration: dict[str, Any]
    metrics: dict[str, float | None]
    violations: dict[str, int]
    by_suite: dict[str, float]
    by_tag: dict[str, float]
    flaky_cases: list[str]
    gate_report: dict[str, Any] | None
    comparison: dict[str, Any] | None
    failed_results: list[EvalResultOut]
    grader_breakdown: dict[str, float]
    label_queue: list[EvalResultOut]
    markdown_report: str | None


class HumanLabelCreate(BaseModel):
    rubric_version: str = Field(min_length=1, max_length=40)
    criterion_scores: dict[str, int]
    overall_preference: Literal["A", "B", "tie"] | None = None
    passed: bool
    notes: str | None = Field(default=None, max_length=500)


class HumanLabelOut(_Out):
    id: UUID
    result_id: UUID
    reviewer_user_id: UUID
    rubric_version: str
    criterion_scores: dict[str, Any]
    overall_preference: str | None
    passed: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class EvalActionResponse(BaseModel):
    eval_run_id: UUID
    status: str


# ------------------------------------------------------------------ experiments
class VariantSpec(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    eval_variant: Literal["baseline", "candidate", "regressing"]
    prompt_versions: dict[str, UUID] = Field(default_factory=dict)
    model_config_key: str | None = None


def _default_risks() -> list[Literal["low", "medium"]]:
    return ["low", "medium"]


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    workflow: str = Field(min_length=1, max_length=60)
    control_variant: VariantSpec
    candidate_variant: VariantSpec
    allocation_percentage: float = Field(gt=0, le=50)
    unit: Literal["workspace", "user", "investigation"] = "investigation"
    allowed_risks: list[Literal["low", "medium"]] = Field(default_factory=_default_risks)
    primary_metric: str = Field(min_length=1, max_length=60)
    guardrail_metrics: list[str] = Field(min_length=1)
    minimum_sample_size: int = Field(ge=30, le=1_000_000)


class MetricComparisonOut(BaseModel):
    metric: str
    guardrail: bool
    control_rate: float
    candidate_rate: float
    difference: float
    ci_low: float
    ci_high: float
    p_value: float | None
    significant: bool
    regressed: bool
    note: str


class ExperimentAnalysisOut(BaseModel):
    control_n: int
    candidate_n: int
    minimum_sample_size: int
    sample_size_reached: bool
    metrics: list[MetricComparisonOut]
    safety_stop: bool
    warnings: list[str]
    promotable_by_data: bool
    promotion_note: str
    assumptions: list[str]


class ExperimentOut(_Out):
    id: UUID
    name: str
    workflow: str
    status: Literal["draft", "running", "paused", "completed", "cancelled"]
    control_variant: dict[str, Any]
    candidate_variant: dict[str, Any]
    allocation_percentage: float
    unit: str
    eligibility_rules: dict[str, Any]
    primary_metric: str
    guardrail_metrics: list[str]
    minimum_sample_size: int
    stop_reason: str | None
    started_at: datetime | None
    ended_at: datetime | None
    created_by_user_id: UUID
    created_at: datetime


class ExperimentDetail(ExperimentOut):
    assignment_counts: dict[str, int]
    analysis: ExperimentAnalysisOut | None


class ExperimentStopRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=200)
    safety: bool = False


# ------------------------------------------------------------------ usage, costs, budgets, health
class UsageBucket(BaseModel):
    key: str
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_unavailable_requests: int


class UsageResponse(BaseModel):
    group_by: str
    since: datetime
    until: datetime
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    fallback_rate: float | None
    error_rate: float | None
    buckets: list[UsageBucket]


class DailyCost(BaseModel):
    day: str
    cost_usd: float
    cost_unavailable_requests: int


class CostsResponse(BaseModel):
    since: datetime
    until: datetime
    daily: list[DailyCost]
    by_workflow: list[UsageBucket]
    by_model: list[UsageBucket]
    by_provider: list[UsageBucket]
    by_user: list[UsageBucket]
    cost_per_investigation_usd: float | None
    cost_per_completed_remediation_usd: float | None
    reserved_vs_actual: dict[str, float]
    limit_events: list[dict[str, Any]]
    total_cost_usd: float
    unavailable_cost_requests: int


class BudgetOut(BaseModel):
    id: UUID
    scope: str
    workflow: str
    hard_limit_usd: float
    soft_fraction: float
    enabled: bool
    spent_usd: float
    reserved_usd: float
    unmetered_calls: int
    soft_threshold_usd: float
    state: Literal["ok", "soft_warning", "hard_limit"]
    updated_at: datetime


class BudgetPatch(BaseModel):
    hard_limit_usd: float | None = Field(default=None, gt=0, le=1_000_000)
    soft_fraction: float | None = Field(default=None, gt=0, le=1)
    enabled: bool | None = None
    reason: str | None = Field(default=None, max_length=300)


class ProviderHealthOut(BaseModel):
    provider: str
    model: str
    breaker_state: Literal["closed", "open", "half_open", "unknown"]
    detail: str
    consecutive_failures: int | None
    recent_error_rate: float | None
    requests_24h: int
    errors_24h: int
