"""Identity, membership, invitation, and API-key application services (async).

Every state-changing function rechecks the acting principal's permission, writes its audit event in
the same transaction as the change, and refuses to proceed if that audit event cannot be staged.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opspilot_api.auth.audit import build_event, stage_audit
from opspilot_api.auth.context import (
    Actor,
    ForbiddenError,
    Principal,
    RequestInfo,
    UnauthorizedError,
)
from opspilot_api.auth.models import (
    ApiKey,
    ApiKeyStatus,
    AuditEvent,
    AuditResult,
    MembershipStatus,
    Role,
    User,
    UserStatus,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from opspilot_api.auth.permissions import SCOPES, Permission, permissions_for
from opspilot_api.auth.providers import VerifiedIdentity
from opspilot_api.config import Settings
from opspilot_api.incidents.models import Workspace
from opspilot_api.incidents.service import (
    ConflictError,
    NotFoundError,
    ValidationError,
    normalize_slug,
    utcnow,
)

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
API_KEY_PREFIX = "ospk_"
LOGIN_TOUCH = timedelta(minutes=5)


def normalize_email(value: str) -> str:
    cleaned = value.strip().lower()
    if not _EMAIL.match(cleaned) or len(cleaned) > 320:
        raise ValidationError("invalid email address")
    return cleaned


def hash_secret(value: str) -> str:
    """SHA-256 of a high-entropy random secret (invitation token or API key)."""
    return hashlib.sha256(value.encode()).hexdigest()


# ---- users ---------------------------------------------------------------------------------
async def upsert_user(session: AsyncSession, identity: VerifiedIdentity) -> tuple[User, bool]:
    """Resolve the local user by immutable ``(provider, sub)``; email changes never re-key."""
    query = select(User).where(
        User.identity_provider == identity.provider, User.external_subject == identity.subject
    )
    user = await session.scalar(query)
    if user is None:
        user = User(
            id=uuid4(),
            identity_provider=identity.provider,
            external_subject=identity.subject,
            email=identity.email,
            display_name=identity.display_name,
            avatar_url=identity.avatar_url,
            status=UserStatus.ACTIVE.value,
            last_login_at=utcnow(),
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            user = await session.scalar(query)
            if user is None:
                raise
        return user, True
    if user.status != UserStatus.ACTIVE.value:
        raise UnauthorizedError("account is disabled")
    changed = (
        user.email != identity.email
        or user.display_name != identity.display_name
        or user.avatar_url != identity.avatar_url
        or user.last_login_at is None
        or utcnow() - user.last_login_at > LOGIN_TOUCH
    )
    if changed:
        user.email = identity.email or user.email
        user.display_name = identity.display_name or user.display_name
        user.avatar_url = identity.avatar_url or user.avatar_url
        stale_login = user.last_login_at is None or utcnow() - user.last_login_at > LOGIN_TOUCH
        user.last_login_at = utcnow()
        await session.commit()
        return user, stale_login
    return user, False


def principal_for(user: User, identity: VerifiedIdentity | None = None) -> Principal:
    return Principal(
        kind="user",
        user_id=user.id,
        email=user.email,
        email_verified=bool(identity.email_verified) if identity else False,
        display_name=user.display_name,
    )


async def active_membership(
    session: AsyncSession, user_id: UUID, workspace_id: UUID
) -> WorkspaceMembership | None:
    return await session.scalar(
        select(WorkspaceMembership)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.status == MembershipStatus.ACTIVE.value,
            User.status == UserStatus.ACTIVE.value,
        )
    )


def has_permission_sync(
    session: Session, workspace_id: UUID, user_id: UUID | None, permission: Permission
) -> bool:
    """Synchronous recheck for workers (publication re-authorization at execution time)."""
    if user_id is None:
        return False
    membership = session.scalar(
        select(WorkspaceMembership)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.status == MembershipStatus.ACTIVE.value,
            User.status == UserStatus.ACTIVE.value,
        )
    )
    return membership is not None and permission in permissions_for(membership.role)


async def resolve_user_actor(
    session: AsyncSession, principal: Principal, workspace_id: UUID
) -> Actor | None:
    """The actor for a user in a workspace, or ``None`` if they hold no active membership."""
    if principal.user_id is None:
        return None
    membership = await active_membership(session, principal.user_id, workspace_id)
    if membership is None:
        return None
    return Actor(
        principal=principal,
        workspace_id=workspace_id,
        role=membership.role,
        membership_id=membership.id,
        permissions=permissions_for(membership.role),
    )


async def list_user_workspaces(
    session: AsyncSession, user_id: UUID
) -> list[tuple[Workspace, WorkspaceMembership]]:
    rows = await session.execute(
        select(Workspace, WorkspaceMembership)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.status == MembershipStatus.ACTIVE.value,
        )
        .order_by(Workspace.name, Workspace.id)
    )
    return [(row[0], row[1]) for row in rows.all()]


async def create_workspace_with_owner(
    session: AsyncSession,
    settings: Settings,
    principal: Principal,
    name: str,
    slug: str | None,
    request: RequestInfo,
) -> Workspace:
    if principal.user_id is None:
        raise ForbiddenError("only users can create workspaces")
    workspace = Workspace(id=uuid4(), name=name.strip(), slug=normalize_slug(slug or name))
    session.add(workspace)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("workspace slug already exists") from exc
    session.add(
        WorkspaceMembership(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=principal.user_id,
            role=Role.OWNER.value,
            status=MembershipStatus.ACTIVE.value,
            created_by_user_id=principal.user_id,
        )
    )
    stage_audit(
        session,
        build_event(
            settings,
            principal,
            workspace.id,
            "workspace.create",
            "workspace",
            workspace.id,
            AuditResult.SUCCESS,
            request,
        ),
    )
    await session.commit()
    await session.refresh(workspace)
    return workspace


# ---- memberships ---------------------------------------------------------------------------
def _audit(
    session: AsyncSession,
    settings: Settings,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None,
    request: RequestInfo,
    metadata: dict[str, Any] | None = None,
    result: AuditResult = AuditResult.SUCCESS,
) -> None:
    stage_audit(
        session,
        build_event(
            settings,
            actor.principal,
            actor.workspace_id,
            action,
            resource_type,
            resource_id,
            result,
            request,
            None,
            metadata,
        ),
    )


async def list_members(
    session: AsyncSession, actor: Actor
) -> list[tuple[WorkspaceMembership, User]]:
    actor.require(Permission.MEMBERS_READ)
    rows = await session.execute(
        select(WorkspaceMembership, User)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == actor.workspace_id)
        .order_by(WorkspaceMembership.created_at, WorkspaceMembership.id)
    )
    return [(row[0], row[1]) for row in rows.all()]


async def _owners_locked(session: AsyncSession, workspace_id: UUID) -> list[WorkspaceMembership]:
    rows = await session.scalars(
        select(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.role == Role.OWNER.value,
            WorkspaceMembership.status == MembershipStatus.ACTIVE.value,
        )
        .with_for_update()
    )
    return list(rows.all())


async def _target_membership(
    session: AsyncSession, actor: Actor, membership_id: UUID
) -> WorkspaceMembership:
    target = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.id == membership_id,
            WorkspaceMembership.workspace_id == actor.workspace_id,
        )
    )
    if target is None:
        raise NotFoundError("membership not found")
    return target


def _owner_guard(actor: Actor, target: WorkspaceMembership, new_role: str | None) -> None:
    """Only owners may touch owner memberships or grant the owner role."""
    touches_owner = target.role == Role.OWNER.value or new_role == Role.OWNER.value
    if touches_owner and actor.role != Role.OWNER.value:
        raise ForbiddenError("only an owner can grant, change, or remove the owner role")


async def update_member(
    session: AsyncSession,
    settings: Settings,
    actor: Actor,
    membership_id: UUID,
    role: str | None,
    status: str | None,
    request: RequestInfo,
) -> WorkspaceMembership:
    actor.require(Permission.MEMBERS_MANAGE)
    if role is not None:
        Role(role)
    if status is not None and status not in {
        MembershipStatus.ACTIVE.value,
        MembershipStatus.SUSPENDED.value,
    }:
        raise ValidationError("status must be active or suspended; use DELETE to revoke")
    target = await _target_membership(session, actor, membership_id)
    if target.status == MembershipStatus.REVOKED.value:
        raise ConflictError("revoked memberships cannot be modified")
    _owner_guard(actor, target, role)
    owners = await _owners_locked(session, actor.workspace_id)
    ceases_owner = target.role == Role.OWNER.value and (
        (role is not None and role != Role.OWNER.value)
        or status == MembershipStatus.SUSPENDED.value
    )
    if ceases_owner and len(owners) <= 1:
        raise ConflictError("a workspace must keep at least one active owner")
    previous = {"role": target.role, "status": target.status}
    if role is not None:
        target.role = role
    if status is not None:
        target.status = status
    target.updated_at = utcnow()
    _audit(
        session,
        settings,
        actor,
        "membership.update",
        "membership",
        target.id,
        request,
        {
            "previous": previous,
            "role": target.role,
            "status": target.status,
            "target_user_id": str(target.user_id),
        },
    )
    await session.commit()
    return target


async def remove_member(
    session: AsyncSession,
    settings: Settings,
    actor: Actor,
    membership_id: UUID,
    request: RequestInfo,
) -> None:
    actor.require(Permission.MEMBERS_MANAGE)
    target = await _target_membership(session, actor, membership_id)
    _owner_guard(actor, target, None)
    if target.status == MembershipStatus.REVOKED.value:
        return
    owners = await _owners_locked(session, actor.workspace_id)
    if target.role == Role.OWNER.value and len(owners) <= 1:
        raise ConflictError("a workspace must keep at least one active owner")
    target.status = MembershipStatus.REVOKED.value
    target.revoked_at = utcnow()
    target.updated_at = utcnow()
    _audit(
        session,
        settings,
        actor,
        "membership.revoke",
        "membership",
        target.id,
        request,
        {"target_user_id": str(target.user_id), "role": target.role},
    )
    await session.commit()


# ---- invitations ---------------------------------------------------------------------------
async def create_invitation(
    session: AsyncSession,
    settings: Settings,
    actor: Actor,
    email: str,
    role: str,
    request: RequestInfo,
) -> tuple[WorkspaceInvitation, str]:
    actor.require(Permission.MEMBERS_MANAGE)
    Role(role)
    if role == Role.OWNER.value and actor.role != Role.OWNER.value:
        raise ForbiddenError("only an owner can invite another owner")
    token = secrets.token_urlsafe(32)
    invitation = WorkspaceInvitation(
        id=uuid4(),
        workspace_id=actor.workspace_id,
        email=normalize_email(email),
        role=role,
        token_hash=hash_secret(token),
        expires_at=utcnow() + timedelta(hours=settings.AUTH_INVITATION_TTL_HOURS),
        created_by_user_id=actor.user_id,
    )
    session.add(invitation)
    _audit(
        session,
        settings,
        actor,
        "invitation.create",
        "invitation",
        invitation.id,
        request,
        {"role": role},  # the email is deliberately not recorded
    )
    await session.commit()
    await session.refresh(invitation)
    return invitation, token


async def list_invitations(session: AsyncSession, actor: Actor) -> list[WorkspaceInvitation]:
    actor.require(Permission.MEMBERS_MANAGE)
    rows = await session.scalars(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.workspace_id == actor.workspace_id)
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id)
        .limit(200)
    )
    return list(rows.all())


async def revoke_invitation(
    session: AsyncSession,
    settings: Settings,
    actor: Actor,
    invitation_id: UUID,
    request: RequestInfo,
) -> WorkspaceInvitation:
    actor.require(Permission.MEMBERS_MANAGE)
    invitation = await session.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.workspace_id == actor.workspace_id,
        )
    )
    if invitation is None:
        raise NotFoundError("invitation not found")
    if invitation.accepted_at is not None:
        raise ConflictError("invitation was already accepted")
    if invitation.revoked_at is None:
        invitation.revoked_at = utcnow()
        _audit(session, settings, actor, "invitation.revoke", "invitation", invitation.id, request)
        await session.commit()
    return invitation


async def accept_invitation(
    session: AsyncSession,
    settings: Settings,
    principal: Principal,
    token: str,
    request: RequestInfo,
) -> WorkspaceMembership:
    """Single-use acceptance bound to the authenticated, verified-email user.

    Every failure returns one error, so callers learn nothing about which emails or tokens exist.
    """
    generic = NotFoundError("invitation is invalid, expired, or not addressed to you")
    if principal.user_id is None:
        raise ForbiddenError("only users can accept invitations")
    invitation = await session.scalar(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.token_hash == hash_secret(token))
        .with_for_update()
    )
    now = utcnow()
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
        or not principal.email_verified
        or principal.email is None
        or not hmac.compare_digest(invitation.email, principal.email.strip().lower())
    ):
        await session.rollback()
        raise generic
    existing = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == invitation.workspace_id,
            WorkspaceMembership.user_id == principal.user_id,
        )
    )
    if existing is None:
        membership = WorkspaceMembership(
            id=uuid4(),
            workspace_id=invitation.workspace_id,
            user_id=principal.user_id,
            role=invitation.role,
            status=MembershipStatus.ACTIVE.value,
            created_by_user_id=invitation.created_by_user_id,
        )
        session.add(membership)
    else:
        membership = existing
        if existing.status == MembershipStatus.ACTIVE.value and existing.role == Role.OWNER.value:
            await session.rollback()
            raise generic  # an invitation can never demote an owner
        membership.role = invitation.role
        membership.status = MembershipStatus.ACTIVE.value
        membership.revoked_at = None
    invitation.accepted_at = now
    invitation.accepted_by_user_id = principal.user_id
    stage_audit(
        session,
        build_event(
            settings,
            principal,
            invitation.workspace_id,
            "invitation.accept",
            "invitation",
            invitation.id,
            AuditResult.SUCCESS,
            request,
            None,
            {"role": invitation.role},
        ),
    )
    await session.commit()
    return membership


# ---- API keys ------------------------------------------------------------------------------
def generate_api_key() -> tuple[str, str, str]:
    """Return ``(full_key, prefix, hash)``; the full key is shown to the caller exactly once."""
    prefix = API_KEY_PREFIX + secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"{prefix}_{secret}"
    return full, prefix, hash_secret(full)


def _validate_scopes(scopes: list[str]) -> list[str]:
    unique = sorted(set(scopes))
    if not unique or any(scope not in SCOPES for scope in unique):
        raise ValidationError("unknown or empty API-key scopes")
    return unique


async def create_api_key(
    session: AsyncSession,
    settings: Settings,
    actor: Actor,
    name: str,
    scopes: list[str],
    expires_in_days: int | None,
    request: RequestInfo,
) -> tuple[ApiKey, str]:
    actor.require(Permission.API_KEYS_MANAGE)
    clean = _validate_scopes(scopes)
    active = await session.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(
            ApiKey.workspace_id == actor.workspace_id, ApiKey.status == ApiKeyStatus.ACTIVE.value
        )
    )
    if (active or 0) >= settings.AUTH_API_KEY_MAX_ACTIVE:
        raise ConflictError("maximum number of active API keys reached")
    days = min(
        expires_in_days or settings.AUTH_API_KEY_MAX_TTL_DAYS, settings.AUTH_API_KEY_MAX_TTL_DAYS
    )
    full, prefix, digest = generate_api_key()
    key = ApiKey(
        id=uuid4(),
        workspace_id=actor.workspace_id,
        name=name.strip()[:120],
        key_prefix=prefix,
        key_hash=digest,
        scopes=clean,
        status=ApiKeyStatus.ACTIVE.value,
        created_by_user_id=actor.user_id,
        expires_at=utcnow() + timedelta(days=days),
    )
    session.add(key)
    _audit(
        session,
        settings,
        actor,
        "api_key.create",
        "api_key",
        key.id,
        request,
        {"scopes": clean, "prefix": prefix},
    )
    await session.commit()
    await session.refresh(key)
    return key, full


async def _key(session: AsyncSession, actor: Actor, key_id: UUID) -> ApiKey:
    key = await session.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.workspace_id == actor.workspace_id)
    )
    if key is None:
        raise NotFoundError("API key not found")
    return key


async def list_api_keys(session: AsyncSession, actor: Actor) -> list[ApiKey]:
    actor.require(Permission.API_KEYS_MANAGE)
    rows = await session.scalars(
        select(ApiKey)
        .where(ApiKey.workspace_id == actor.workspace_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id)
    )
    keys = list(rows.all())
    now = utcnow()
    for key in keys:  # surface expiry without a background job
        if key.status == ApiKeyStatus.ACTIVE.value and key.expires_at and key.expires_at <= now:
            key.status = ApiKeyStatus.EXPIRED.value
    return keys


async def revoke_api_key(
    session: AsyncSession, settings: Settings, actor: Actor, key_id: UUID, request: RequestInfo
) -> ApiKey:
    actor.require(Permission.API_KEYS_MANAGE)
    key = await _key(session, actor, key_id)
    if key.status != ApiKeyStatus.REVOKED.value:
        key.status = ApiKeyStatus.REVOKED.value
        key.revoked_at = utcnow()
        _audit(
            session,
            settings,
            actor,
            "api_key.revoke",
            "api_key",
            key.id,
            request,
            {"prefix": key.key_prefix},
        )
        await session.commit()
    return key


async def rotate_api_key(
    session: AsyncSession, settings: Settings, actor: Actor, key_id: UUID, request: RequestInfo
) -> tuple[ApiKey, str]:
    actor.require(Permission.API_KEYS_MANAGE)
    old = await _key(session, actor, key_id)
    if old.status != ApiKeyStatus.ACTIVE.value:
        raise ConflictError("only active keys can be rotated")
    old.status = ApiKeyStatus.REVOKED.value
    old.revoked_at = utcnow()
    full, prefix, digest = generate_api_key()
    new = ApiKey(
        id=uuid4(),
        workspace_id=actor.workspace_id,
        name=old.name,
        key_prefix=prefix,
        key_hash=digest,
        scopes=list(old.scopes),
        status=ApiKeyStatus.ACTIVE.value,
        created_by_user_id=actor.user_id,
        expires_at=old.expires_at,
    )
    session.add(new)
    _audit(
        session,
        settings,
        actor,
        "api_key.rotate",
        "api_key",
        new.id,
        request,
        {"replaced": str(old.id), "prefix": prefix},
    )
    await session.commit()
    await session.refresh(new)
    return new, full


async def authenticate_api_key(
    session: AsyncSession, settings: Settings, raw: str
) -> tuple[Principal, ApiKey] | None:
    """Resolve a presented key. Lookup is by prefix; the hash comparison is constant-time."""
    if not raw.startswith(API_KEY_PREFIX) or len(raw) > 128:
        return None
    prefix = raw.split("_", 2)
    if len(prefix) != 3:
        return None
    key = await session.scalar(
        select(ApiKey).where(ApiKey.key_prefix == f"{prefix[0]}_{prefix[1]}")
    )
    candidate = hash_secret(raw)
    stored = key.key_hash if key is not None else "0" * 64
    matches = hmac.compare_digest(candidate, stored)
    if key is None or not matches:
        return None
    now = utcnow()
    if key.status != ApiKeyStatus.ACTIVE.value:
        return None
    if key.expires_at is not None and key.expires_at <= now:
        key.status = ApiKeyStatus.EXPIRED.value
        await session.commit()
        return None
    if key.last_used_at is None or now - key.last_used_at > timedelta(
        seconds=settings.AUTH_API_KEY_TOUCH_SECONDS
    ):
        await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))
        await session.commit()
    principal = Principal(
        kind="api_key",
        api_key_id=key.id,
        api_key_workspace_id=key.workspace_id,
        scopes=tuple(key.scopes),
    )
    return principal, key


# ---- audit queries -------------------------------------------------------------------------
def encode_cursor(event: AuditEvent) -> str:
    raw = json.dumps({"t": event.occurred_at.isoformat(), "i": str(event.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(data["t"]), UUID(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError("invalid cursor") from exc


async def list_audit_events(
    session: AsyncSession,
    actor: Actor,
    action: str | None,
    result: str | None,
    actor_user_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[AuditEvent], str | None]:
    actor.require(Permission.AUDIT_READ)
    limit = max(1, min(limit, 200))
    query = select(AuditEvent).where(AuditEvent.workspace_id == actor.workspace_id)
    if action:
        query = query.where(AuditEvent.action == action)
    if result:
        query = query.where(AuditEvent.result == result)
    if actor_user_id:
        query = query.where(AuditEvent.actor_user_id == actor_user_id)
    if since:
        query = query.where(AuditEvent.occurred_at >= since)
    if until:
        query = query.where(AuditEvent.occurred_at <= until)
    if cursor:
        when, last_id = decode_cursor(cursor)
        query = query.where(
            or_(
                AuditEvent.occurred_at < when,
                and_(AuditEvent.occurred_at == when, AuditEvent.id < last_id),
            )
        )
    rows = list(
        (
            await session.scalars(
                query.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor
