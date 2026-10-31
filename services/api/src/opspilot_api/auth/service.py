"""Authentication and membership business logic. Route handlers stay thin."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.models import ApiKey, RefreshToken, Role, User, WorkspaceMember
from opspilot_api.auth.security import (
    InvalidTokenError,
    constant_time_equals,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_token,
    new_api_key,
    new_refresh_token,
    password_needs_rehash,
    split_api_key,
    verify_password,
)
from opspilot_api.config import Settings
from opspilot_api.incidents.models import Workspace
from opspilot_api.incidents.service import ConflictError, DomainError, NotFoundError

logger = logging.getLogger(__name__)


class UnauthorizedError(DomainError):
    status_code = 401
    code = "unauthorized"

    @property
    def headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": "Bearer"}


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class RateLimitedError(DomainError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)

    @property
    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)}


class DependencyUnavailableError(DomainError):
    status_code = 503
    code = "dependency_unavailable"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class Principal:
    """The authenticated caller acting inside one workspace."""

    workspace_id: UUID
    role: Role
    user_id: UUID | None
    api_key_id: UUID | None

    @property
    def actor_id(self) -> str:
        if self.user_id is not None:
            return f"user:{self.user_id}"
        return f"api_key:{self.api_key_id}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


# --- rate limiting -----------------------------------------------------------------------------


async def enforce_rate_limit(redis: Redis, key: str, limit: int, window_seconds: int) -> None:
    """Fixed-window counter in Redis. Fails closed: if Redis is down the request is refused."""
    redis_key = f"opspilot:ratelimit:{key}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds, nx=True)
            pipe.ttl(redis_key)
            count, _, ttl = await pipe.execute()
    except RedisError as exc:
        logger.error("rate limiter unavailable", extra={"error_class": type(exc).__name__})
        raise DependencyUnavailableError("rate limiter unavailable") from exc
    if int(count) > limit:
        raise RateLimitedError("too many requests", int(ttl) if int(ttl) > 0 else window_seconds)


# --- users and login ---------------------------------------------------------------------------


async def create_user(session: AsyncSession, email: str, display_name: str, password: str) -> User:
    user = User(
        id=uuid4(),
        email=normalize_email(email),
        display_name=display_name.strip(),
        password_hash=hash_password(password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("an account with this email already exists") from exc
    await session.refresh(user)
    return user


async def issue_tokens(
    session: AsyncSession, settings: Settings, user: User, family_id: UUID | None = None
) -> TokenPair:
    refresh = new_refresh_token()
    now = utcnow()
    session.add(
        RefreshToken(
            id=uuid4(),
            user_id=user.id,
            family_id=family_id or uuid4(),
            token_hash=hash_token(refresh),
            expires_at=now + timedelta(days=settings.AUTH_REFRESH_TOKEN_DAYS),
        )
    )
    await session.commit()
    ttl = settings.AUTH_ACCESS_TOKEN_MINUTES
    access = create_access_token(user.id, settings.AUTH_JWT_SECRET.get_secret_value(), ttl)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=ttl * 60)


async def login(session: AsyncSession, settings: Settings, email: str, password: str) -> TokenPair:
    user = await session.scalar(select(User).where(User.email == normalize_email(email)))
    valid = verify_password(password, user.password_hash if user else None)
    if user is None or not valid or not user.is_active:
        raise UnauthorizedError("invalid email or password")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return await issue_tokens(session, settings, user)


async def refresh_tokens(
    session: AsyncSession, settings: Settings, refresh_token: str
) -> TokenPair:
    stored = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(refresh_token))
        .with_for_update()
    )
    if stored is None:
        raise UnauthorizedError("invalid refresh token")
    now = utcnow()
    if stored.revoked_at is not None:
        # A rotated token was replayed: assume theft and kill the whole session family.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == stored.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await session.commit()
        logger.warning("refresh token reuse detected", extra={"user_id": str(stored.user_id)})
        raise UnauthorizedError("invalid refresh token")
    user = await session.get(User, stored.user_id)
    if stored.expires_at <= now or user is None or not user.is_active:
        raise UnauthorizedError("invalid refresh token")
    stored.revoked_at = now
    return await issue_tokens(session, settings, user, stored.family_id)


async def logout(session: AsyncSession, refresh_token: str) -> None:
    stored = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
    )
    if stored is None:
        return
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == stored.family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await session.commit()


async def user_from_access_token(session: AsyncSession, settings: Settings, token: str) -> User:
    try:
        user_id = decode_access_token(token, settings.AUTH_JWT_SECRET.get_secret_value())
    except InvalidTokenError as exc:
        raise UnauthorizedError("invalid or expired access token") from exc
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("invalid or expired access token")
    return user


# --- membership --------------------------------------------------------------------------------


async def principal_for_user(
    session: AsyncSession, user_id: UUID, workspace_id: UUID, minimum: Role
) -> Principal:
    member = await session.get(WorkspaceMember, (workspace_id, user_id))
    if member is None:
        # Same answer as a missing workspace so workspace IDs cannot be probed.
        raise NotFoundError("workspace not found")
    role = Role.parse(member.role)
    if role < minimum:
        raise ForbiddenError(f"requires the {minimum.label} role")
    return Principal(workspace_id=workspace_id, role=role, user_id=user_id, api_key_id=None)


async def list_memberships(session: AsyncSession, user_id: UUID) -> list[tuple[Workspace, Role]]:
    rows = await session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.created_at, Workspace.id)
    )
    return [(workspace, Role.parse(role)) for workspace, role in rows.all()]


async def add_member(
    session: AsyncSession, workspace_id: UUID, user_id: UUID, role: Role
) -> WorkspaceMember:
    member = await session.get(WorkspaceMember, (workspace_id, user_id))
    if member is None:
        member = WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role.label)
        session.add(member)
    else:
        await _guard_last_admin(session, member, new_role=role)
        member.role = role.label
    await session.commit()
    await session.refresh(member)
    return member


async def invite_member(
    session: AsyncSession, workspace_id: UUID, email: str, role: Role
) -> tuple[WorkspaceMember, User]:
    user = await session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None or not user.is_active:
        raise NotFoundError("no active user with that email")
    return await add_member(session, workspace_id, user.id, role), user


async def list_members(
    session: AsyncSession, workspace_id: UUID
) -> list[tuple[WorkspaceMember, User]]:
    rows = await session.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(User.email)
    )
    return [(member, user) for member, user in rows.all()]


async def remove_member(session: AsyncSession, workspace_id: UUID, user_id: UUID) -> None:
    member = await session.get(WorkspaceMember, (workspace_id, user_id))
    if member is None:
        raise NotFoundError("member not found")
    await _guard_last_admin(session, member, new_role=None)
    await session.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
        )
    )
    await session.commit()


async def _guard_last_admin(
    session: AsyncSession, member: WorkspaceMember, new_role: Role | None
) -> None:
    if member.role != Role.ADMIN.label or new_role is Role.ADMIN:
        return
    admins = await session.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == member.workspace_id,
            WorkspaceMember.role == Role.ADMIN.label,
        )
        .with_for_update()
    )
    if admins is not None and admins <= 1:
        raise ConflictError("a workspace must keep at least one admin")


# --- API keys ----------------------------------------------------------------------------------


async def create_api_key(
    session: AsyncSession, workspace_id: UUID, name: str, created_by: UUID | None
) -> tuple[ApiKey, str]:
    prefix, full_key = new_api_key()
    key = ApiKey(
        id=uuid4(),
        workspace_id=workspace_id,
        name=name.strip(),
        key_prefix=prefix,
        key_hash=hash_token(full_key),
        created_by=created_by,
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key, full_key


async def list_api_keys(session: AsyncSession, workspace_id: UUID) -> list[ApiKey]:
    result = await session.scalars(
        select(ApiKey).where(ApiKey.workspace_id == workspace_id).order_by(ApiKey.created_at)
    )
    return list(result)


async def revoke_api_key(session: AsyncSession, workspace_id: UUID, key_id: UUID) -> None:
    result = await session.execute(
        update(ApiKey)
        .where(
            ApiKey.id == key_id, ApiKey.workspace_id == workspace_id, ApiKey.revoked_at.is_(None)
        )
        .values(revoked_at=utcnow())
        .returning(ApiKey.id)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        raise NotFoundError("api key not found")
    await session.commit()


async def principal_for_api_key(
    session: AsyncSession, workspace_id: UUID, presented_key: str
) -> Principal:
    prefix = split_api_key(presented_key)
    key = (
        await session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix)) if prefix else None
    )
    if (
        key is None
        or key.revoked_at is not None
        or not constant_time_equals(key.key_hash, hash_token(presented_key))
        or key.workspace_id != workspace_id
    ):
        raise UnauthorizedError("invalid api key")
    key.last_used_at = utcnow()
    await session.commit()
    # Keys may only ingest events; the dependency that accepts them is the sole consumer.
    return Principal(
        workspace_id=workspace_id, role=Role.RESPONDER, user_id=None, api_key_id=key.id
    )
