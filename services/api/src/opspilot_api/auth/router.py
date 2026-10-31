"""Auth, membership, invitation, API-key, audit, and local-IdP endpoints (thin adapters)."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode, urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth import service
from opspilot_api.auth.audit import verify_event
from opspilot_api.auth.context import ForbiddenError, UnauthorizedError
from opspilot_api.auth.dependencies import CurrentActor, CurrentPrincipal, RequestMeta
from opspilot_api.auth.models import AuditEvent, Role
from opspilot_api.auth.permissions import ROLE_PERMISSIONS, SCOPES, Permission, permissions_for
from opspilot_api.auth.providers import issue_local_token
from opspilot_api.auth.tickets import ticket_key
from opspilot_api.config import Environment, Settings
from opspilot_api.incidents.schemas import WorkspaceResponse
from opspilot_api.incidents.service import NotFoundError, ValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["auth"])

LOCAL_USERS: dict[str, tuple[str, str, str]] = {
    "owner": ("local-owner", "owner@local.test", "Local Owner"),
    "admin": ("local-admin", "admin@local.test", "Local Admin"),
    "responder": ("local-responder", "responder@local.test", "Local Responder"),
    "analyst": ("local-analyst", "analyst@local.test", "Local Analyst"),
    "viewer": ("local-viewer", "viewer@local.test", "Local Viewer"),
}
WS_CHANNELS = {"incidents"}


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


# ---- identity --------------------------------------------------------------------------------
class MeResponse(BaseModel):
    id: UUID
    email: str | None
    display_name: str | None
    identity_provider: str
    status: str


class MembershipSummary(BaseModel):
    workspace: WorkspaceResponse
    membership_id: UUID
    role: str
    permissions: list[str]


@router.get("/auth/me", response_model=MeResponse)
async def me(principal: CurrentPrincipal, session: Session) -> MeResponse:
    from opspilot_api.auth.models import User

    user = await session.get(User, principal.user_id)
    if user is None:
        raise UnauthorizedError("account not found")
    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        identity_provider=user.identity_provider,
        status=user.status,
    )


@router.get("/auth/workspaces", response_model=list[MembershipSummary])
async def my_workspaces(principal: CurrentPrincipal, session: Session) -> list[MembershipSummary]:
    if principal.user_id is None:
        return []
    rows = await service.list_user_workspaces(session, principal.user_id)
    return [
        MembershipSummary(
            workspace=WorkspaceResponse.model_validate(workspace),
            membership_id=membership.id,
            role=membership.role,
            permissions=sorted(p.value for p in permissions_for(membership.role)),
        )
        for workspace, membership in rows
    ]


# ---- WebSocket tickets ------------------------------------------------------------------------
class TicketRequest(BaseModel):
    workspace_id: UUID
    channel: str = "incidents"


class TicketResponse(BaseModel):
    ticket: str
    expires_in_seconds: int
    channel: str


@router.post("/auth/ws-ticket", response_model=TicketResponse)
async def ws_ticket(
    request: Request,
    data: TicketRequest,
    principal: CurrentPrincipal,
    session: Session,
    meta: RequestMeta,
) -> TicketResponse:
    settings = _settings(request)
    if data.channel not in WS_CHANNELS:
        raise ValidationError("unknown channel")
    actor = await service.resolve_user_actor(session, principal, data.workspace_id)
    if actor is None:
        raise NotFoundError("workspace not found")
    actor.require(Permission.WORKSPACE_READ)
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "user_id": str(principal.user_id),
            "workspace_id": str(data.workspace_id),
            "membership_id": str(actor.membership_id),
            "channel": data.channel,
        }
    )
    try:
        await request.app.state.redis.set(
            ticket_key(ticket), payload, ex=settings.AUTH_WS_TICKET_TTL_SECONDS
        )
    except RedisError as exc:
        raise ForbiddenError("realtime tickets are temporarily unavailable") from exc
    return TicketResponse(
        ticket=ticket, expires_in_seconds=settings.AUTH_WS_TICKET_TTL_SECONDS, channel=data.channel
    )


# ---- members ---------------------------------------------------------------------------------
class MemberResponse(BaseModel):
    id: UUID
    user_id: UUID
    email: str | None
    display_name: str | None
    role: str
    status: str
    created_at: datetime


class MemberUpdate(BaseModel):
    role: str | None = None
    status: str | None = None


def _member(membership: Any, user: Any) -> MemberResponse:
    return MemberResponse(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        status=membership.status,
        created_at=membership.created_at,
    )


@router.get("/workspaces/{workspace_id}/members", response_model=list[MemberResponse])
async def get_members(actor: CurrentActor, session: Session) -> list[MemberResponse]:
    return [_member(m, u) for m, u in await service.list_members(session, actor)]


@router.patch("/workspaces/{workspace_id}/members/{membership_id}", response_model=MemberResponse)
async def patch_member(
    request: Request,
    membership_id: UUID,
    data: MemberUpdate,
    actor: CurrentActor,
    session: Session,
    meta: RequestMeta,
) -> MemberResponse:
    try:
        membership = await service.update_member(
            session, _settings(request), actor, membership_id, data.role, data.status, meta
        )
    except ValueError as exc:
        raise ValidationError("invalid role") from exc
    from opspilot_api.auth.models import User

    user = await session.get(User, membership.user_id)
    return _member(membership, user)


@router.delete("/workspaces/{workspace_id}/members/{membership_id}", status_code=204)
async def delete_member(
    request: Request, membership_id: UUID, actor: CurrentActor, session: Session, meta: RequestMeta
) -> None:
    await service.remove_member(session, _settings(request), actor, membership_id, meta)


# ---- invitations -----------------------------------------------------------------------------
class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str


class InvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    role: str
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class InvitationCreated(InvitationResponse):
    token: str  # shown exactly once


class InvitationAccept(BaseModel):
    token: str = Field(min_length=20, max_length=200)


@router.post(
    "/workspaces/{workspace_id}/invitations", response_model=InvitationCreated, status_code=201
)
async def post_invitation(
    request: Request,
    data: InvitationCreate,
    actor: CurrentActor,
    session: Session,
    meta: RequestMeta,
) -> InvitationCreated:
    try:
        invitation, token = await service.create_invitation(
            session, _settings(request), actor, data.email, data.role, meta
        )
    except ValueError as exc:
        raise ValidationError("invalid role") from exc
    base = InvitationResponse.model_validate(invitation).model_dump()
    return InvitationCreated(**base, token=token)


@router.get("/workspaces/{workspace_id}/invitations", response_model=list[InvitationResponse])
async def get_invitations(actor: CurrentActor, session: Session) -> list[InvitationResponse]:
    return [
        InvitationResponse.model_validate(i) for i in await service.list_invitations(session, actor)
    ]


@router.post(
    "/workspaces/{workspace_id}/invitations/{invitation_id}/revoke",
    response_model=InvitationResponse,
)
async def revoke_invitation(
    request: Request, invitation_id: UUID, actor: CurrentActor, session: Session, meta: RequestMeta
) -> InvitationResponse:
    invitation = await service.revoke_invitation(
        session, _settings(request), actor, invitation_id, meta
    )
    return InvitationResponse.model_validate(invitation)


class AcceptResponse(BaseModel):
    workspace_id: UUID
    role: str


@router.post("/invitations/accept", response_model=AcceptResponse)
async def accept_invitation(
    request: Request,
    data: InvitationAccept,
    principal: CurrentPrincipal,
    session: Session,
    meta: RequestMeta,
) -> AcceptResponse:
    membership = await service.accept_invitation(
        session, _settings(request), principal, data.token, meta
    )
    return AcceptResponse(workspace_id=membership.workspace_id, role=membership.role)


# ---- API keys --------------------------------------------------------------------------------
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=len(SCOPES))
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    key_prefix: str
    scopes: list[str]
    status: str
    created_by_user_id: UUID
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyResponse):
    key: str  # shown exactly once


@router.post("/workspaces/{workspace_id}/api-keys", response_model=ApiKeyCreated, status_code=201)
async def post_api_key(
    request: Request, data: ApiKeyCreate, actor: CurrentActor, session: Session, meta: RequestMeta
) -> ApiKeyCreated:
    key, full = await service.create_api_key(
        session, _settings(request), actor, data.name, data.scopes, data.expires_in_days, meta
    )
    return ApiKeyCreated(**ApiKeyResponse.model_validate(key).model_dump(), key=full)


@router.get("/workspaces/{workspace_id}/api-keys", response_model=list[ApiKeyResponse])
async def get_api_keys(actor: CurrentActor, session: Session) -> list[ApiKeyResponse]:
    return [ApiKeyResponse.model_validate(k) for k in await service.list_api_keys(session, actor)]


@router.post(
    "/workspaces/{workspace_id}/api-keys/{api_key_id}/rotate", response_model=ApiKeyCreated
)
async def rotate_key(
    request: Request, api_key_id: UUID, actor: CurrentActor, session: Session, meta: RequestMeta
) -> ApiKeyCreated:
    key, full = await service.rotate_api_key(session, _settings(request), actor, api_key_id, meta)
    return ApiKeyCreated(**ApiKeyResponse.model_validate(key).model_dump(), key=full)


@router.post(
    "/workspaces/{workspace_id}/api-keys/{api_key_id}/revoke", response_model=ApiKeyResponse
)
async def revoke_key(
    request: Request, api_key_id: UUID, actor: CurrentActor, session: Session, meta: RequestMeta
) -> ApiKeyResponse:
    key = await service.revoke_api_key(session, _settings(request), actor, api_key_id, meta)
    return ApiKeyResponse.model_validate(key)


# ---- audit -----------------------------------------------------------------------------------
class AuditEventResponse(BaseModel):
    id: UUID
    workspace_id: UUID | None
    actor_type: str
    actor_user_id: UUID | None
    actor_api_key_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    request_id: str
    result: str
    reason_code: str | None
    metadata: dict[str, Any]
    integrity_valid: bool
    occurred_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEventResponse]
    next_cursor: str | None


def _audit_response(settings: Settings, event: AuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        id=event.id,
        workspace_id=event.workspace_id,
        actor_type=event.actor_type,
        actor_user_id=event.actor_user_id,
        actor_api_key_id=event.actor_api_key_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        request_id=event.request_id,
        result=event.result,
        reason_code=event.reason_code,
        metadata=event.event_metadata,
        integrity_valid=verify_event(settings, event),
        occurred_at=event.occurred_at,
    )


@router.get("/workspaces/{workspace_id}/audit-events", response_model=AuditPage)
async def get_audit_events(
    request: Request,
    actor: CurrentActor,
    session: Session,
    action: str | None = None,
    result: str | None = None,
    actor_user_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditPage:
    events, next_cursor = await service.list_audit_events(
        session, actor, action, result, actor_user_id, since, until, cursor, limit
    )
    settings = _settings(request)
    return AuditPage(items=[_audit_response(settings, e) for e in events], next_cursor=next_cursor)


@router.get("/auth/permission-matrix")
async def permission_matrix() -> dict[str, list[str]]:
    return {role.value: sorted(p.value for p in perms) for role, perms in ROLE_PERMISSIONS.items()}


# ---- local development identity provider (OIDC authorization-code + PKCE shape) --------------
def _require_local(settings: Settings) -> None:
    if settings.AUTH_PROVIDER != "local" or settings.APP_ENV not in {
        Environment.DEVELOPMENT,
        Environment.TEST,
    }:
        raise NotFoundError("not found")


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@router.get("/auth/local/users")
async def local_users(request: Request) -> dict[str, list[dict[str, str]]]:
    _require_local(_settings(request))
    return {
        "users": [{"login_hint": k, "email": v[1], "name": v[2]} for k, v in LOCAL_USERS.items()]
    }


@router.get("/auth/local/authorize")
async def local_authorize(
    request: Request,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    login_hint: str = "owner",
    client_id: str = "opspilot-web",
) -> RedirectResponse:
    settings = _settings(request)
    _require_local(settings)
    parsed = urlparse(redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in settings.CORS_ORIGINS or parsed.path != "/api/auth/callback":
        raise ValidationError("redirect_uri is not registered")
    if code_challenge_method != "S256" or login_hint not in LOCAL_USERS:
        raise ValidationError("unsupported PKCE method or user")
    code = secrets.token_urlsafe(32)
    await request.app.state.redis.set(
        "opspilot:localcode:" + hashlib.sha256(code.encode()).hexdigest(),
        json.dumps(
            {
                "challenge": code_challenge,
                "nonce": nonce,
                "user": login_hint,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
            }
        ),
        ex=60,
    )
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302
    )


@router.post("/auth/local/token")
async def local_token(
    request: Request,
    grant_type: Annotated[str, Form()],
    code: Annotated[str, Form()],
    code_verifier: Annotated[str, Form()],
    redirect_uri: Annotated[str, Form()],
) -> dict[str, Any]:
    settings = _settings(request)
    _require_local(settings)
    raw = await request.app.state.redis.getdel(
        "opspilot:localcode:" + hashlib.sha256(code.encode()).hexdigest()
    )
    if grant_type != "authorization_code" or raw is None:
        raise UnauthorizedError("invalid authorization code")
    data = json.loads(raw)
    if data["redirect_uri"] != redirect_uri or not hmac.compare_digest(
        _s256(code_verifier), data["challenge"]
    ):
        raise UnauthorizedError("invalid authorization code")
    subject, email, name = LOCAL_USERS[data["user"]]
    access = issue_local_token(settings, subject, email, name)
    id_token = issue_local_token(settings, subject, email, name, nonce=data["nonce"])
    return {
        "token_type": "Bearer",
        "expires_in": settings.AUTH_LOCAL_TOKEN_TTL_SECONDS,
        "access_token": access,
        "id_token": id_token,
    }


__all__ = ["Role", "router"]
