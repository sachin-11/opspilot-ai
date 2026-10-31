"""Synchronous lifecycle primitives shared by the worker pipeline and API services."""

from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from opspilot_api.incidents.service import ConflictError, utcnow
from opspilot_api.remediation.models import RemediationProposal, RemediationStatus
from opspilot_api.remediation.state import require_transition


class RemediationCancelledError(Exception):
    pass


def transition(
    session: Session,
    proposal: RemediationProposal,
    target: RemediationStatus,
    **fields: Any,
) -> None:
    """Compare-and-set status change obeying the explicit transition table."""
    require_transition(proposal.status, target)
    result = session.execute(
        update(RemediationProposal)
        .where(
            RemediationProposal.id == proposal.id,
            RemediationProposal.workspace_id == proposal.workspace_id,
            RemediationProposal.status == proposal.status,
        )
        .values(
            status=target.value,
            version=RemediationProposal.version + 1,
            updated_at=utcnow(),
            **fields,
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        session.rollback()
        session.refresh(proposal)
        if proposal.status == RemediationStatus.CANCELLED.value:
            raise RemediationCancelledError
        raise ConflictError("remediation changed concurrently")
    session.commit()
    session.refresh(proposal)
