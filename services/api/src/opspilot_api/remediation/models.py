"""Persistence models for evidence-backed remediation proposals.

Revisions, policy evaluations, approvals, and audit events are append-only: PostgreSQL triggers
(see migration 0007) reject UPDATE and DELETE on them.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class RemediationStatus(StrEnum):
    DRAFTING = "drafting"
    POLICY_CHECK = "policy_check"
    POLICY_BLOCKED = "policy_blocked"
    SANDBOX_QUEUED = "sandbox_queued"
    SANDBOX_RUNNING = "sandbox_running"
    VERIFICATION_FAILED = "verification_failed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PULL_REQUEST_CREATED = "pull_request_created"
    REJECTED = "rejected"
    STALE = "stale"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


class PolicyDecision(StrEnum):
    ALLOWED = "allowed"
    ALLOWED_WITH_WARNINGS = "allowed_with_warnings"
    BLOCKED = "blocked"


class SandboxStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class CheckType(StrEnum):
    BASELINE = "baseline"
    PATCH_APPLY = "patch_apply"
    FORMAT = "format"
    LINT = "lint"
    TYPECHECK = "typecheck"
    UNIT_TEST = "unit_test"
    TARGETED_TEST = "targeted_test"
    BUILD = "build"
    SECURITY_SCAN = "security_scan"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"
    ERROR = "error"


class ApprovalAction(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"


class PullRequestStatus(StrEnum):
    CREATING = "creating"
    CREATED = "created"
    CLOSED = "closed"
    FAILED = "failed"
    STALE = "stale"


ACTIVE_STATUSES = (
    RemediationStatus.DRAFTING.value,
    RemediationStatus.POLICY_CHECK.value,
    RemediationStatus.SANDBOX_QUEUED.value,
    RemediationStatus.SANDBOX_RUNNING.value,
    RemediationStatus.AWAITING_APPROVAL.value,
    RemediationStatus.APPROVED.value,
    RemediationStatus.PUBLISHING.value,
)


def _pk() -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _workspace_fk() -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )


def _fk(table: str, nullable: bool = False) -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{table}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RemediationProposal(TimestampMixin, Base):
    __tablename__ = "remediation_proposals"
    __table_args__ = (
        Index("ix_remediation_workspace_created", "workspace_id", "created_at"),
        Index("ix_remediation_investigation", "workspace_id", "investigation_id"),
        Index(
            "uq_remediation_one_active_investigation",
            "workspace_id",
            "investigation_id",
            unique=True,
            postgresql_where=text(
                "status IN ('drafting','policy_check','sandbox_queued','sandbox_running',"
                "'awaiting_approval','approved','publishing')"
            ),
        ),
        CheckConstraint(
            "status IN ('drafting','policy_check','policy_blocked','sandbox_queued',"
            "'sandbox_running','verification_failed','awaiting_approval','approved',"
            "'publishing','pull_request_created','rejected','stale','failed','cancelled')",
            name="ck_remediation_status",
        ),
        CheckConstraint(
            "risk_level IN ('low','medium','high','prohibited')", name="ck_remediation_risk"
        ),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    incident_id: Mapped[UUID] = _fk("incidents")
    investigation_id: Mapped[UUID] = _fk("investigations")
    repository_id: Mapped[UUID] = _fk("repositories")
    retry_of_id: Mapped[UUID | None] = _fk("remediation_proposals", nullable=True)
    graph_thread_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    problem_statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    proposed_solution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    affected_components: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    patch_text: Mapped[str | None] = mapped_column(Text)
    patch_hash: Mapped[str | None] = mapped_column(String(64))
    files_changed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generator_prompt_version: Mapped[str] = mapped_column(String(30), nullable=False)
    generator_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    generator_model: Mapped[str] = mapped_column(String(120), nullable=False)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Structured (non-diff) model output: evidence ids, per-file explanations, limitations, ...
    structured_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    current_revision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    policy_version: Mapped[str | None] = mapped_column(String(30))
    policy_decision: Mapped[str | None] = mapped_column(String(30))
    verification_hash: Mapped[str | None] = mapped_column(String(64))
    verification_state: Mapped[str | None] = mapped_column(String(20))
    sandbox_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    stale_reason: Mapped[str | None] = mapped_column(String(200))
    # Verified identities (Module 7); used for separation-of-duties and publication rechecks.
    requested_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    last_revised_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    publish_requested_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))


class RemediationRevision(Base):
    __tablename__ = "remediation_revisions"
    __table_args__ = (
        UniqueConstraint("remediation_id", "revision_number", name="uq_remediation_revision"),
        Index("ix_remediation_revision_scope", "workspace_id", "remediation_id"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    remediation_id: Mapped[UUID] = _fk("remediation_proposals")
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_text: Mapped[str] = mapped_column(Text, nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    files_changed: Mapped[int] = mapped_column(Integer, nullable=False)
    lines_added: Mapped[int] = mapped_column(Integer, nullable=False)
    lines_deleted: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()


class PolicyEvaluation(Base):
    __tablename__ = "remediation_policy_evaluations"
    __table_args__ = (
        Index("ix_policy_eval_remediation", "workspace_id", "remediation_id", "evaluated_at"),
        CheckConstraint(
            "decision IN ('allowed','allowed_with_warnings','blocked')",
            name="ck_policy_eval_decision",
        ),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    remediation_id: Mapped[UUID] = _fk("remediation_proposals")
    revision_id: Mapped[UUID] = _fk("remediation_revisions")
    policy_version: Mapped[str] = mapped_column(String(30), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    violations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    evaluated_at: Mapped[datetime] = _created()


class SandboxRun(Base):
    __tablename__ = "remediation_sandbox_runs"
    __table_args__ = (
        Index("ix_sandbox_run_remediation", "workspace_id", "remediation_id", "created_at"),
        CheckConstraint(
            "status IN ('queued','preparing','running','passed','failed','timed_out',"
            "'blocked','cancelled')",
            name="ck_sandbox_run_status",
        ),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    remediation_id: Mapped[UUID] = _fk("remediation_proposals")
    revision_id: Mapped[UUID] = _fk("remediation_revisions")
    # "baseline" runs execute the unpatched base commit; "patched" runs apply the revision first.
    run_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="patched")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    sandbox_provider: Mapped[str] = mapped_column(String(30), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_run_id: Mapped[UUID | None] = _fk("remediation_sandbox_runs", nullable=True)
    cpu_limit: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    disk_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    network_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text)
    output_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    base_tree_hash: Mapped[str | None] = mapped_column(String(64))
    cleanup_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()


class VerificationCheck(Base):
    __tablename__ = "remediation_verification_checks"
    __table_args__ = (Index("ix_verification_check_run", "workspace_id", "sandbox_run_id"),)

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    sandbox_run_id: Mapped[UUID] = _fk("remediation_sandbox_runs")
    check_type: Mapped[str] = mapped_column(String(30), nullable=False)
    command_id: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text)
    output_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Failing test identifiers (bounded) used to separate pre-existing from new failures.
    failing_tests: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()


class RemediationApproval(Base):
    __tablename__ = "remediation_approvals"
    __table_args__ = (
        Index("ix_remediation_approval_scope", "workspace_id", "remediation_id", "created_at"),
        CheckConstraint(
            "action IN ('approved','rejected','revision_requested')",
            name="ck_remediation_approval_action",
        ),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    remediation_id: Mapped[UUID] = _fk("remediation_proposals")
    revision_id: Mapped[UUID] = _fk("remediation_revisions")
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(120))
    comment: Mapped[str] = mapped_column(String(2000), nullable=False)
    approved_patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class FixPullRequest(TimestampMixin, Base):
    __tablename__ = "remediation_pull_requests"
    __table_args__ = (
        # Single use: one approval can back at most one publication record.
        UniqueConstraint("approval_id", name="uq_fix_pr_approval"),
        UniqueConstraint("workspace_id", "repository_id", "branch_name", name="uq_fix_pr_branch"),
        Index("ix_fix_pr_remediation", "workspace_id", "remediation_id"),
        CheckConstraint(
            "status IN ('creating','created','closed','failed','stale')",
            name="ck_fix_pr_status",
        ),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    remediation_id: Mapped[UUID] = _fk("remediation_proposals")
    revision_id: Mapped[UUID] = _fk("remediation_revisions")
    approval_id: Mapped[UUID] = _fk("remediation_approvals")
    repository_id: Mapped[UUID] = _fk("repositories")
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    provider_pull_request_id: Mapped[str | None] = mapped_column(String(80))
    pull_request_number: Mapped[int | None] = mapped_column(Integer)
    pull_request_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))


class RemediationStep(Base):
    """Concise, user-visible workflow step summaries (never chain-of-thought)."""

    __tablename__ = "remediation_steps"
    __table_args__ = (
        UniqueConstraint("remediation_id", "node_name", "attempt", name="uq_remediation_step"),
        Index("ix_remediation_step_order", "workspace_id", "remediation_id", "created_at"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    remediation_id: Mapped[UUID] = _fk("remediation_proposals")
    node_name: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    summary: Mapped[str | None] = mapped_column(String(1000))
    structured_output: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class RemediationAuditEvent(Base):
    __tablename__ = "remediation_audit_events"
    __table_args__ = (
        Index("ix_remediation_audit_scope", "workspace_id", "remediation_id", "created_at"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    remediation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("remediation_proposals.id", ondelete="RESTRICT")
    )
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()
