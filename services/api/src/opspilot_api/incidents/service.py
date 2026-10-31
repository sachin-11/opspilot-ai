"""Transactional application services for the incident domain."""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.models import Role, WorkspaceMember
from opspilot_api.incidents.models import (
    ActorType,
    Environment,
    EventType,
    Incident,
    IncidentEvent,
    IncidentSource,
    IncidentStatus,
    IngestedEvent,
    MonitoredService,
    Severity,
    Workspace,
)
from opspilot_api.incidents.schemas import (
    EventIngestRequest,
    EventIngestResponse,
    IncidentCreate,
    IncidentListResponse,
    IncidentResponse,
    NoteCreate,
    RealtimeEnvelope,
    ServiceCreate,
    SeverityUpdate,
    StatusUpdate,
    WorkspaceCreate,
)


class DomainError(Exception):
    status_code = 422
    code = "domain_error"


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class ValidationError(DomainError):
    status_code = 422
    code = "invalid_transition"


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValidationError("slug must contain a letter or number")
    return slug[:80]


async def require_workspace(session: AsyncSession, workspace_id: UUID) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFoundError("workspace not found")
    return workspace


async def require_service(
    session: AsyncSession, workspace_id: UUID, service_id: UUID
) -> MonitoredService:
    service = await session.scalar(
        select(MonitoredService).where(
            MonitoredService.id == service_id, MonitoredService.workspace_id == workspace_id
        )
    )
    if service is None:
        raise NotFoundError("service not found")
    return service


async def require_incident(
    session: AsyncSession, workspace_id: UUID, incident_id: UUID
) -> Incident:
    incident = await session.scalar(
        select(Incident).where(Incident.id == incident_id, Incident.workspace_id == workspace_id)
    )
    if incident is None:
        raise NotFoundError("incident not found")
    return incident


def timeline_event(
    incident: Incident,
    event_type: EventType,
    summary: str,
    actor_type: ActorType,
    metadata: dict[str, object] | None = None,
    occurred_at: datetime | None = None,
    actor_id: str | None = None,
) -> IncidentEvent:
    return IncidentEvent(
        id=uuid4(),
        workspace_id=incident.workspace_id,
        incident_id=incident.id,
        event_type=event_type.value,
        actor_type=actor_type.value,
        actor_id=actor_id,
        summary=summary,
        event_metadata=metadata or {},
        occurred_at=occurred_at or utcnow(),
    )


async def create_workspace(
    session: AsyncSession, data: WorkspaceCreate, owner_id: UUID
) -> Workspace:
    workspace = Workspace(
        id=uuid4(), name=data.name.strip(), slug=normalize_slug(data.slug or data.name)
    )
    session.add(workspace)
    session.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner_id, role=Role.ADMIN.label))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("workspace slug already exists") from exc
    await session.refresh(workspace)
    return workspace


async def list_workspaces(session: AsyncSession) -> list[Workspace]:
    return list(
        (await session.scalars(select(Workspace).order_by(Workspace.name, Workspace.id))).all()
    )


async def create_service(
    session: AsyncSession, workspace_id: UUID, data: ServiceCreate
) -> MonitoredService:
    await require_workspace(session, workspace_id)
    service = MonitoredService(
        id=uuid4(),
        workspace_id=workspace_id,
        name=data.name.strip(),
        slug=normalize_slug(data.slug or data.name),
        description=data.description,
        environment=data.environment.value,
    )
    session.add(service)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("service slug already exists in this workspace") from exc
    await session.refresh(service)
    return service


async def list_services(session: AsyncSession, workspace_id: UUID) -> list[MonitoredService]:
    await require_workspace(session, workspace_id)
    return list(
        (
            await session.scalars(
                select(MonitoredService)
                .where(MonitoredService.workspace_id == workspace_id)
                .order_by(MonitoredService.name, MonitoredService.id)
            )
        ).all()
    )


async def create_manual_incident(
    session: AsyncSession, workspace_id: UUID, data: IncidentCreate, actor_id: str | None = None
) -> Incident:
    await require_workspace(session, workspace_id)
    if data.service_id is not None:
        await require_service(session, workspace_id, data.service_id)
    now = utcnow()
    incident = Incident(
        id=uuid4(),
        workspace_id=workspace_id,
        service_id=data.service_id,
        title=data.title.strip(),
        description=data.description,
        severity=data.severity.value,
        status=IncidentStatus.OPEN.value,
        source=IncidentSource.MANUAL.value,
        version=1,
        opened_at=now,
    )
    session.add(incident)
    session.add(
        timeline_event(
            incident,
            EventType.INCIDENT_CREATED,
            "Incident created",
            ActorType.USER,
            occurred_at=now,
            actor_id=actor_id,
        )
    )
    await session.commit()
    await session.refresh(incident)
    return incident


def _encode_cursor(created_at: datetime, incident_id: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(incident_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        timestamp, incident_id = json.loads(raw)
        return datetime.fromisoformat(timestamp), UUID(incident_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid cursor") from exc


def _incident_response(incident: Incident, service: MonitoredService | None) -> IncidentResponse:
    response = IncidentResponse.model_validate(incident)
    return response.model_copy(
        update={
            "service_name": service.name if service else None,
            "service_environment": Environment(service.environment) if service else None,
        }
    )


async def list_incidents(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    statuses: list[IncidentStatus],
    severities: list[Severity],
    service_id: UUID | None,
    environment: str | None,
    cursor: str | None,
    limit: int,
) -> IncidentListResponse:
    await require_workspace(session, workspace_id)
    stmt = (
        select(Incident, MonitoredService)
        .outerjoin(
            MonitoredService,
            and_(
                MonitoredService.id == Incident.service_id,
                MonitoredService.workspace_id == workspace_id,
            ),
        )
        .where(Incident.workspace_id == workspace_id)
    )
    if statuses:
        stmt = stmt.where(Incident.status.in_([item.value for item in statuses]))
    if severities:
        stmt = stmt.where(Incident.severity.in_([item.value for item in severities]))
    if service_id:
        stmt = stmt.where(Incident.service_id == service_id)
    if environment:
        stmt = stmt.where(MonitoredService.environment == environment)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Incident.created_at < cursor_time,
                and_(Incident.created_at == cursor_time, Incident.id < cursor_id),
            )
        )
    raw_rows = (
        await session.execute(
            stmt.order_by(Incident.created_at.desc(), Incident.id.desc()).limit(limit + 1)
        )
    ).all()
    rows = cast(list[tuple[Incident, MonitoredService | None]], raw_rows)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_incident_response(incident, service) for incident, service in rows]
    next_cursor = _encode_cursor(rows[-1][0].created_at, rows[-1][0].id) if has_more else None
    return IncidentListResponse(items=items, next_cursor=next_cursor)


async def get_incident_response(
    session: AsyncSession, workspace_id: UUID, incident_id: UUID
) -> IncidentResponse:
    incident = await require_incident(session, workspace_id, incident_id)
    service = None
    if incident.service_id:
        service = await require_service(session, workspace_id, incident.service_id)
    return _incident_response(incident, service)


VALID_TRANSITIONS: dict[str, set[str]] = {
    "open": {"investigating", "mitigated", "resolved"},
    "investigating": {"mitigated", "resolved"},
    "mitigated": {"investigating", "resolved"},
    "resolved": {"investigating"},
}


async def update_status(
    session: AsyncSession,
    workspace_id: UUID,
    incident_id: UUID,
    data: StatusUpdate,
    actor_id: str | None = None,
) -> tuple[Incident, bool]:
    incident = await require_incident(session, workspace_id, incident_id)
    if data.status.value not in VALID_TRANSITIONS[incident.status]:
        raise ValidationError(f"cannot transition from {incident.status} to {data.status.value}")
    now = utcnow()
    values: dict[str, object] = {
        "status": data.status.value,
        "version": data.version + 1,
        "updated_at": now,
    }
    reopened = incident.status == IncidentStatus.RESOLVED.value
    if data.status is IncidentStatus.INVESTIGATING:
        values["acknowledged_at"] = incident.acknowledged_at or now
    if data.status is IncidentStatus.MITIGATED:
        values["mitigated_at"] = now
    if data.status is IncidentStatus.RESOLVED:
        values["resolved_at"] = now
    if reopened:
        values.update({"resolved_at": None, "mitigated_at": None})
    result = await session.execute(
        update(Incident)
        .where(
            Incident.id == incident_id,
            Incident.workspace_id == workspace_id,
            Incident.version == data.version,
        )
        .values(**values)
        .returning(Incident)
    )
    updated = result.scalar_one_or_none()
    if updated is None:
        await session.rollback()
        raise ConflictError("incident version is stale")
    event_type = EventType.INCIDENT_REOPENED if reopened else EventType.STATUS_CHANGED
    summary = "Incident reopened" if reopened else f"Status changed to {data.status.value}"
    session.add(
        timeline_event(
            updated,
            event_type,
            summary,
            ActorType.USER,
            {"from": incident.status, "to": data.status.value},
            now,
            actor_id,
        )
    )
    await session.commit()
    return updated, reopened


async def update_severity(
    session: AsyncSession,
    workspace_id: UUID,
    incident_id: UUID,
    data: SeverityUpdate,
    actor_id: str | None = None,
) -> Incident:
    incident = await require_incident(session, workspace_id, incident_id)
    if incident.severity == data.severity.value:
        raise ValidationError("severity is already set to that value")
    old = incident.severity
    now = utcnow()
    result = await session.execute(
        update(Incident)
        .where(
            Incident.id == incident_id,
            Incident.workspace_id == workspace_id,
            Incident.version == data.version,
        )
        .values(severity=data.severity.value, version=data.version + 1, updated_at=now)
        .returning(Incident)
    )
    updated = result.scalar_one_or_none()
    if updated is None:
        await session.rollback()
        raise ConflictError("incident version is stale")
    session.add(
        timeline_event(
            updated,
            EventType.SEVERITY_CHANGED,
            f"Severity changed to {data.severity.value}",
            ActorType.USER,
            {"from": old, "to": data.severity.value},
            now,
            actor_id,
        )
    )
    await session.commit()
    return updated


async def add_note(
    session: AsyncSession,
    workspace_id: UUID,
    incident_id: UUID,
    data: NoteCreate,
    actor_id: str | None = None,
) -> IncidentEvent:
    incident = await require_incident(session, workspace_id, incident_id)
    now = utcnow()
    result = await session.execute(
        update(Incident)
        .where(
            Incident.id == incident_id,
            Incident.workspace_id == workspace_id,
            Incident.version == data.version,
        )
        .values(version=data.version + 1, updated_at=now)
        .returning(Incident.id)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        raise ConflictError("incident version is stale")
    event = timeline_event(
        incident,
        EventType.NOTE_ADDED,
        data.summary.strip(),
        ActorType.USER,
        occurred_at=now,
        actor_id=actor_id,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def list_timeline(
    session: AsyncSession, workspace_id: UUID, incident_id: UUID
) -> list[IncidentEvent]:
    await require_incident(session, workspace_id, incident_id)
    return list(
        (
            await session.scalars(
                select(IncidentEvent)
                .where(
                    IncidentEvent.workspace_id == workspace_id,
                    IncidentEvent.incident_id == incident_id,
                )
                .order_by(IncidentEvent.occurred_at, IncidentEvent.created_at, IncidentEvent.id)
            )
        ).all()
    )


def event_fingerprint(workspace_id: UUID, data: EventIngestRequest) -> str:
    normalized = "\x1f".join(
        [
            str(workspace_id),
            str(data.service_id),
            " ".join(data.event_type.lower().split()),
            " ".join((data.error_signature or data.event_type).lower().split()),
        ]
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


async def ingest_event(
    session: AsyncSession,
    workspace_id: UUID,
    idempotency_key: str,
    data: EventIngestRequest,
    actor_id: str | None = None,
) -> EventIngestResponse:
    await require_workspace(session, workspace_id)
    await require_service(session, workspace_id, data.service_id)
    # Serialize same-key requests. The database unique constraint remains the backstop.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{workspace_id}:{idempotency_key}"},
    )
    existing = await session.scalar(
        select(IngestedEvent).where(
            IngestedEvent.workspace_id == workspace_id,
            IngestedEvent.idempotency_key == idempotency_key,
        )
    )
    if existing:
        existing_incident = await require_incident(session, workspace_id, existing.incident_id)
        result = "created" if existing_incident.opened_at == existing.received_at else "correlated"
        # The original outcome is explicitly persisted in attributes under a reserved internal key.
        result = str(existing.attributes.get("_opspilot_result", result))
        return EventIngestResponse(
            event_id=existing.id,
            incident_id=existing.incident_id,
            result=cast(Literal["created", "correlated"], result),
        )
    fingerprint = event_fingerprint(workspace_id, data)
    # Different idempotency keys can still describe the same underlying alert.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:fingerprint))"),
        {"fingerprint": fingerprint},
    )
    correlated_incident = await session.scalar(
        select(Incident)
        .where(
            Incident.workspace_id == workspace_id,
            Incident.fingerprint == fingerprint,
            Incident.status != IncidentStatus.RESOLVED.value,
        )
        .with_for_update()
    )
    created = correlated_incident is None
    now = utcnow()
    if correlated_incident is None:
        correlated_incident = Incident(
            id=uuid4(),
            workspace_id=workspace_id,
            service_id=data.service_id,
            title=data.summary,
            description=None,
            severity=data.severity.value,
            status=IncidentStatus.OPEN.value,
            source=IncidentSource.EVENT_INGESTION.value,
            fingerprint=fingerprint,
            version=1,
            opened_at=now,
        )
        session.add(correlated_incident)
        session.add(
            timeline_event(
                correlated_incident,
                EventType.INCIDENT_CREATED,
                "Incident created from external event",
                ActorType.SYSTEM,
                occurred_at=now,
            )
        )
    event = IngestedEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        service_id=data.service_id,
        idempotency_key=idempotency_key,
        external_id=data.external_id,
        source=data.source,
        event_type=data.event_type,
        severity=data.severity.value,
        summary=data.summary,
        error_signature=data.error_signature,
        attributes={**data.attributes, "_opspilot_result": "created" if created else "correlated"},
        fingerprint=fingerprint,
        occurred_at=data.occurred_at,
        incident_id=correlated_incident.id,
    )
    session.add(event)
    session.add(
        timeline_event(
            correlated_incident,
            EventType.ALERT_RECEIVED,
            data.summary,
            ActorType.INTEGRATION,
            {"event_id": str(event.id), "source": data.source},
            data.occurred_at,
            actor_id,
        )
    )
    await session.commit()
    return EventIngestResponse(
        event_id=event.id,
        incident_id=correlated_incident.id,
        result="created" if created else "correlated",
    )


async def publish_notification(
    redis: Redis,
    workspace_id: UUID,
    incident_id: UUID,
    event_type: str,
    data: dict[str, object] | None = None,
) -> None:
    envelope = RealtimeEnvelope(
        event_id=uuid4(),
        type=event_type,
        workspace_id=workspace_id,
        incident_id=incident_id,
        occurred_at=utcnow(),
        data=data or {},
    )
    await redis.publish(f"opspilot:workspace:{workspace_id}:incidents", envelope.model_dump_json())
