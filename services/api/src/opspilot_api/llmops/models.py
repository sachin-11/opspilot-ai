"""Persistence for the LLMOps module.

Prompts, datasets, runs, experiments, budgets, and usage are workspace-scoped so one tenant can
never change another tenant's prompts or see its usage. Model configurations, pricing rates, and
routing policies are platform-level, read-only through the API. ``usage_events`` is append-only
(UPDATE/DELETE rejected by triggers added in the migration) and idempotent on ``idempotency_key``.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_api.incidents.models import Base, TimestampMixin


def _pk() -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _fk(table: str, *, nullable: bool = False) -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), ForeignKey(f"{table}.id"), nullable=nullable)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _ts(nullable: bool = True) -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


# ----------------------------------------------------------------------------- prompts
class PromptDefinition(TimestampMixin, Base):
    __tablename__ = "prompt_definitions"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_prompt_workspace_name"),)

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _fk("workspaces")
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    workflow: Mapped[str] = mapped_column(String(60), nullable=False)
    node_name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_definition_id", "semantic_version", name="uq_prompt_semver"),
        # At most one default published version per definition.
        Index(
            "uq_prompt_single_default",
            "prompt_definition_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[UUID] = _pk()
    prompt_definition_id: Mapped[UUID] = _fk("prompt_definitions")
    semantic_version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    system_template: Mapped[str] = mapped_column(Text, nullable=False)
    developer_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_notes: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_user_id: Mapped[UUID] = _fk("users")
    published_by_user_id: Mapped[UUID | None] = _fk("users", nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    gate_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    eval_run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    published_at: Mapped[datetime | None] = _ts()
    retired_at: Mapped[datetime | None] = _ts()


# ----------------------------------------------------------------------------- models/routing
class PricingRateRow(Base):
    __tablename__ = "pricing_rates"
    __table_args__ = (UniqueConstraint("provider", "model", "version", name="uq_pricing_version"),)

    id: Mapped[UUID] = _pk()
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_per_million: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    output_per_million: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    cached_input_per_million: Mapped[float | None] = mapped_column(Numeric(14, 6), nullable=True)
    effective_from: Mapped[datetime] = _created()


class ModelConfiguration(Base):
    __tablename__ = "model_configurations"
    __table_args__ = (UniqueConstraint("config_key", "version", name="uq_model_config_version"),)

    id: Mapped[UUID] = _pk()
    config_key: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    workflows: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    capability_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    supports_structured_output: Mapped[bool] = mapped_column(Boolean, nullable=False)
    supports_tools: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    pricing_rate_id: Mapped[UUID | None] = _fk("pricing_rates", nullable=True)
    environments: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    fallback_config_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="published")
    created_at: Mapped[datetime] = _created()
    published_at: Mapped[datetime | None] = _ts()


class RoutingPolicyRow(Base):
    __tablename__ = "routing_policies"

    id: Mapped[UUID] = _pk()
    version: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_at: Mapped[datetime] = _created()


class RoutingDecisionRow(Base):
    __tablename__ = "routing_decisions"
    __table_args__ = (Index("ix_routing_decision_ws_time", "workspace_id", "created_at"),)

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _fk("workspaces")
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    config_key: Mapped[str] = mapped_column(String(80), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    workflow: Mapped[str] = mapped_column(String(60), nullable=False)
    node_name: Mapped[str] = mapped_column(String(60), nullable=False)
    risk: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    fallback_chain: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    reservation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    experiment_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    experiment_variant: Mapped[str | None] = mapped_column(String(20), nullable=True)
    shadow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created()


# ----------------------------------------------------------------------------- evaluation
class EvalDataset(Base):
    __tablename__ = "eval_datasets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", "version", name="uq_dataset_version"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID | None] = _fk("workspaces", nullable=True)  # NULL = system dataset
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    workflow: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[UUID | None] = _fk("users", nullable=True)
    created_at: Mapped[datetime] = _created()
    published_at: Mapped[datetime | None] = _ts()


class EvalCaseRow(Base):
    __tablename__ = "eval_cases"
    __table_args__ = (UniqueConstraint("dataset_id", "case_key", name="uq_case_key"),)

    id: Mapped[UUID] = _pk()
    dataset_id: Mapped[UUID] = _fk("eval_datasets")
    case_key: Mapped[str] = mapped_column(String(80), nullable=False)
    suite: Mapped[str] = mapped_column(String(40), nullable=False)
    split: Mapped[str] = mapped_column(String(20), nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_tools: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    required_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    forbidden_claims: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (Index("ix_eval_run_ws_time", "workspace_id", "created_at"),)

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID | None] = _fk("workspaces", nullable=True)
    dataset_id: Mapped[UUID] = _fk("eval_datasets")
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_configuration_id: Mapped[UUID | None] = _fk("model_configurations", nullable=True)
    routing_policy_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_type: Mapped[str] = mapped_column(String(30), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, default="gate")
    variant: Mapped[str] = mapped_column(String(40), nullable=False, default="baseline")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    seed_configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime | None] = _ts()
    completed_at: Mapped[datetime | None] = _ts()
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    metrics_source: Mapped[str] = mapped_column(String(20), nullable=False, default="synthetic")
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    gate_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    report_artifact_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by_user_id: Mapped[UUID | None] = _fk("users", nullable=True)
    created_at: Mapped[datetime] = _created()


class EvalCaseResult(Base):
    __tablename__ = "eval_case_results"
    __table_args__ = (
        UniqueConstraint("eval_run_id", "case_key", "variant", "repeat_index", name="uq_result"),
        Index("ix_result_run", "eval_run_id"),
    )

    id: Mapped[UUID] = _pk()
    eval_run_id: Mapped[UUID] = _fk("eval_runs")
    case_key: Mapped[str] = mapped_column(String(80), nullable=False)
    suite: Mapped[str] = mapped_column(String(40), nullable=False)
    variant: Mapped[str] = mapped_column(String(40), nullable=False)
    repeat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_calls: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evidence_citations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    deterministic_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    llm_judge_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    human_labels: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    safety_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class HumanLabel(Base):
    __tablename__ = "human_labels"
    __table_args__ = (
        UniqueConstraint("result_id", "reviewer_user_id", name="uq_label_reviewer_result"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _fk("workspaces")
    result_id: Mapped[UUID] = _fk("eval_case_results")
    reviewer_user_id: Mapped[UUID] = _fk("users")
    rubric_version: Mapped[str] = mapped_column(String(40), nullable=False)
    criterion_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    overall_preference: Mapped[str | None] = mapped_column(String(10), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ----------------------------------------------------------------------------- experiments
class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _fk("workspaces")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    workflow: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    control_variant: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidate_variant: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    allocation_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="investigation")
    eligibility_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    primary_metric: Mapped[str] = mapped_column(String(60), nullable=False)
    guardrail_metrics: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    minimum_sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    salt: Mapped[str] = mapped_column(String(40), nullable=False)
    analyses_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime | None] = _ts()
    ended_at: Mapped[datetime | None] = _ts()
    created_by_user_id: Mapped[UUID] = _fk("users")
    created_at: Mapped[datetime] = _created()


class ExperimentAssignmentRow(Base):
    __tablename__ = "experiment_assignments"
    __table_args__ = (UniqueConstraint("experiment_id", "unit_id", name="uq_assignment_unit"),)

    id: Mapped[UUID] = _pk()
    experiment_id: Mapped[UUID] = _fk("experiments")
    workspace_id: Mapped[UUID] = _fk("workspaces")
    unit_type: Mapped[str] = mapped_column(String(20), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False)
    variant: Mapped[str] = mapped_column(String(20), nullable=False)
    assignment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    assigned_at: Mapped[datetime] = _created()


# ----------------------------------------------------------------------------- usage and budgets
class UsageEventRow(Base):
    """Append-only. A trigger rejects UPDATE and DELETE; ``idempotency_key`` is unique."""

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_usage_idempotency"),
        Index("ix_usage_ws_time", "workspace_id", "occurred_at"),
    )

    id: Mapped[UUID] = _pk()
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    workspace_id: Mapped[UUID] = _fk("workspaces")
    user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    api_key_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    incident_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    investigation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    remediation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    workflow: Mapped[str] = mapped_column(String(60), nullable=False)
    node_name: Mapped[str] = mapped_column(String(60), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    pricing_rate_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    routing_decision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BudgetPolicyRow(Base):
    __tablename__ = "budget_policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "scope", "workflow", name="uq_budget_scope"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _fk("workspaces")
    scope: Mapped[str] = mapped_column(String(30), nullable=False)
    workflow: Mapped[str] = mapped_column(String(60), nullable=False, default="*")
    hard_limit_usd: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    soft_fraction: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BudgetCounter(Base):
    """Spend/reserved totals per concrete budget window. Updated only by conditional UPDATEs."""

    __tablename__ = "budget_counters"

    scope_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    workspace_id: Mapped[UUID] = _fk("workspaces")
    hard_limit_usd: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False)
    spent_usd: Mapped[float] = mapped_column(Numeric(14, 8), nullable=False, default=0)
    reserved_usd: Mapped[float] = mapped_column(Numeric(14, 8), nullable=False, default=0)
    unmetered_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BudgetReservationRow(Base):
    __tablename__ = "budget_reservations"

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _fk("workspaces")
    scope_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    amount_usd: Mapped[float] = mapped_column(Numeric(14, 8), nullable=False)
    actual_usd: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="reserved")
    cost_unavailable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class BudgetEventRow(Base):
    __tablename__ = "budget_events"

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _fk("workspaces")
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    detail: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created()


class LlmTraceRow(Base):
    """Internal database trace adapter target. Holds only sanitized fields."""

    __tablename__ = "llm_traces"
    __table_args__ = (Index("ix_trace_ws_time", "workspace_id", "created_at"),)

    id: Mapped[UUID] = _pk()
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    workspace_id: Mapped[UUID] = _fk("workspaces")
    workflow: Mapped[str] = mapped_column(String(60), nullable=False)
    node_name: Mapped[str] = mapped_column(String(60), nullable=False)
    final_status: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    routing_policy_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class GateConfigRow(Base):
    __tablename__ = "gate_configs"
    __table_args__ = (UniqueConstraint("workflow", "version", name="uq_gate_version"),)

    id: Mapped[UUID] = _pk()
    workflow: Mapped[str] = mapped_column(String(60), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()
