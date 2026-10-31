"""Persistence models for identities, memberships, invitations, API keys, and audit events."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_api.incidents.models import Base, TimestampMixin


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    RESPONDER = "responder"
    ANALYST = "analyst"
    VIEWER = "viewer"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class ApiKeyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class AuditActor(StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SYSTEM = "system"
    INTEGRATION = "integration"


class AuditResult(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"


def _pk() -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _workspace_fk(nullable: bool = False) -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=nullable
    )


def _user_fk(nullable: bool = True) -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("identity_provider", "external_subject", name="uq_user_identity"),
        CheckConstraint("status IN ('active','disabled')", name="ck_user_status"),
    )

    id: Mapped[UUID] = _pk()
    identity_provider: Mapped[str] = mapped_column(String(40), nullable=False)
    # Immutable OIDC ``sub``. Email is informational and never an identity key.
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceMembership(TimestampMixin, Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),
        Index("ix_membership_user", "user_id", "status"),
        CheckConstraint(
            "role IN ('owner','admin','responder','analyst','viewer')", name="ck_membership_role"
        ),
        CheckConstraint("status IN ('active','suspended','revoked')", name="ck_membership_status"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    user_id: Mapped[UUID] = _user_fk(nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by_user_id: Mapped[UUID | None] = _user_fk()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceInvitation(Base):
    __tablename__ = "workspace_invitations"
    __table_args__ = (
        Index("uq_invitation_token_hash", "token_hash", unique=True),
        Index("ix_invitation_workspace", "workspace_id", "created_at"),
        CheckConstraint(
            "role IN ('owner','admin','responder','analyst','viewer')", name="ck_invitation_role"
        ),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_user_id: Mapped[UUID | None] = _user_fk()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID] = _user_fk(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("uq_api_key_prefix", "key_prefix", unique=True),
        Index("ix_api_key_workspace", "workspace_id", "status"),
        CheckConstraint("status IN ('active','revoked','expired')", name="ck_api_key_status"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by_user_id: Mapped[UUID] = _user_fk(nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    """Append-only security audit log (UPDATE/DELETE rejected by database triggers)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_workspace_time", "workspace_id", "occurred_at", "id"),
        CheckConstraint(
            "actor_type IN ('user','api_key','system','integration')", name="ck_audit_actor"
        ),
        CheckConstraint("result IN ('success','denied','failed')", name="ck_audit_result"),
    )

    id: Mapped[UUID] = _pk()
    workspace_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_api_key_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    source_ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent_summary: Mapped[str | None] = mapped_column(String(120))
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    integrity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
