"""Thin HTTP adapters for authentication, membership, and API keys."""

import hashlib
from uuid import UUID

from fastapi import APIRouter, Request, Response, status

from opspilot_api.auth import service
from opspilot_api.auth.deps import Admin, CurrentUser
from opspilot_api.auth.models import Role
from opspilot_api.auth.schemas import (
    ApiKeyCreate,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    LoginRequest,
    MemberResponse,
    MembershipResponse,
    MemberUpsert,
    MeResponse,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserResponse,
)
from opspilot_api.config import Settings
from opspilot_api.dependencies import Session

router = APIRouter(prefix="/api/v1")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def token_response(pair: service.TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post(
    "/auth/signup",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
async def signup(request: Request, data: SignupRequest, session: Session) -> UserResponse:
    settings: Settings = request.app.state.settings
    if not settings.AUTH_SIGNUP_ENABLED:
        raise service.ForbiddenError("self-service signup is disabled")
    await service.enforce_rate_limit(
        request.app.state.redis,
        f"signup:{client_ip(request)}",
        settings.AUTH_LOGIN_MAX_ATTEMPTS,
        settings.AUTH_LOGIN_WINDOW_SECONDS,
    )
    user = await service.create_user(session, data.email, data.display_name, data.password)
    return UserResponse.model_validate(user)


@router.post("/auth/login", response_model=TokenResponse, tags=["auth"])
async def login(request: Request, data: LoginRequest, session: Session) -> TokenResponse:
    settings: Settings = request.app.state.settings
    redis = request.app.state.redis
    email_digest = hashlib.sha256(service.normalize_email(data.email).encode()).hexdigest()
    limit, window = settings.AUTH_LOGIN_MAX_ATTEMPTS, settings.AUTH_LOGIN_WINDOW_SECONDS
    await service.enforce_rate_limit(redis, f"login:email:{email_digest}", limit, window)
    await service.enforce_rate_limit(redis, f"login:ip:{client_ip(request)}", limit * 3, window)
    return token_response(await service.login(session, settings, data.email, data.password))


@router.post("/auth/refresh", response_model=TokenResponse, tags=["auth"])
async def refresh(request: Request, data: RefreshRequest, session: Session) -> TokenResponse:
    settings: Settings = request.app.state.settings
    return token_response(await service.refresh_tokens(session, settings, data.refresh_token))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
async def logout(data: RefreshRequest, session: Session) -> Response:
    await service.logout(session, data.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=MeResponse, tags=["auth"])
async def me(user: CurrentUser, session: Session) -> MeResponse:
    memberships = await service.list_memberships(session, user.id)
    return MeResponse(
        user=UserResponse.model_validate(user),
        workspaces=[
            MembershipResponse(
                workspace_id=workspace.id,
                name=workspace.name,
                slug=workspace.slug,
                role=role.label,
            )
            for workspace, role in memberships
        ],
    )


def member_response(member_role: str, user_id: UUID, email: str, name: str) -> MemberResponse:
    return MemberResponse(user_id=user_id, email=email, display_name=name, role=member_role)


@router.get(
    "/workspaces/{workspace_id}/members", response_model=list[MemberResponse], tags=["members"]
)
async def get_members(workspace_id: UUID, _admin: Admin, session: Session) -> list[MemberResponse]:
    return [
        member_response(member.role, user.id, user.email, user.display_name)
        for member, user in await service.list_members(session, workspace_id)
    ]


@router.post("/workspaces/{workspace_id}/members", response_model=MemberResponse, tags=["members"])
async def put_member(
    workspace_id: UUID, data: MemberUpsert, _admin: Admin, session: Session
) -> MemberResponse:
    member, user = await service.invite_member(
        session, workspace_id, data.email, Role.parse(data.role)
    )
    return member_response(member.role, user.id, user.email, user.display_name)


@router.delete(
    "/workspaces/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["members"],
)
async def delete_member(
    workspace_id: UUID, user_id: UUID, _admin: Admin, session: Session
) -> Response:
    await service.remove_member(session, workspace_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/workspaces/{workspace_id}/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["api-keys"],
)
async def post_api_key(
    workspace_id: UUID, data: ApiKeyCreate, admin: Admin, session: Session
) -> ApiKeyCreatedResponse:
    key, full_key = await service.create_api_key(session, workspace_id, data.name, admin.user_id)
    return ApiKeyCreatedResponse(**ApiKeyResponse.model_validate(key).model_dump(), key=full_key)


@router.get(
    "/workspaces/{workspace_id}/api-keys", response_model=list[ApiKeyResponse], tags=["api-keys"]
)
async def get_api_keys(workspace_id: UUID, _admin: Admin, session: Session) -> list[ApiKeyResponse]:
    return [
        ApiKeyResponse.model_validate(key)
        for key in await service.list_api_keys(session, workspace_id)
    ]


@router.delete(
    "/workspaces/{workspace_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api-keys"],
)
async def delete_api_key(
    workspace_id: UUID, key_id: UUID, _admin: Admin, session: Session
) -> Response:
    await service.revoke_api_key(session, workspace_id, key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
