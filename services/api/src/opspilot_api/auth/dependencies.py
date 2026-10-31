"""The global authorization dependency and request-scoped accessors.

Order of evaluation (every step fails closed): classify route -> authenticate -> resolve workspace
membership/key -> check permission or scope -> rate-limit -> pre-write audit for sensitive routes.
Handlers receive only the trusted :class:`Actor` built here; client-supplied roles, workspace
headers, and actor IDs are never consulted.
"""

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from opspilot_api.auth.audit import hash_ip, record_audit
from opspilot_api.auth.clientip import resolve_client_ip
from opspilot_api.auth.context import (
    Actor,
    ForbiddenError,
    Principal,
    RateLimitedError,
    RequestInfo,
    UnauthorizedError,
)
from opspilot_api.auth.models import AuditResult
from opspilot_api.auth.permissions import Permission
from opspilot_api.auth.providers import AuthError, IdentityProvider
from opspilot_api.auth.ratelimit import RateLimiter
from opspilot_api.auth.route_policy import RoutePolicy, classify
from opspilot_api.auth.service import (
    authenticate_api_key,
    principal_for,
    resolve_user_actor,
    upsert_user,
)
from opspilot_api.config import Settings
from opspilot_api.incidents.service import NotFoundError

logger = logging.getLogger(__name__)
API_KEY_HEADER = "x-api-key"


def request_info(conn: HTTPConnection) -> RequestInfo:
    settings: Settings = conn.app.state.settings
    state = conn.scope.get("state", {})
    request_id = state.get("correlation_id") or "unknown-request"
    client = resolve_client_ip(
        conn.client.host if conn.client else None,
        conn.headers.get("x-forwarded-for"),
        settings.trusted_proxy_networks,
    )
    return RequestInfo(
        request_id=request_id,
        correlation_id=state.get("correlation_id"),
        ip_hash=hash_ip(settings, client),
        user_agent=conn.headers.get("user-agent"),
    )


def _limiter(conn: HTTPConnection) -> RateLimiter | None:
    value: RateLimiter | None = getattr(conn.app.state, "rate_limiter", None)
    return value


async def _check_rate(conn: HTTPConnection, rule: str, identity: str) -> None:
    limiter = _limiter(conn)
    if limiter is None:
        from opspilot_api.auth.ratelimit import RULES

        if RULES[rule].fail_closed:
            raise RateLimitedError("rate limiting is unavailable; try again shortly", 5)
        return
    try:
        await limiter.check(rule, identity)
    except RateLimitedError:
        principal = getattr(conn.state, "principal", None) or Principal(kind="system")
        workspace = conn.path_params.get("workspace_id")
        try:
            workspace_id = UUID(str(workspace)) if workspace else None
        except ValueError:
            workspace_id = None
        await _audit(
            conn,
            principal,
            workspace_id,
            "ratelimit.denied",
            AuditResult.DENIED,
            "rate_limited",
            {"rule": rule},
        )
        raise


async def _audit(
    conn: HTTPConnection,
    principal: Principal,
    workspace_id: UUID | None,
    action: str,
    result: AuditResult,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    fail_closed: bool = False,
) -> None:
    await record_audit(
        conn.app.state.session_factory,
        conn.app.state.settings,
        principal,
        workspace_id,
        action,
        "route",
        None,
        result,
        request_info(conn),
        reason,
        metadata,
        fail_closed=fail_closed,
    )


async def _authenticate(conn: HTTPConnection) -> tuple[Principal, str]:
    """Resolve the caller from exactly one credential. Returns ``(principal, rate identity)``."""
    settings: Settings = conn.app.state.settings
    info = request_info(conn)
    bearer = conn.headers.get("authorization")
    api_key = conn.headers.get(API_KEY_HEADER)
    if conn.query_params.get("api_key") or conn.query_params.get("access_token"):
        raise UnauthorizedError("credentials must not be sent in the query string")
    if bearer and api_key:
        raise UnauthorizedError("send exactly one credential")
    if not bearer and not api_key:
        raise UnauthorizedError("authentication required")
    ip_identity = f"ip:{info.ip_hash}"
    try:
        if api_key:
            async with conn.app.state.session_factory() as session:
                found = await authenticate_api_key(session, settings, api_key)
            if found is None:
                raise UnauthorizedError("invalid API key")
            principal, key = found
            return principal, f"key:{key.id}"
        scheme, _, token = (bearer or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise UnauthorizedError("invalid authorization header")
        provider: IdentityProvider = conn.app.state.identity_provider
        identity = await run_in_threadpool(provider.verify, token.strip())
        async with conn.app.state.session_factory() as session:
            user, established = await upsert_user(session, identity)
        principal = principal_for(user, identity)
        if established:  # a new or refreshed sign-in, not every request
            await _audit(
                conn,
                principal,
                None,
                "auth.login",
                AuditResult.SUCCESS,
                metadata={"provider": identity.provider},
            )
        return principal, f"user:{user.id}"
    except (UnauthorizedError, AuthError) as exc:
        limiter = _limiter(conn)
        if limiter is not None:
            await _check_rate(conn, "auth_failure", ip_identity)
        if isinstance(exc, AuthError):
            raise UnauthorizedError(exc.code.replace("_", " ")) from exc
        raise


async def authorize_request(conn: HTTPConnection) -> None:
    if conn.scope["type"] != "http":
        return  # WebSockets authenticate with single-use tickets in their own handler
    route = conn.scope.get("route")
    template = str(getattr(route, "path", conn.scope["path"]))
    method = conn.scope["method"]
    policy: RoutePolicy | None = classify(method, template)
    conn.state.policy = policy
    if policy is None:
        raise ForbiddenError("route is not authorized for access")
    if policy.public:
        if policy.rate:
            identity = str(conn.path_params.get("public_id") or request_info(conn).ip_hash)
            await _check_rate(conn, policy.rate, identity)
        return
    principal, rate_identity = await _authenticate(conn)
    conn.state.principal = principal
    if principal.kind == "api_key":
        await _check_rate(conn, "api_key", rate_identity)
    if policy.authenticated:
        if principal.kind != "user":
            raise ForbiddenError("this endpoint requires a signed-in user")
        if policy.rate:
            await _check_rate(conn, policy.rate, rate_identity)
        return
    raw = conn.path_params.get("workspace_id")
    try:
        workspace_id = UUID(str(raw))
    except ValueError as exc:
        raise NotFoundError("workspace not found") from exc
    actor = await _resolve_actor(conn, principal, workspace_id, policy)
    conn.state.actor = actor
    if policy.rate:
        await _check_rate(conn, policy.rate, rate_identity)
    if policy.audit:
        await _audit(
            conn,
            principal,
            workspace_id,
            policy.audit,
            AuditResult.SUCCESS,
            metadata={"phase": "authorized", "method": method, "route": template},
            fail_closed=True,
        )
        conn.state.audit_action = policy.audit


async def _resolve_actor(
    conn: HTTPConnection, principal: Principal, workspace_id: UUID, policy: RoutePolicy
) -> Actor:
    permission = policy.permission
    if permission is None:
        raise ForbiddenError("route has no permission mapping")
    if principal.kind == "api_key":
        if principal.api_key_workspace_id != workspace_id:
            raise NotFoundError("workspace not found")  # same answer as a nonexistent workspace
        if policy.api_scope is None or policy.api_scope.value not in principal.scopes:
            await _audit(
                conn,
                principal,
                workspace_id,
                "authz.denied",
                AuditResult.DENIED,
                "scope_missing",
                {"permission": permission.value},
            )
            raise ForbiddenError("API key scope does not permit this operation")
        return Actor(principal=principal, workspace_id=workspace_id, permissions=frozenset())
    async with conn.app.state.session_factory() as session:
        actor = await resolve_user_actor(session, principal, workspace_id)
    if actor is None:
        raise NotFoundError("workspace not found")
    if permission not in actor.permissions:
        await _audit(
            conn,
            principal,
            workspace_id,
            "authz.denied",
            AuditResult.DENIED,
            "permission_missing",
            {"permission": permission.value},
        )
        raise ForbiddenError(f"missing permission {permission.value}")
    return actor


def current_actor(conn: HTTPConnection) -> Actor:
    actor: Actor | None = getattr(conn.state, "actor", None)
    if actor is None:
        raise UnauthorizedError("authentication required")
    return actor


def current_principal(conn: HTTPConnection) -> Principal:
    principal: Principal | None = getattr(conn.state, "principal", None)
    if principal is None:
        raise UnauthorizedError("authentication required")
    return principal


CurrentActor = Annotated[Actor, Depends(current_actor)]
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
RequestMeta = Annotated[RequestInfo, Depends(request_info)]

__all__ = [
    "CurrentActor",
    "CurrentPrincipal",
    "Permission",
    "RequestMeta",
    "authorize_request",
    "current_actor",
    "current_principal",
    "request_info",
]
