"""Recomputation of what an approval is bound to, straight from persisted rows."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opspilot_api.incidents.service import utcnow
from opspilot_api.remediation.approval import binding_mismatches
from opspilot_api.remediation.commands import COMMANDS
from opspilot_api.remediation.diffparse import patch_hash
from opspilot_api.remediation.models import (
    CheckStatus,
    RemediationApproval,
    RemediationProposal,
    RemediationRevision,
    SandboxRun,
    SandboxStatus,
    VerificationCheck,
)
from opspilot_api.remediation.policy import POLICY_VERSION
from opspilot_api.remediation.verification import CheckOutcome, RunOutcome, verification_hash


def current_revision(session: Session, proposal: RemediationProposal) -> RemediationRevision | None:
    if proposal.current_revision_id is None:
        return None
    return session.scalar(
        select(RemediationRevision).where(
            RemediationRevision.id == proposal.current_revision_id,
            RemediationRevision.workspace_id == proposal.workspace_id,
            RemediationRevision.remediation_id == proposal.id,
        )
    )


def latest_run(session: Session, proposal: RemediationProposal, kind: str) -> SandboxRun | None:
    return session.scalar(
        select(SandboxRun)
        .where(
            SandboxRun.remediation_id == proposal.id,
            SandboxRun.workspace_id == proposal.workspace_id,
            SandboxRun.revision_id == proposal.current_revision_id,
            SandboxRun.run_kind == kind,
        )
        .order_by(SandboxRun.created_at.desc(), SandboxRun.id)
    )


def outcome_from_rows(session: Session, run: SandboxRun) -> RunOutcome:
    rows = session.scalars(
        select(VerificationCheck)
        .where(
            VerificationCheck.sandbox_run_id == run.id,
            VerificationCheck.workspace_id == run.workspace_id,
        )
        .order_by(VerificationCheck.created_at, VerificationCheck.id)
    ).all()
    checks = []
    for row in rows:
        if row.command_id == "patch.apply":
            check_type = "patch_apply"
        elif row.command_id in COMMANDS:
            check_type = COMMANDS[row.command_id].check_type.value
        else:
            check_type = row.check_type
        checks.append(
            CheckOutcome(
                check_type=check_type,
                command_id=row.command_id,
                display_name=row.display_name,
                status=CheckStatus(row.status),
                exit_code=row.exit_code,
                duration_ms=row.duration_ms,
                stdout=row.stdout_excerpt or "",
                stderr=row.stderr_excerpt or "",
                truncated=row.output_truncated,
                failing_tests=[str(item) for item in row.failing_tests],
            )
        )
    return RunOutcome(
        run.run_kind,
        SandboxStatus(run.status),
        checks,
        run.started_at or run.created_at,
        run.completed_at or run.created_at,
        tree_hash=run.base_tree_hash,
        cleanup_verified=run.cleanup_verified,
        failure_code=run.failure_code,
        failure_message=run.failure_message,
    )


def recompute_verification_hash(session: Session, proposal: RemediationProposal) -> str:
    """Hash recomputed from persisted check rows, so edited rows are detected."""
    patched = latest_run(session, proposal, "patched")
    if patched is None or patched.baseline_run_id is None:
        return ""
    baseline = session.get(SandboxRun, patched.baseline_run_id)
    if baseline is None or baseline.workspace_id != proposal.workspace_id:
        return ""
    return verification_hash(
        outcome_from_rows(session, baseline), outcome_from_rows(session, patched)
    )


def current_binding(session: Session, proposal: RemediationProposal) -> dict[str, str]:
    revision = current_revision(session, proposal)
    return {
        "workspace_id": str(proposal.workspace_id),
        "repository_id": str(proposal.repository_id),
        "base_commit_sha": proposal.base_commit_sha,
        "revision_id": str(revision.id) if revision else "",
        "patch_hash": patch_hash(revision.patch_text) if revision else "",
        "policy_version": proposal.policy_version or "",
        "verification_hash": recompute_verification_hash(session, proposal),
    }


def latest_approval(
    session: Session, proposal: RemediationProposal, action: str = "approved"
) -> RemediationApproval | None:
    return session.scalar(
        select(RemediationApproval)
        .where(
            RemediationApproval.remediation_id == proposal.id,
            RemediationApproval.workspace_id == proposal.workspace_id,
            RemediationApproval.action == action,
        )
        .order_by(RemediationApproval.created_at.desc(), RemediationApproval.id)
    )


def check_binding(
    session: Session, proposal: RemediationProposal, approval: RemediationApproval
) -> list[str]:
    """Reasons an approval no longer matches the exact artifact (empty means valid)."""
    revision = current_revision(session, proposal)
    if revision is None:
        return ["revision_id"]
    problems = binding_mismatches(
        approval.approval_context,
        approval.approval_context_hash,
        approval.expires_at,
        current_binding(session, proposal),
        utcnow(),
    )
    if proposal.patch_hash != revision.patch_hash:
        problems.append("proposal_patch_hash")
    if approval.revision_id != revision.id:
        problems.append("revision_id")
    if approval.approved_patch_hash != revision.patch_hash:
        problems.append("patch_hash")
    if approval.approval_context.get("policy_version") != POLICY_VERSION:
        problems.append("policy_version")
    return sorted(set(problems))


def revision_id_of(proposal: RemediationProposal) -> UUID | None:
    return proposal.current_revision_id
