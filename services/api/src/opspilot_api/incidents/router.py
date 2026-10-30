"""Thin HTTP and WebSocket adapters for the incident domain."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.incidents.models import Environment, IncidentStatus, Severity
from opspilot_api.incidents.schemas import (
    EventIngestRequest,
    EventIngestResponse,
    IncidentCreate,
    IncidentListResponse,
    IncidentResponse,
    NoteCreate,
    RealtimeEnvelope,
    ServiceCreate,
    ServiceResponse,
    SeverityUpdate,
    StatusUpdate,
    TimelineResponse,
    WorkspaceCreate,
    WorkspaceResponse,
)
from opspilot_api.incidents.service import (
    NotFoundError,
    add_note,
    create_manual_incident,
    create_service,
    create_workspace,
    get_incident_response,
    ingest_event,
    list_incidents,
    list_services,
    list_timeline,
    list_workspaces,
    publish_notification,
    require_service,
    require_workspace,
    update_severity,
    update_status,
    utcnow,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def notify(request: Request, workspace_id: UUID, incident_id: UUID, event_type: str) -> None:
    try:
        await publish_notification(request.app.state.redis, workspace_id, incident_id, event_type)
    except RedisError as exc:
        logger.warning("real-time publication failed", extra={"error_class": type(exc).__name__})


@router.post(
    "/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["workspaces"],
)
async def post_workspace(data: WorkspaceCreate, session: Session) -> WorkspaceResponse:
    return WorkspaceResponse.model_validate(await create_workspace(session, data))


@router.get("/workspaces", response_model=list[WorkspaceResponse], tags=["workspaces"])
async def get_workspaces(session: Session) -> list[WorkspaceResponse]:
    return [WorkspaceResponse.model_validate(item) for item in await list_workspaces(session)]


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceResponse, tags=["workspaces"])
async def get_workspace(workspace_id: UUID, session: Session) -> WorkspaceResponse:
    return WorkspaceResponse.model_validate(await require_workspace(session, workspace_id))


@router.post(
    "/workspaces/{workspace_id}/services",
    response_model=ServiceResponse,
    status_code=201,
    tags=["services"],
)
async def post_service(
    workspace_id: UUID, data: ServiceCreate, session: Session
) -> ServiceResponse:
    return ServiceResponse.model_validate(await create_service(session, workspace_id, data))


@router.get(
    "/workspaces/{workspace_id}/services", response_model=list[ServiceResponse], tags=["services"]
)
async def get_services(workspace_id: UUID, session: Session) -> list[ServiceResponse]:
    return [
        ServiceResponse.model_validate(item) for item in await list_services(session, workspace_id)
    ]


@router.get(
    "/workspaces/{workspace_id}/services/{service_id}",
    response_model=ServiceResponse,
    tags=["services"],
)
async def get_service(workspace_id: UUID, service_id: UUID, session: Session) -> ServiceResponse:
    return ServiceResponse.model_validate(await require_service(session, workspace_id, service_id))


@router.post(
    "/workspaces/{workspace_id}/incidents",
    response_model=IncidentResponse,
    status_code=201,
    tags=["incidents"],
)
async def post_incident(
    request: Request, workspace_id: UUID, data: IncidentCreate, session: Session
) -> IncidentResponse:
    incident = await create_manual_incident(session, workspace_id, data)
    await notify(request, workspace_id, incident.id, "incident.created")
    return await get_incident_response(session, workspace_id, incident.id)


@router.get(
    "/workspaces/{workspace_id}/incidents", response_model=IncidentListResponse, tags=["incidents"]
)
async def get_incidents(
    workspace_id: UUID,
    session: Session,
    status_filter: Annotated[list[IncidentStatus] | None, Query(alias="status")] = None,
    severity: Annotated[list[Severity] | None, Query()] = None,
    service_id: UUID | None = None,
    environment: Environment | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> IncidentListResponse:
    return await list_incidents(
        session,
        workspace_id,
        statuses=status_filter or [],
        severities=severity or [],
        service_id=service_id,
        environment=environment.value if environment else None,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/workspaces/{workspace_id}/incidents/{incident_id}",
    response_model=IncidentResponse,
    tags=["incidents"],
)
async def get_incident(workspace_id: UUID, incident_id: UUID, session: Session) -> IncidentResponse:
    return await get_incident_response(session, workspace_id, incident_id)


@router.patch(
    "/workspaces/{workspace_id}/incidents/{incident_id}/status",
    response_model=IncidentResponse,
    tags=["incidents"],
)
async def patch_status(
    request: Request, workspace_id: UUID, incident_id: UUID, data: StatusUpdate, session: Session
) -> IncidentResponse:
    _incident, reopened = await update_status(session, workspace_id, incident_id, data)
    event_type = "incident.reopened" if reopened else "incident.status_changed"
    await notify(request, workspace_id, incident_id, event_type)
    return await get_incident_response(session, workspace_id, incident_id)


@router.patch(
    "/workspaces/{workspace_id}/incidents/{incident_id}/severity",
    response_model=IncidentResponse,
    tags=["incidents"],
)
async def patch_severity(
    request: Request, workspace_id: UUID, incident_id: UUID, data: SeverityUpdate, session: Session
) -> IncidentResponse:
    await update_severity(session, workspace_id, incident_id, data)
    await notify(request, workspace_id, incident_id, "incident.severity_changed")
    return await get_incident_response(session, workspace_id, incident_id)


@router.get(
    "/workspaces/{workspace_id}/incidents/{incident_id}/timeline",
    response_model=list[TimelineResponse],
    tags=["incidents"],
)
async def get_timeline(
    workspace_id: UUID, incident_id: UUID, session: Session
) -> list[TimelineResponse]:
    return [
        TimelineResponse.model_validate(item)
        for item in await list_timeline(session, workspace_id, incident_id)
    ]


@router.post(
    "/workspaces/{workspace_id}/incidents/{incident_id}/notes",
    response_model=TimelineResponse,
    status_code=201,
    tags=["incidents"],
)
async def post_note(
    request: Request, workspace_id: UUID, incident_id: UUID, data: NoteCreate, session: Session
) -> TimelineResponse:
    event = await add_note(session, workspace_id, incident_id, data)
    await notify(request, workspace_id, incident_id, "incident.note_added")
    return TimelineResponse.model_validate(event)


@router.post(
    "/workspaces/{workspace_id}/events",
    response_model=EventIngestResponse,
    status_code=201,
    tags=["events"],
)
async def post_event(
    request: Request,
    workspace_id: UUID,
    data: EventIngestRequest,
    session: Session,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> EventIngestResponse:
    result = await ingest_event(session, workspace_id, idempotency_key, data)
    await notify(
        request,
        workspace_id,
        result.incident_id,
        "incident.created" if result.result == "created" else "incident.event_correlated",
    )
    return result


@router.websocket("/ws/workspaces/{workspace_id}/incidents")
async def incident_socket(websocket: WebSocket, workspace_id: UUID) -> None:
    async with websocket.app.state.session_factory() as session:
        try:
            await require_workspace(session, workspace_id)
        except NotFoundError:
            await websocket.close(code=4404)
            return
    await websocket.accept()
    redis = websocket.app.state.redis
    pubsub = redis.pubsub()
    channel = f"opspilot:workspace:{workspace_id}:incidents"
    try:
        await pubsub.subscribe(channel)
        connected = RealtimeEnvelope(
            event_id=uuid4(),
            type="connection.established",
            workspace_id=workspace_id,
            incident_id=None,
            occurred_at=utcnow(),
            data={},
        )
        await websocket.send_text(connected.model_dump_json())
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and isinstance(message.get("data"), bytes):
                envelope = RealtimeEnvelope.model_validate_json(message["data"])
                await websocket.send_text(envelope.model_dump_json())
            try:
                incoming = await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
                if incoming == "ping":
                    await websocket.send_text("pong")
            except TimeoutError:
                pass
    except (WebSocketDisconnect, RedisError):
        pass
    finally:
        with suppress(RedisError):
            await pubsub.unsubscribe(channel)
        await pubsub.aclose()
