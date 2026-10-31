"""Append-only audit trail and real-time notifications for remediation.

Audit rows hold identifiers, codes, and counts only: never secrets, diffs, or full logs.
"""

import logging
from typing import Any
from uuid import UUID, uuid4

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.schemas import RealtimeEnvelope
from opspilot_api.incidents.service import utcnow
from opspilot_api.remediation.models import RemediationAuditEvent
from opspilot_api.remediation.safety import scrub_details

logger = logging.getLogger(__name__)


def record_audit(
    session: Session | AsyncSession,
    workspace_id: UUID,
    remediation_id: UUID | None,
    action: str,
    target_type: str,
    target_id: str | UUID | None,
    result: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    correlation_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> RemediationAuditEvent:
    """Stage an audit row in the caller's transaction (the caller commits)."""
    event = RemediationAuditEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        remediation_id=remediation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        result=result,
        correlation_id=correlation_id,
        details=scrub_details(details or {}),
        created_at=utcnow(),
    )
    session.add(event)
    return event


def _envelope(
    workspace_id: UUID, incident_id: UUID | None, event_type: str, data: dict[str, Any]
) -> RealtimeEnvelope:
    return RealtimeEnvelope(
        event_id=uuid4(),
        type=event_type,
        workspace_id=workspace_id,
        incident_id=incident_id,
        occurred_at=utcnow(),
        data=scrub_details(data),
    )


def channel(workspace_id: UUID) -> str:
    return f"opspilot:workspace:{workspace_id}:incidents"


def emit_event(
    settings: Settings,
    workspace_id: UUID,
    incident_id: UUID | None,
    remediation_id: UUID,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Publish a progress event (worker side). Failures never fail the workflow."""
    envelope = _envelope(
        workspace_id,
        incident_id,
        event_type,
        {"remediation_id": str(remediation_id), **(data or {})},
    )
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        connection.publish(channel(workspace_id), envelope.model_dump_json())
    except RedisError as exc:
        logger.warning("remediation event failed", extra={"error_class": type(exc).__name__})
    finally:
        connection.close()


async def emit_event_async(
    redis: AsyncRedis,
    workspace_id: UUID,
    incident_id: UUID | None,
    remediation_id: UUID,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    envelope = _envelope(
        workspace_id,
        incident_id,
        event_type,
        {"remediation_id": str(remediation_id), **(data or {})},
    )
    try:
        await redis.publish(channel(workspace_id), envelope.model_dump_json())
    except RedisError as exc:
        logger.warning("remediation event failed", extra={"error_class": type(exc).__name__})
