"""Realistic demo data for a workspace: ``python -m opspilot_api.maintenance.demo_data``.

Fills an *existing* workspace (so the signed-in user can see it) with several incident scenarios,
real AI investigations produced by the worker (one approved, one rejected, one left waiting for
review), and pipeline signals including a duplicate delivery and a poison payload that lands in the
dead-letter queue. Everything goes through the same services the API uses, so the data is
consistent. It is idempotent: scenarios that already exist (matched by title) are skipped.

Development use only: it refuses to run in production and always needs an explicit workspace.
Investigations need the worker running; signals need the API, Kafka and the consumers running.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.context import Principal
from opspilot_api.auth.models import MembershipStatus, Role, WorkspaceMembership
from opspilot_api.auth.service import resolve_user_actor
from opspilot_api.config import Environment as AppEnvironment
from opspilot_api.config import Settings, get_settings
from opspilot_api.incidents.models import (
    Environment,
    EventType,
    Incident,
    IncidentEvent,
    IncidentStatus,
    MonitoredService,
    Severity,
    Workspace,
)
from opspilot_api.incidents.schemas import (
    EventIngestRequest,
    IncidentCreate,
    NoteCreate,
    ServiceCreate,
    StatusUpdate,
)
from opspilot_api.incidents.service import (
    DomainError,
    add_note,
    create_manual_incident,
    create_service,
    ingest_event,
    require_incident,
    update_status,
)
from opspilot_api.infrastructure import create_db_engine, create_session_factory
from opspilot_api.investigations.models import Investigation, InvestigationStatus, ReviewAction
from opspilot_api.investigations.queue import enqueue_investigation, enqueue_review_resume
from opspilot_api.investigations.schemas import ReviewRequest
from opspilot_api.investigations.service import create_investigation, submit_review
from opspilot_api.observability.loadgen import sign
from opspilot_api.observability.models import (
    ObservabilitySource,
    ReceiptStatus,
    SignalReceipt,
    SourceProvider,
)
from opspilot_api.observability.schemas import SourceCreate
from opspilot_api.observability.service import create_source

TERMINAL_INVESTIGATION = {
    InvestigationStatus.COMPLETED.value,
    InvestigationStatus.REJECTED.value,
    InvestigationStatus.FAILED.value,
    InvestigationStatus.CANCELLED.value,
}
IN_FLIGHT_RECEIPTS = {
    ReceiptStatus.RECEIVED.value,
    ReceiptStatus.PUBLISHED.value,
    ReceiptStatus.NORMALIZED.value,
}

SERVICES: tuple[tuple[str, str, Environment], ...] = (
    ("Checkout API", "checkout-api", Environment.PRODUCTION),
    ("Payments Worker", "payments-worker", Environment.PRODUCTION),
    ("Search API", "search-api", Environment.PRODUCTION),
    ("Inventory Service", "inventory-service", Environment.PRODUCTION),
    ("Notifications", "notifications", Environment.PRODUCTION),
    ("Customer Portal", "customer-portal", Environment.STAGING),
)


@dataclass(frozen=True)
class Alert:
    minutes_ago: float
    summary: str
    signature: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    kind: str  # "status" | "note"
    value: str


@dataclass(frozen=True)
class Scenario:
    service: str
    severity: Severity
    age_minutes: int
    title: str = ""
    description: str = ""
    alerts: tuple[Alert, ...] = ()
    steps: tuple[Step, ...] = ()
    redeliver_first_alert: bool = False
    investigate: bool = False
    review: ReviewAction | None = None
    review_comment: str | None = None

    @property
    def incident_title(self) -> str:
        return self.alerts[0].summary if self.alerts else self.title


def status(value: IncidentStatus) -> Step:
    return Step("status", value.value)


def note(text: str) -> Step:
    return Step("note", text)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        service="checkout-api",
        severity=Severity.SEV1,
        age_minutes=95,
        alerts=tuple(
            Alert(
                minutes,
                "Checkout API 5xx error rate above 15% after release v2.14.0",
                "checkout.http.5xx",
                {
                    "error_rate": rate,
                    "threshold": 0.05,
                    "release": "v2.14.0",
                    "region": "ap-south-1",
                },
            )
            for minutes, rate in (
                (90, 0.16),
                (86, 0.19),
                (81, 0.24),
                (77, 0.31),
                (73, 0.28),
                (70, 0.22),
            )
        ),
        redeliver_first_alert=True,
        steps=(
            status(IncidentStatus.INVESTIGATING),
            note(
                "Error rate jumped within two minutes of the v2.14.0 rollout; feature flag new-pricing is the prime suspect."
            ),
            note(
                "Rolled back to v2.13.4. Watching the error rate for ten minutes before declaring recovery."
            ),
            status(IncidentStatus.MITIGATED),
        ),
        investigate=True,  # left waiting in "awaiting review" to show the human gate
    ),
    Scenario(
        service="payments-worker",
        severity=Severity.SEV2,
        age_minutes=310,
        alerts=(
            Alert(
                300,
                "Payments worker database connection pool exhausted",
                "payments.db.pool",
                {"in_use": 40, "max": 40},
            ),
            Alert(
                296,
                "Payments worker database connection pool exhausted",
                "payments.db.pool",
                {"in_use": 40, "max": 40},
            ),
            Alert(
                290,
                "Payments worker database connection pool exhausted",
                "payments.db.pool",
                {"in_use": 40, "max": 40},
            ),
        ),
        steps=(
            status(IncidentStatus.INVESTIGATING),
            note(
                "Refund job holds connections across an external call; pool was raised from 20 to 40 as a stop-gap."
            ),
            status(IncidentStatus.MITIGATED),
            note(
                "Hotfix releases the connection before the provider call. Pool back to 20, no waits for two hours."
            ),
            status(IncidentStatus.RESOLVED),
        ),
        investigate=True,
        review=ReviewAction.APPROVED,
        review_comment="Root cause matches what we found by hand. Approving for the post-incident review.",
    ),
    Scenario(
        service="search-api",
        severity=Severity.SEV3,
        age_minutes=40,
        alerts=(
            Alert(
                38,
                "Search API p95 latency above 1.2s",
                "search.latency.p95",
                {"p95_ms": 1480, "threshold_ms": 1200},
            ),
        ),
    ),
    Scenario(
        service="customer-portal",
        severity=Severity.SEV3,
        age_minutes=1500,
        title="Staging portal login errors",
        description="Users on staging get a 500 after the identity provider redirect.",
        steps=(
            status(IncidentStatus.INVESTIGATING),
            note(
                "The staging OIDC client secret expired yesterday; the callback fails at token exchange."
            ),
            note("Rotated the secret and redeployed staging."),
            status(IncidentStatus.RESOLVED),
        ),
    ),
    Scenario(
        service="inventory-service",
        severity=Severity.SEV4,
        age_minutes=2900,
        title="Nightly inventory sync delayed",
        description="The 02:00 sync finished 70 minutes late; stock counts were stale during the morning peak.",
        steps=(
            status(IncidentStatus.INVESTIGATING),
            note("Upstream warehouse export was slow. No customer impact beyond stale counts."),
            status(IncidentStatus.RESOLVED),
            status(IncidentStatus.INVESTIGATING),
            note(
                "Reappeared on the next run: the export is slow every night. Reopened to track the permanent fix."
            ),
        ),
    ),
    Scenario(
        service="notifications",
        severity=Severity.SEV2,
        age_minutes=60,
        alerts=tuple(
            Alert(
                minutes,
                "Notification queue lag growing, emails delayed",
                "notifications.queue.lag",
                {"lag_seconds": lag, "dlq_depth": dlq},
            )
            for minutes, lag, dlq in (
                (56, 90, 4),
                (52, 140, 6),
                (48, 210, 9),
                (44, 330, 14),
                (40, 480, 21),
                (36, 640, 30),
                (32, 820, 44),
                (28, 990, 61),
                (24, 1200, 85),
                (20, 1500, 112),
                (15, 1800, 140),
                (10, 2100, 171),
            )
        ),
        steps=(
            status(IncidentStatus.INVESTIGATING),
            note(
                "Consumer lag started right after the queue concurrency change shipped; the email provider status page is green."
            ),
        ),
        investigate=True,
        review=ReviewAction.REJECTED,
        review_comment="Blames the email provider, but its status page is green and the lag began with our concurrency change. Rejecting; rerun with deploy history.",
    ),
)


@dataclass
class Summary:
    incidents_created: int = 0
    incidents_existing: int = 0
    investigations: int = 0
    notes: list[str] = field(default_factory=list)


def print_note(summary: Summary, text: str) -> None:
    summary.notes.append(text)
    print(f"  ! {text}")


async def find_workspace(session: AsyncSession, reference: str) -> Workspace | None:
    try:
        return await session.get(Workspace, uuid.UUID(reference))
    except ValueError:
        return await session.scalar(select(Workspace).where(Workspace.slug == reference))


async def ensure_services(
    session: AsyncSession, workspace_id: uuid.UUID
) -> dict[str, MonitoredService]:
    services: dict[str, MonitoredService] = {}
    for name, slug, environment in SERVICES:
        service = await session.scalar(
            select(MonitoredService).where(
                MonitoredService.workspace_id == workspace_id, MonitoredService.slug == slug
            )
        )
        if service is None:
            service = await create_service(
                session,
                workspace_id,
                ServiceCreate(
                    name=name, slug=slug, description=f"Demo {name}", environment=environment
                ),
            )
        services[slug] = service
    return services


async def find_incident(
    session: AsyncSession, workspace_id: uuid.UUID, title: str
) -> Incident | None:
    return await session.scalar(
        select(Incident).where(Incident.workspace_id == workspace_id, Incident.title == title)
    )


async def create_incident(
    session: AsyncSession, workspace_id: uuid.UUID, scenario: Scenario, service: MonitoredService
) -> Incident:
    if scenario.alerts:
        now = datetime.now(UTC)
        incident_id: uuid.UUID | None = None
        for index, alert in enumerate(scenario.alerts):
            request = EventIngestRequest(
                service_id=service.id,
                external_id=f"demo-{scenario.service}-{index}",
                source="demo-monitor",
                event_type="alert",
                severity=scenario.severity,
                summary=alert.summary,
                error_signature=alert.signature,
                attributes=alert.attributes,
                occurred_at=now - timedelta(minutes=alert.minutes_ago),
            )
            result = await ingest_event(
                session,
                workspace_id,
                f"demo:{scenario.service}:{scenario.incident_title}:{index}",
                request,
            )
            incident_id = result.incident_id
            if index == 0 and scenario.redeliver_first_alert:
                # The monitor retries the same delivery: it must be recognised, not duplicated.
                again = await ingest_event(
                    session,
                    workspace_id,
                    f"demo:{scenario.service}:{scenario.incident_title}:0",
                    request,
                )
                if again.incident_id != result.incident_id or again.event_id != result.event_id:
                    raise RuntimeError("a redelivered alert was not recognised as a duplicate")
        if incident_id is None:
            raise RuntimeError("an alert scenario produced no incident")
        return await require_incident(session, workspace_id, incident_id)
    return await create_manual_incident(
        session,
        workspace_id,
        IncidentCreate(
            title=scenario.title,
            description=scenario.description,
            severity=scenario.severity,
            service_id=service.id,
        ),
    )


async def apply_steps(
    session: AsyncSession, workspace_id: uuid.UUID, incident: Incident, steps: Sequence[Step]
) -> None:
    for step in steps:
        current = await require_incident(session, workspace_id, incident.id)
        if step.kind == "status":
            await update_status(
                session,
                workspace_id,
                incident.id,
                StatusUpdate(status=IncidentStatus(step.value), version=current.version),
            )
        else:
            await add_note(
                session,
                workspace_id,
                incident.id,
                NoteCreate(summary=step.value, version=current.version),
            )


async def age_incident(session: AsyncSession, incident_id: uuid.UUID, age_minutes: int) -> None:
    """Spread the timeline over the scenario's age so the console shows believable history."""
    now = datetime.now(UTC)
    opened = now - timedelta(minutes=age_minutes)
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise RuntimeError("incident disappeared while seeding")
    events = list(
        await session.scalars(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.occurred_at, IncidentEvent.created_at)
        )
    )
    own = [event for event in events if event.event_type != EventType.ALERT_RECEIVED.value]
    # The newest own event lands a little before the newest alert (or "now" if there are no alerts).
    alert_times = [e.occurred_at for e in events if e.event_type == EventType.ALERT_RECEIVED.value]
    end = max(alert_times) + timedelta(minutes=2) if alert_times else now - timedelta(minutes=3)
    span = max((end - opened).total_seconds(), 60.0)
    incident.opened_at = incident.created_at = opened
    incident.acknowledged_at = incident.mitigated_at = incident.resolved_at = None
    for index, event in enumerate(own):
        moment = opened + timedelta(seconds=span * index / max(len(own) - 1, 1))
        event.occurred_at = event.created_at = moment
        target = (event.event_metadata or {}).get("to")
        if event.event_type == EventType.INCIDENT_REOPENED.value:
            incident.resolved_at = incident.mitigated_at = None
        elif event.event_type == EventType.STATUS_CHANGED.value:
            if target == IncidentStatus.INVESTIGATING.value and incident.acknowledged_at is None:
                incident.acknowledged_at = moment
            elif target == IncidentStatus.MITIGATED.value:
                incident.mitigated_at = moment
            elif target == IncidentStatus.RESOLVED.value:
                incident.resolved_at = moment
    for event in events:
        if event.event_type == EventType.ALERT_RECEIVED.value:
            event.created_at = event.occurred_at
    incident.updated_at = max([e.occurred_at for e in events] or [opened])
    await session.commit()


async def settle_review_events(session: AsyncSession, incident_id: uuid.UUID) -> None:
    """Place the investigation outcome shortly after the rest of the (back-dated) timeline.

    The worker records its events at the real current time, which would otherwise land hours
    after an incident that the seed back-dated, and the console would show a review that
    happened after the incident was already resolved.
    """
    review_types = {EventType.INVESTIGATION_COMPLETED.value, EventType.INVESTIGATION_REJECTED.value}
    events = list(
        await session.scalars(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.occurred_at, IncidentEvent.created_at)
        )
    )
    others = [event for event in events if event.event_type not in review_types]
    reviews = [event for event in events if event.event_type in review_types]
    if not others or not reviews:
        return
    now = datetime.now(UTC)
    base = max(event.occurred_at for event in others)
    for index, event in enumerate(reviews):
        event.occurred_at = event.created_at = min(base + timedelta(minutes=8 + 2 * index), now)
    incident = await session.get(Incident, incident_id)
    if incident is not None:
        incident.updated_at = max(event.occurred_at for event in events)
    await session.commit()


async def owner_actor(session: AsyncSession, workspace_id: uuid.UUID) -> Any:
    membership = await session.scalar(
        select(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.role == Role.OWNER.value,
            WorkspaceMembership.status == MembershipStatus.ACTIVE.value,
        )
        .limit(1)
    )
    if membership is None:
        return None
    principal = Principal(kind="user", user_id=membership.user_id)
    return await resolve_user_actor(session, principal, workspace_id)


async def wait_for_investigation(
    factory: Any, investigation_id: uuid.UUID, states: set[str], max_wait: float
) -> str | None:
    deadline = asyncio.get_running_loop().time() + max_wait
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as session:
            current = await session.get(Investigation, investigation_id)
            if current is not None and current.status in states:
                return str(current.status)
        await asyncio.sleep(1.5)
    return None


async def investigate(
    factory: Any,
    settings: Settings,
    workspace_id: uuid.UUID,
    incident: Incident,
    scenario: Scenario,
    max_wait: float,
    summary: Summary,
) -> None:
    async with factory() as session:
        existing = await session.scalar(
            select(func.count())
            .select_from(Investigation)
            .where(Investigation.incident_id == incident.id)
        )
        if existing:
            return
        investigation = await create_investigation(session, settings, workspace_id, incident.id)
    enqueue_investigation(settings, workspace_id, investigation.id)
    summary.investigations += 1
    reached = await wait_for_investigation(
        factory,
        investigation.id,
        {InvestigationStatus.AWAITING_REVIEW.value} | TERMINAL_INVESTIGATION,
        max_wait,
    )
    if reached != InvestigationStatus.AWAITING_REVIEW.value:
        print_note(
            summary,
            f"investigation for '{incident.title}' ended as {reached or 'still running (is the worker up?)'}; no review applied",
        )
        return
    if scenario.review is None:
        return
    async with factory() as session:
        actor = await owner_actor(session, workspace_id)
        if actor is None:
            print_note(
                summary, "no active owner in this workspace; left the investigation awaiting review"
            )
            return
        current = await session.get(Investigation, investigation.id)
        if current is None:
            raise RuntimeError("investigation disappeared while seeding")
        review = await submit_review(
            session,
            settings,
            workspace_id,
            investigation.id,
            ReviewRequest(
                action=scenario.review, comment=scenario.review_comment, version=current.version
            ),
            actor,
        )
    enqueue_review_resume(settings, workspace_id, investigation.id, review.id)
    await wait_for_investigation(factory, investigation.id, TERMINAL_INVESTIGATION, max_wait)
    async with factory() as session:
        await settle_review_events(session, incident.id)


def signal_payloads(now: datetime) -> list[tuple[str, dict[str, Any]]]:
    def at(minutes: float) -> str:
        return (now - timedelta(minutes=minutes)).isoformat()

    alert = {
        "type": "alert",
        "title": "Inventory reservation timeouts",
        "message": "Reservation calls to the warehouse API exceed 3s",
        "severity": "high",
        "error_signature": "inventory.reservation.timeout",
        "resource_type": "service",
        "resource_id": "inventory-service",
    }
    payloads: list[tuple[str, dict[str, Any]]] = [
        ("alert", {**alert, "event_id": "demo-sig-alert-1", "occurred_at": at(30)}),
        ("alert", {**alert, "event_id": "demo-sig-alert-2", "occurred_at": at(26)}),
        ("alert", {**alert, "event_id": "demo-sig-alert-3", "occurred_at": at(21)}),
        (
            "log",
            {
                "type": "log",
                "event_id": "demo-sig-log-1",
                "level": "error",
                "occurred_at": at(25),
                "message": "ReservationClient: ReadTimeout after 3000ms calling warehouse /reserve",
                "error_signature": "inventory.reservation.timeout",
            },
        ),
        (
            "metric",
            {
                "type": "metric",
                "event_id": "demo-sig-metric-1",
                "metric_name": "queue_depth",
                "value": 18250,
                "threshold": 5000,
                "unit": "messages",
                "severity": "medium",
                "occurred_at": at(18),
            },
        ),
    ]
    return payloads


async def seed_signals(
    factory: Any,
    settings: Settings,
    workspace_id: uuid.UUID,
    service: MonitoredService,
    api_url: str,
    summary: Summary,
) -> None:
    async with factory() as session:
        existing = await session.scalar(
            select(ObservabilitySource).where(
                ObservabilitySource.workspace_id == workspace_id,
                ObservabilitySource.name == "Demo monitor",
            )
        )
        if existing is not None:
            print("  signals: the Demo monitor source already exists, skipping")
            return
        try:
            created = await create_source(
                session,
                settings,
                workspace_id,
                SourceCreate(
                    name="Demo monitor",
                    provider=SourceProvider.GENERIC_WEBHOOK,
                    service_id=service.id,
                    environment=Environment.PRODUCTION,
                ),
            )
        except DomainError as exc:
            print_note(summary, f"signals skipped: {exc} (set OBS_SECRET_ENCRYPTION_KEY)")
            return
    if created.webhook_secret is None:
        raise RuntimeError("the new webhook source returned no secret")
    url = f"{api_url.rstrip('/')}/api/v1/ingest/webhooks/{created.public_id}"
    payloads = signal_payloads(datetime.now(UTC))
    payloads.append(("duplicate delivery", payloads[0][1]))  # same event id again
    payloads.append(("poison payload", {"type": "mystery", "event_id": "demo-sig-poison"}))
    codes: list[int] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for _label, payload in payloads:
            body = json.dumps(payload).encode()
            try:
                response = await client.post(
                    url, content=body, headers=sign(created.webhook_secret, body)
                )
            except httpx.HTTPError:
                print_note(
                    summary,
                    f"could not reach the API at {api_url}; signals not sent (use --api-url)",
                )
                return
            codes.append(response.status_code)
    print(f"  signals: sent {len(payloads)} deliveries, HTTP {sorted(set(codes))}")
    deadline = asyncio.get_running_loop().time() + 40
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as session:
            states = list(
                await session.scalars(
                    select(SignalReceipt.status).where(SignalReceipt.source_id == created.id)
                )
            )
        if states and not IN_FLIGHT_RECEIPTS.intersection(states):
            break
        await asyncio.sleep(1.5)
    else:
        print_note(summary, "signals are still in flight; are the Kafka consumers running?")
    counts = {state: states.count(state) for state in sorted(set(states))}
    print(f"  signals processed: {counts}")


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.APP_ENV is AppEnvironment.PRODUCTION:
        print("refusing: demo data is disabled in production", file=sys.stderr)
        return 2
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    summary = Summary()
    try:
        async with factory() as session:
            if args.list:
                for item in await session.scalars(select(Workspace).order_by(Workspace.name)):
                    print(f"{item.slug:30} {item.id}  {item.name}")
                return 0
            if args.retime_existing:
                target = await find_workspace(session, args.workspace)
                if target is None:
                    print(
                        f"workspace '{args.workspace}' not found; run with --list", file=sys.stderr
                    )
                    return 1
                for scenario in SCENARIOS:
                    found = await find_incident(session, target.id, scenario.incident_title)
                    if found is not None:
                        await settle_review_events(session, found.id)
                print("Investigation outcomes re-timed inside each demo incident's timeline.")
                return 0
            workspace = await find_workspace(session, args.workspace)
            if workspace is None:
                print(f"workspace '{args.workspace}' not found; run with --list", file=sys.stderr)
                return 1
            workspace_id = workspace.id
            print(f"Seeding workspace '{workspace.name}' ({workspace.slug})")
            services = await ensure_services(session, workspace_id)
        for scenario in SCENARIOS:
            async with factory() as session:
                existing = await find_incident(session, workspace_id, scenario.incident_title)
                if existing is not None:
                    summary.incidents_existing += 1
                    print(f"  = exists: {scenario.incident_title}")
                    incident = existing
                    new = False
                else:
                    incident = await create_incident(
                        session, workspace_id, scenario, services[scenario.service]
                    )
                    await apply_steps(session, workspace_id, incident, scenario.steps)
                    await age_incident(session, incident.id, scenario.age_minutes)
                    summary.incidents_created += 1
                    new = True
                    print(f"  + created: {scenario.incident_title}")
            if new and scenario.investigate and args.investigations:
                await investigate(
                    factory, settings, workspace_id, incident, scenario, args.wait, summary
                )
        if args.signals:
            await seed_signals(
                factory,
                settings,
                workspace_id,
                services["inventory-service"],
                args.api_url,
                summary,
            )
        print(
            f"\nDone: {summary.incidents_created} incidents created, {summary.incidents_existing} already there, "
            f"{summary.investigations} investigations started."
        )
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--workspace", help="workspace slug or UUID to fill (see --list)")
    parser.add_argument("--list", action="store_true", help="list workspaces and exit")
    parser.add_argument(
        "--retime-existing",
        action="store_true",
        help="move investigation outcomes of already-seeded scenarios inside their timelines",
    )
    parser.add_argument("--no-investigations", dest="investigations", action="store_false")
    parser.add_argument("--no-signals", dest="signals", action="store_false")
    parser.add_argument(
        "--wait", type=float, default=90.0, help="seconds to wait per investigation"
    )
    parser.add_argument(
        "--api-url", default="http://127.0.0.1:8000", help="API address as seen from this process"
    )
    args = parser.parse_args()
    if not args.list and not args.workspace:
        parser.error("--workspace is required (use --list to see choices)")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
