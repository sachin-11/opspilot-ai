"""Trusted request context: who is acting, in which workspace, with which permissions."""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from opspilot_api.auth.permissions import Permission
from opspilot_api.incidents.service import DomainError


class UnauthorizedError(DomainError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class RateLimitedError(DomainError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AuditUnavailableError(DomainError):
    status_code = 503
    code = "audit_unavailable"


@dataclass(frozen=True)
class RequestInfo:
    request_id: str
    correlation_id: str | None = None
    ip_hash: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "api_key", "system"]
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    api_key_workspace_id: UUID | None = None
    scopes: tuple[str, ...] = ()
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None


@dataclass(frozen=True)
class Actor:
    """Principal bound to one workspace with its resolved permissions (never client supplied)."""

    principal: Principal
    workspace_id: UUID
    role: str | None = None
    membership_id: UUID | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)

    @property
    def is_human(self) -> bool:
        return self.principal.kind == "user"

    @property
    def user_id(self) -> UUID | None:
        return self.principal.user_id

    def has(self, permission: Permission) -> bool:
        return self.is_human and permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Domain-service recheck; system and API-key actors never satisfy human permissions."""
        if not self.has(permission):
            raise ForbiddenError(f"missing permission {permission.value}")


def system_principal() -> Principal:
    return Principal(kind="system")
