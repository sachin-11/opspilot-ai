"""Background jobs for smoke tests and durable investigations."""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot_api.config import get_settings
from opspilot_api.investigations.models import Investigation, InvestigationReview
from opspilot_api.investigations.workflow import run_workflow
from opspilot_api.knowledge.ingestion import run_ingestion
from opspilot_api.remediation.jobs import (
    resume_remediation_job,
    run_publish_job,
    run_remediation_job,
)
from opspilot_api.repositories.jobs import (
    run_installation_sync_job,
    run_repository_sync_job,
    run_webhook_delivery_job,
)

logger = logging.getLogger(__name__)


class PingResult(BaseModel):
    message: str
    handled_at: datetime


def ping(message: str = "pong") -> dict[str, str]:
    """Smoke-test job proving the worker can pick up and execute work.

    RQ serialises results, so a plain JSON-friendly dict is returned.
    """
    result = PingResult(message=message, handled_at=datetime.now(UTC))
    logger.info("ping job executed", extra={"job_message": message})
    return {"message": result.message, "handled_at": result.handled_at.isoformat()}


def run_investigation(workspace_id: str, investigation_id: str) -> None:
    """Start or safely continue an investigation by stable graph thread ID."""
    from uuid import UUID

    run_workflow(get_settings(), UUID(workspace_id), UUID(investigation_id))


def resume_investigation(workspace_id: str, investigation_id: str, review_id: str) -> None:
    """Resume a checkpointed human-review interrupt using an append-only review row."""
    from uuid import UUID

    settings = get_settings()
    workspace_uuid = UUID(workspace_id)
    investigation_uuid = UUID(investigation_id)
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            review = session.scalar(
                select(InvestigationReview)
                .join(Investigation, Investigation.id == InvestigationReview.investigation_id)
                .where(
                    InvestigationReview.id == UUID(review_id),
                    InvestigationReview.investigation_id == investigation_uuid,
                    InvestigationReview.workspace_id == workspace_uuid,
                    Investigation.workspace_id == workspace_uuid,
                )
            )
            investigation = session.scalar(
                select(Investigation).where(
                    Investigation.id == investigation_uuid,
                    Investigation.workspace_id == workspace_uuid,
                )
            )
            if review is None or investigation is None:
                raise LookupError("review or investigation not found in workspace")
            payload = {
                "action": review.action,
                "comment": review.comment,
                "revision_count": investigation.revision_count,
            }
    finally:
        engine.dispose()
    run_workflow(settings, workspace_uuid, investigation_uuid, resume=payload)


def run_knowledge_ingestion(workspace_id: str, job_id: str) -> None:
    """Run or safely retry one workspace-scoped knowledge ingestion job."""
    from uuid import UUID

    run_ingestion(get_settings(), UUID(workspace_id), UUID(job_id))


def run_installation_sync(workspace_id: str, installation_id: str, attempt: int = 1) -> None:
    """Synchronize one GitHub installation's repositories (read-only)."""
    from uuid import UUID

    run_installation_sync_job(get_settings(), UUID(workspace_id), UUID(installation_id), attempt)


def run_repository_sync(
    workspace_id: str, repository_id: str, options: str = "", attempt: int = 1
) -> None:
    """Sync commits/deployments and (re)index one repository's default-branch snapshot."""
    from uuid import UUID

    run_repository_sync_job(
        get_settings(), UUID(workspace_id), UUID(repository_id), options, attempt
    )


def run_webhook_delivery(delivery_id: str, attempt: int = 1) -> None:
    """Apply one signature-verified, de-duplicated GitHub webhook delivery."""
    from uuid import UUID

    run_webhook_delivery_job(get_settings(), UUID(delivery_id), attempt)


def run_remediation(
    workspace_id: str, remediation_id: str, thread_id: str, entry: str = "start"
) -> None:
    """Run the remediation graph (or its re-verification entry) on the sandbox-capable worker."""
    from uuid import UUID

    run_remediation_job(get_settings(), UUID(workspace_id), UUID(remediation_id), thread_id, entry)


def resume_remediation(
    workspace_id: str, remediation_id: str, thread_id: str, approval_id: str
) -> None:
    """Resume the human-approval interrupt from an append-only approval row."""
    from uuid import UUID

    resume_remediation_job(
        get_settings(), UUID(workspace_id), UUID(remediation_id), thread_id, UUID(approval_id)
    )


def run_remediation_publish(workspace_id: str, remediation_id: str) -> dict[str, object]:
    """Create the fix branch, commit, and pull request for an approved remediation."""
    from uuid import UUID

    return run_publish_job(get_settings(), UUID(workspace_id), UUID(remediation_id))
