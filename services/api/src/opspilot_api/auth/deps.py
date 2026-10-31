"""FastAPI dependencies that authenticate the caller and enforce workspace roles."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from opspilot_api.auth.models import Role, User
from opspilot_api.auth.service import (
    Principal,
    UnauthorizedError,
    enforce_rate_limit,
    principal_for_api_key,
    principal_for_user,
    user_from_access_token,
)
from opspilot_api.config import Settings
from opspilot_api.dependencies import Session

_bearer = HTTPBearer(auto_error=False)
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


async def get_current_user(request: Request, session: Session, credentials: Credentials) -> User:
    if credentials is None:
        raise UnauthorizedError("authentication required")
    settings: Settings = request.app.state.settings
    return await user_from_access_token(session, settings, credentials.credentials)


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(minimum: Role) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: the caller must hold at least ``minimum`` in the path workspace."""

    async def dependency(workspace_id: UUID, user: CurrentUser, session: Session) -> Principal:
        return await principal_for_user(session, user.id, workspace_id, minimum)

    return dependency


Viewer = Annotated[Principal, Depends(require_role(Role.VIEWER))]
Responder = Annotated[Principal, Depends(require_role(Role.RESPONDER))]
Admin = Annotated[Principal, Depends(require_role(Role.ADMIN))]


async def ingest_principal(
    request: Request,
    workspace_id: UUID,
    session: Session,
    credentials: Credentials,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Event ingestion accepts a workspace API key or a responder-level user token."""
    settings: Settings = request.app.state.settings
    if api_key:
        principal = await principal_for_api_key(session, workspace_id, api_key)
    elif credentials is not None:
        user = await user_from_access_token(session, settings, credentials.credentials)
        principal = await principal_for_user(session, user.id, workspace_id, Role.RESPONDER)
    else:
        raise UnauthorizedError("authentication required")
    await enforce_rate_limit(
        request.app.state.redis,
        f"ingest:{workspace_id}",
        settings.INGEST_RATE_LIMIT_PER_MINUTE,
        60,
    )
    return principal


Ingestor = Annotated[Principal, Depends(ingest_principal)]
