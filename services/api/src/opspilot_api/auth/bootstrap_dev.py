"""Idempotent development bootstrap: local users and memberships for existing workspaces.

``python -m opspilot_api.auth.bootstrap_dev`` migrates anonymous development data to explicit
ownership. It creates no credentials (sign-in uses the local development provider) and refuses to
run outside development/test.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.models import MembershipStatus, Role, User, WorkspaceMembership
from opspilot_api.auth.providers import LOCAL_PROVIDER_NAME
from opspilot_api.auth.router import LOCAL_USERS
from opspilot_api.config import Environment, Settings, get_settings
from opspilot_api.incidents.models import Workspace
from opspilot_api.infrastructure import create_db_engine, create_session_factory

DEMO_SLUG = "demo-operations"


async def _user(session: AsyncSession, subject: str, email: str, name: str) -> User:
    user = await session.scalar(
        select(User).where(
            User.identity_provider == LOCAL_PROVIDER_NAME, User.external_subject == subject
        )
    )
    if user is None:
        user = User(
            identity_provider=LOCAL_PROVIDER_NAME,
            external_subject=subject,
            email=email,
            display_name=name,
            status="active",
        )
        session.add(user)
        await session.flush()
    return user


async def _membership(session: AsyncSession, workspace_id: object, user: User, role: Role) -> bool:
    existing = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    if existing is not None:
        return False
    session.add(
        WorkspaceMembership(
            workspace_id=workspace_id,
            user_id=user.id,
            role=role.value,
            status=MembershipStatus.ACTIVE.value,
        )
    )
    return True


async def bootstrap(session: AsyncSession, settings: Settings) -> dict[str, int]:
    if settings.APP_ENV not in {Environment.DEVELOPMENT, Environment.TEST}:
        raise RuntimeError("development bootstrap is disabled outside development and test")
    if settings.AUTH_PROVIDER != "local":
        raise RuntimeError("development bootstrap requires AUTH_PROVIDER=local")
    users: dict[str, User] = {}
    for key, (subject, email, name) in LOCAL_USERS.items():
        users[key] = await _user(session, subject, email, name)
    workspaces = list((await session.scalars(select(Workspace))).all())
    created = 0
    for workspace in workspaces:
        created += await _membership(session, workspace.id, users["owner"], Role.OWNER)
        if workspace.slug == DEMO_SLUG:  # one workspace carries every role for RBAC exercises
            for key, role in (
                ("admin", Role.ADMIN),
                ("responder", Role.RESPONDER),
                ("analyst", Role.ANALYST),
                ("viewer", Role.VIEWER),
            ):
                created += await _membership(session, workspace.id, users[key], role)
    await session.commit()
    return {"users": len(users), "workspaces": len(workspaces), "memberships_created": created}


async def main() -> None:
    settings = get_settings()
    engine = create_db_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            print(await bootstrap(session, settings))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
