"""Concise workspace WebSocket events for repository work (never tokens, source, or payloads)."""

import logging
from typing import Any
from uuid import UUID, uuid4

from redis import Redis

from opspilot_api.config import Settings
from opspilot_api.incidents.schemas import RealtimeEnvelope
from opspilot_api.incidents.service import utcnow

logger = logging.getLogger(__name__)

_ALLOWED_KEYS = {
    "repository_id",
    "installation_id",
    "snapshot_id",
    "status",
    "stage",
    "files_processed",
    "total_files",
    "chunks_processed",
    "failure_code",
    "event_type",
    "service_id",
    "mapping_id",
}


def publish_repository_event(
    settings: Settings, workspace_id: UUID, event_type: str, data: dict[str, Any]
) -> None:
    payload = {key: value for key, value in data.items() if key in _ALLOWED_KEYS}
    envelope = RealtimeEnvelope(
        event_id=uuid4(),
        type=event_type,
        workspace_id=workspace_id,
        incident_id=None,
        occurred_at=utcnow(),
        data={
            key: str(value) if isinstance(value, UUID) else value for key, value in payload.items()
        },
    )
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        connection.publish(
            f"opspilot:workspace:{workspace_id}:incidents", envelope.model_dump_json()
        )
    except Exception as exc:
        logger.warning(
            "repository event publication failed", extra={"error_class": type(exc).__name__}
        )
    finally:
        connection.close()
