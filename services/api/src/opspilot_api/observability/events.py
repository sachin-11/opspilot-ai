"""Concise workspace WebSocket events for the observability pipeline (never payloads/secrets)."""

import logging
from typing import Any
from uuid import UUID, uuid4

from redis import Redis

from opspilot_api.config import Settings
from opspilot_api.incidents.schemas import RealtimeEnvelope
from opspilot_api.incidents.service import utcnow

logger = logging.getLogger(__name__)
_ALLOWED = {
    "signal_id",
    "source_id",
    "status",
    "incident_id",
    "failure_code",
    "reason",
    "dead_letter_id",
}


def publish_observability_event(
    settings: Settings, workspace_id: UUID, event_type: str, data: dict[str, Any]
) -> None:
    """Best-effort fan-out: durable state lives in PostgreSQL, so a Redis outage only delays UI."""
    payload = {k: str(v) if isinstance(v, UUID) else v for k, v in data.items() if k in _ALLOWED}
    incident = data.get("incident_id")
    envelope = RealtimeEnvelope(
        event_id=uuid4(),
        type=event_type,
        workspace_id=workspace_id,
        incident_id=incident if isinstance(incident, UUID) else None,
        occurred_at=utcnow(),
        data=payload,
    )
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        connection.publish(
            f"opspilot:workspace:{workspace_id}:incidents", envelope.model_dump_json()
        )
    except Exception as exc:
        logger.warning(
            "observability event publication failed", extra={"error_class": type(exc).__name__}
        )
    finally:
        connection.close()
