"""RQ job bodies for remediation. All are idempotent and safe to retry."""

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.remediation.lifecycle import RemediationCancelledError
from opspilot_api.remediation.models import RemediationApproval, RemediationProposal
from opspilot_api.remediation.pipeline import (
    ApprovalInvalidError,
    RemediationFailureError,
    RemediationPipeline,
)
from opspilot_api.remediation.queue import enqueue_remediation, new_thread_id
from opspilot_api.remediation.sandbox import purge_expired_workspaces
from opspilot_api.remediation.workflow import run_remediation_workflow

logger = logging.getLogger(__name__)


def run_remediation_job(
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    thread_id: str,
    entry: str,
    pipeline: RemediationPipeline | None = None,
) -> None:
    root = settings.REMEDIATION_WORKSPACE_ROOT
    purge_expired_workspaces(
        Path(root) if root else None, settings.REMEDIATION_ARTIFACT_RETENTION_SECONDS
    )
    run_remediation_workflow(
        settings,
        workspace_id,
        remediation_id,
        thread_id,
        "reverify" if entry == "reverify" else "start",
        pipeline=pipeline,
    )


def resume_remediation_job(
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    thread_id: str,
    approval_id: UUID,
    pipeline: RemediationPipeline | None = None,
) -> None:
    """Resume the interrupted graph using the append-only review row written by the API."""
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            approval = session.scalar(
                select(RemediationApproval).where(
                    RemediationApproval.id == approval_id,
                    RemediationApproval.remediation_id == remediation_id,
                    RemediationApproval.workspace_id == workspace_id,
                )
            )
            if approval is None:
                raise LookupError("review not found in workspace")
            payload = {"action": approval.action, "comment": approval.comment}
    finally:
        engine.dispose()
    run_remediation_workflow(
        settings, workspace_id, remediation_id, thread_id, resume=payload, pipeline=pipeline
    )


def run_publish_job(
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    pipeline: RemediationPipeline | None = None,
) -> dict[str, object]:
    """Publish an approved remediation. Re-validates the whole approval binding first."""
    runtime = pipeline or RemediationPipeline(settings)
    try:
        try:
            return runtime.publish(workspace_id, remediation_id)
        except RemediationCancelledError:
            return {"outcome": "cancelled"}
        except ApprovalInvalidError as exc:
            logger.warning("approval invalidated at publish", extra={"reasons": exc.reasons})
            _maybe_reverify(settings, runtime, workspace_id, remediation_id, exc)
            return {"outcome": "approval_invalid", "reasons": exc.reasons}
        except RemediationFailureError as exc:
            return {"outcome": "failed", "code": exc.code}
    finally:
        if pipeline is None:
            runtime.close()


def _maybe_reverify(
    settings: Settings,
    runtime: RemediationPipeline,
    workspace_id: UUID,
    remediation_id: UUID,
    exc: ApprovalInvalidError,
) -> None:
    """After an invalidation the remediation is back in policy_check: start re-verification."""
    if "base_commit_stale" in exc.reasons:
        return
    with Session(runtime.engine) as session:
        proposal = session.scalar(
            select(RemediationProposal).where(
                RemediationProposal.id == remediation_id,
                RemediationProposal.workspace_id == workspace_id,
            )
        )
        if proposal is None or proposal.status != "policy_check":
            return
        thread = new_thread_id(remediation_id)
        proposal.graph_thread_id = thread
        session.commit()
    enqueue_remediation(settings, workspace_id, remediation_id, thread, "reverify")
