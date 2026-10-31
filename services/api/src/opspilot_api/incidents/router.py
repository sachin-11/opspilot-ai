"""Thin HTTP and WebSocket adapters for the incident domain."""

import asyncio
import json
import logging
from contextlib import suppress
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Header,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from redis.exceptions import RedisError

from opspilot_api.auth.deps import Admin, CurrentUser, Ingestor, Responder, Viewer
from opspilot_api.auth.models import Role
from opspilot_api.auth.service import (
    UnauthorizedError,
    list_memberships,
    principal_for_user,
    user_from_access_token,
)
from opspilot_api.config import Settings
from opspilot_api.dependencies import Session
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
    publish_notification,
    require_service,
    require_workspace,
    update_severity,
    update_status,
    utcnow,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


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
async def post_workspace(
    data: WorkspaceCreate, user: CurrentUser, session: Session
) -> WorkspaceResponse:
    return WorkspaceResponse.model_validate(await create_workspace(session, data, user.id))


@router.get("/workspaces", response_model=list[WorkspaceResponse], tags=["workspaces"])
async def get_workspaces(user: CurrentUser, session: Session) -> list[WorkspaceResponse]:
    return [
        WorkspaceResponse.model_validate(workspace)
        for workspace, _role in await list_memberships(session, user.id)
    ]


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceResponse, tags=["workspaces"])
async def get_workspace(
    workspace_id: UUID, _principal: Viewer, session: Session
) -> WorkspaceResponse:
    return WorkspaceResponse.model_validate(await require_workspace(session, workspace_id))


@router.post(
    "/workspaces/{workspace_id}/services",
    response_model=ServiceResponse,
    status_code=201,
    tags=["services"],
)
async def post_service(
    workspace_id: UUID, data: ServiceCreate, _principal: Admin, session: Session
) -> ServiceResponse:
    return ServiceResponse.model_validate(await create_service(session, workspace_id, data))


@router.get(
    "/workspaces/{workspace_id}/services", response_model=list[ServiceResponse], tags=["services"]
)
async def get_services(
    workspace_id: UUID, _principal: Viewer, session: Session
) -> list[ServiceResponse]:
    return [
        ServiceResponse.model_validate(item) for item in await list_services(session, workspace_id)
    ]


@router.get(
    "/workspaces/{workspace_id}/services/{service_id}",
    response_model=ServiceResponse,
    tags=["services"],
)
async def get_service(
    workspace_id: UUID, service_id: UUID, _principal: Viewer, session: Session
) -> ServiceResponse:
    return ServiceResponse.model_validate(await require_service(session, workspace_id, service_id))


@router.post(
    "/workspaces/{workspace_id}/incidents",
    response_model=IncidentResponse,
    status_code=201,
    tags=["incidents"],
)
async def post_incident(
    request: Request,
    workspace_id: UUID,
    data: IncidentCreate,
    principal: Responder,
    session: Session,
) -> IncidentResponse:
    incident = await create_manual_incident(session, workspace_id, data, principal.actor_id)
    await notify(request, workspace_id, incident.id, "incident.created")
    return await get_incident_response(session, workspace_id, incident.id)


@router.get(
    "/workspaces/{workspace_id}/incidents", response_model=IncidentListResponse, tags=["incidents"]
)
async def get_incidents(
    workspace_id: UUID,
    _principal: Viewer,
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
async def get_incident(
    workspace_id: UUID, incident_id: UUID, _principal: Viewer, session: Session
) -> IncidentResponse:
    return await get_incident_response(session, workspace_id, incident_id)


@router.patch(
    "/workspaces/{workspace_id}/incidents/{incident_id}/status",
    response_model=IncidentResponse,
    tags=["incidents"],
)
async def patch_status(
    request: Request,
    workspace_id: UUID,
    incident_id: UUID,
    data: StatusUpdate,
    principal: Responder,
    session: Session,
) -> IncidentResponse:
    _incident, reopened = await update_status(
        session, workspace_id, incident_id, data, principal.actor_id
    )
    event_type = "incident.reopened" if reopened else "incident.status_changed"
    await notify(request, workspace_id, incident_id, event_type)
    return await get_incident_response(session, workspace_id, incident_id)


@router.patch(
    "/workspaces/{workspace_id}/incidents/{incident_id}/severity",
    response_model=IncidentResponse,
    tags=["incidents"],
)
async def patch_severity(
    request: Request,
    workspace_id: UUID,
    incident_id: UUID,
    data: SeverityUpdate,
    principal: Responder,
    session: Session,
) -> IncidentResponse:
    await update_severity(session, workspace_id, incident_id, data, principal.actor_id)
    await notify(request, workspace_id, incident_id, "incident.severity_changed")
    return await get_incident_response(session, workspace_id, incident_id)


@router.get(
    "/workspaces/{workspace_id}/incidents/{incident_id}/timeline",
    response_model=list[TimelineResponse],
    tags=["incidents"],
)
async def get_timeline(
    workspace_id: UUID, incident_id: UUID, _principal: Viewer, session: Session
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
    request: Request,
    workspace_id: UUID,
    incident_id: UUID,
    data: NoteCreate,
    principal: Responder,
    session: Session,
) -> TimelineResponse:
    event = await add_note(session, workspace_id, incident_id, data, principal.actor_id)
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
    principal: Ingestor,
    session: Session,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> EventIngestResponse:
    result = await ingest_event(session, workspace_id, idempotency_key, data, principal.actor_id)
    await notify(
        request,
        workspace_id,
        result.incident_id,
        "incident.created" if result.result == "created" else "incident.event_correlated",
    )
    return result


WEBSOCKET_AUTH_TIMEOUT_SECONDS = 5.0


async def authenticate_socket(websocket: WebSocket, workspace_id: UUID) -> bool:
    """First-message auth keeps tokens out of URLs and access logs.

    The client must send ``{"type": "auth", "token": "<access token>"}`` right after connecting.
    Closes with 4401 (bad or missing credentials) or 4404 (not a member) and returns False.
    """
    settings: Settings = websocket.app.state.settings
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), WEBSOCKET_AUTH_TIMEOUT_SECONDS)
        message = json.loads(raw)
        token = message["token"] if message.get("type") == "auth" else None
        if not isinstance(token, str):
            raise UnauthorizedError("auth message required")
        async with websocket.app.state.session_factory() as session:
            user = await user_from_access_token(session, settings, token)
            await principal_for_user(session, user.id, workspace_id, Role.VIEWER)
    except (TimeoutError, ValueError, KeyError, AttributeError, UnauthorizedError):
        await websocket.close(code=4401)
        return False
    except NotFoundError:
        await websocket.close(code=4404)
        return False
    return True


@router.websocket("/ws/workspaces/{workspace_id}/incidents")
async def incident_socket(websocket: WebSocket, workspace_id: UUID) -> None:
    await websocket.accept()
    try:
        if not await authenticate_socket(websocket, workspace_id):
            return
    except WebSocketDisconnect:
        return
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
