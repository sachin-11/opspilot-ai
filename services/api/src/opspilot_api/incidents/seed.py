"""Idempotent development data seed. Run with ``python -m opspilot_api.incidents.seed``."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.models import Role, User
from opspilot_api.auth.service import add_member, create_user, normalize_email
from opspilot_api.config import Environment as AppEnvironment
from opspilot_api.config import get_settings
from opspilot_api.incidents.models import (
    Environment,
    Incident,
    IncidentStatus,
    MonitoredService,
    Severity,
    Workspace,
)
from opspilot_api.incidents.schemas import (
    EventIngestRequest,
    IncidentCreate,
    ServiceCreate,
    StatusUpdate,
    WorkspaceCreate,
)
from opspilot_api.incidents.service import (
    create_manual_incident,
    create_service,
    create_workspace,
    ingest_event,
    update_status,
)
from opspilot_api.infrastructure import create_db_engine, create_session_factory


async def ensure_demo_admin(session: AsyncSession) -> User:
    """Create the demo admin on first run. The password must come from the environment."""
    email = os.environ.get("SEED_ADMIN_EMAIL", "demo@opspilot.local")
    user = await session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is not None:
        return user
    password = os.environ.get("SEED_ADMIN_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError(
            "set SEED_ADMIN_PASSWORD (at least 12 characters) to create the demo admin"
        )
    return await create_user(session, email, "Demo Admin", password)


async def seed() -> None:
    settings = get_settings()
    if settings.APP_ENV is AppEnvironment.PRODUCTION:
        raise RuntimeError("development seed is disabled in production")
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            owner = await ensure_demo_admin(session)
            workspace = await session.scalar(
                select(Workspace).where(Workspace.slug == "demo-operations")
            )
            if workspace is None:
                workspace = await create_workspace(
                    session,
                    WorkspaceCreate(name="Demo Operations", slug="demo-operations"),
                    owner.id,
                )
            else:
                await add_member(session, workspace.id, owner.id, Role.ADMIN)

            service_specs = [
                ("Checkout API", "checkout-api", Environment.PRODUCTION),
                ("Payments Worker", "payments-worker", Environment.PRODUCTION),
                ("Customer Portal", "customer-portal", Environment.STAGING),
            ]
            services: dict[str, MonitoredService] = {}
            for name, slug, environment in service_specs:
                service = await session.scalar(
                    select(MonitoredService).where(
                        MonitoredService.workspace_id == workspace.id, MonitoredService.slug == slug
                    )
                )
                if service is None:
                    service = await create_service(
                        session,
                        workspace.id,
                        ServiceCreate(
                            name=name,
                            slug=slug,
                            description=f"Demo {name} service",
                            environment=environment,
                        ),
                    )
                services[slug] = service

            incidents = [
                (
                    "Checkout latency elevated",
                    "checkout-api",
                    Severity.SEV2,
                    IncidentStatus.INVESTIGATING,
                ),
                (
                    "Payment retries backing up",
                    "payments-worker",
                    Severity.SEV1,
                    IncidentStatus.MITIGATED,
                ),
                (
                    "Staging portal login errors",
                    "customer-portal",
                    Severity.SEV3,
                    IncidentStatus.RESOLVED,
                ),
                (
                    "Intermittent webhook delivery",
                    "payments-worker",
                    Severity.SEV4,
                    IncidentStatus.OPEN,
                ),
            ]
            for title, service_slug, severity, target_status in incidents:
                incident = await session.scalar(
                    select(Incident).where(
                        Incident.workspace_id == workspace.id, Incident.title == title
                    )
                )
                if incident is None:
                    incident = await create_manual_incident(
                        session,
                        workspace.id,
                        IncidentCreate(
                            title=title,
                            description="Idempotent demo incident",
                            severity=severity,
                            service_id=services[service_slug].id,
                        ),
                    )
                    if target_status is not IncidentStatus.OPEN:
                        await update_status(
                            session,
                            workspace.id,
                            incident.id,
                            StatusUpdate(status=target_status, version=incident.version),
                        )

            occurred_at = datetime.now(UTC) - timedelta(minutes=5)
            event = EventIngestRequest(
                service_id=services["checkout-api"].id,
                external_id="demo-alert-1",
                source="demo-monitor",
                event_type="http_error_rate",
                severity=Severity.SEV2,
                summary="Checkout API 5xx error rate exceeded threshold",
                error_signature="checkout.http.5xx",
                attributes={"error_rate": 0.18, "threshold": 0.05, "region": "ap-south-1"},
                occurred_at=occurred_at,
            )
            await ingest_event(session, workspace.id, "demo-checkout-alert-1", event)
            await ingest_event(
                session,
                workspace.id,
                "demo-checkout-alert-2",
                event.model_copy(
                    update={
                        "external_id": "demo-alert-2",
                        "occurred_at": occurred_at + timedelta(minutes=2),
                    }
                ),
            )
            print(f"Seed complete. Workspace ID: {workspace.id}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
