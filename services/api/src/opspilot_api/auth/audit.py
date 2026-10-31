"""Append-only, HMAC-verifiable security audit log.

Each event stores an HMAC-SHA256 (keyed by ``AUTH_AUDIT_KEY``) over its canonical content, so any
edit made outside the application is detectable with ``python -m opspilot_api.auth.audit_verify``.
Database triggers reject UPDATE and DELETE. Tokens, keys, and secrets are never stored.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opspilot_api.auth.context import AuditUnavailableError, Principal, RequestInfo
from opspilot_api.auth.models import AuditActor, AuditEvent, AuditResult
from opspilot_api.config import Settings
from opspilot_api.incidents.service import utcnow
from opspilot_api.remediation.safety import scrub_details

logger = logging.getLogger(__name__)
MAX_METADATA_CHARS = 4000


def hash_ip(settings: Settings, ip: str | None) -> str | None:
    if not ip:
        return None
    key = settings.AUTH_AUDIT_KEY.get_secret_value().encode()
    return hmac.new(key, f"ip:{ip}".encode(), hashlib.sha256).hexdigest()


def bounded_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    clean = scrub_details(metadata or {})
    if len(json.dumps(clean, default=str)) > MAX_METADATA_CHARS:
        return {"truncated": True}
    return dict(clean)


def integrity_payload(event: AuditEvent) -> bytes:
    body = {
        "id": str(event.id),
        "workspace_id": str(event.workspace_id) if event.workspace_id else None,
        "actor_type": event.actor_type,
        "actor_user_id": str(event.actor_user_id) if event.actor_user_id else None,
        "actor_api_key_id": str(event.actor_api_key_id) if event.actor_api_key_id else None,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "request_id": event.request_id,
        "correlation_id": event.correlation_id,
        "source_ip_hash": event.source_ip_hash,
        "user_agent_summary": event.user_agent_summary,
        "result": event.result,
        "reason_code": event.reason_code,
        "metadata": event.event_metadata,
        "occurred_at": _iso(event.occurred_at),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _iso(value: datetime) -> str:
    return value.isoformat()


def compute_integrity(settings: Settings, event: AuditEvent) -> str:
    key = settings.AUTH_AUDIT_KEY.get_secret_value().encode()
    return hmac.new(key, integrity_payload(event), hashlib.sha256).hexdigest()


def verify_event(settings: Settings, event: AuditEvent) -> bool:
    return hmac.compare_digest(compute_integrity(settings, event), event.integrity_hash)


def build_event(
    settings: Settings,
    principal: Principal,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None,
    result: AuditResult,
    request: RequestInfo,
    reason_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    if principal.kind == "user":
        actor_type = AuditActor.USER
    elif principal.kind == "api_key":
        actor_type = AuditActor.API_KEY
    else:
        actor_type = AuditActor.SYSTEM
    event = AuditEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        actor_type=actor_type.value,
        actor_user_id=principal.user_id,
        actor_api_key_id=principal.api_key_id,
        action=action[:100],
        resource_type=resource_type[:60],
        resource_id=str(resource_id)[:100] if resource_id is not None else None,
        request_id=request.request_id[:100],
        correlation_id=request.correlation_id,
        source_ip_hash=request.ip_hash,
        user_agent_summary=(request.user_agent or "")[:120] or None,
        result=result.value,
        reason_code=reason_code,
        event_metadata=bounded_metadata(metadata),
        occurred_at=utcnow(),
    )
    event.integrity_hash = compute_integrity(settings, event)
    return event


def stage_audit(session: Session | AsyncSession, event: AuditEvent) -> AuditEvent:
    """Add to the caller's transaction so the audit row commits atomically with the action."""
    session.add(event)
    return event


async def record_audit(
    session_factory: Callable[[], AsyncSession],
    settings: Settings,
    principal: Principal,
    workspace_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None,
    result: AuditResult,
    request: RequestInfo,
    reason_code: str | None = None,
    metadata: dict[str, Any] | None = None,
    fail_closed: bool = False,
) -> None:
    """Persist in an independent transaction. ``fail_closed`` raises instead of swallowing."""
    try:
        event = build_event(
            settings,
            principal,
            workspace_id,
            action,
            resource_type,
            resource_id,
            result,
            request,
            reason_code,
            metadata,
        )
        async with session_factory() as session:
            session.add(event)
            await session.commit()
    except Exception as exc:
        logger.error(
            "audit write failed", extra={"error_class": type(exc).__name__, "audit_action": action}
        )
        if fail_closed:
            raise AuditUnavailableError("audit log unavailable; action refused") from exc
