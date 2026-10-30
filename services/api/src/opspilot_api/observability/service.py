"""Observability application services. Every query begins with a workspace predicate."""

import asyncio
import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.config import Environment as AppEnvironment
from opspilot_api.config import Settings
from opspilot_api.incidents.models import MonitoredService
from opspilot_api.incidents.service import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
    utcnow,
)
from opspilot_api.observability.backpressure import consumer_lag
from opspilot_api.observability.bus import EventBus, Topics
from opspilot_api.observability.events import publish_observability_event
from opspilot_api.observability.ingress import IngressRejected, accept_signal
from opspilot_api.observability.models import (
    DeadLetterReplay,
    DeadLetterSignal,
    ObservabilitySource,
    ReceiptStatus,
    SignalReceipt,
    SourceProvider,
    SourceStatus,
)
from opspilot_api.observability.pipeline import RETRYABLE_CODES
from opspilot_api.observability.providers import (
    ObsProviderError,
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderThrottledError,
    provider_for_source,
    validate_configuration,
)
from opspilot_api.observability.schemas import (
    BackpressureStatus,
    DeadLetterPage,
    DeadLetterResponse,
    DemoGenerateRequest,
    DemoGenerateResponse,
    PipelineStatus,
    ReplayResponse,
    SecretRotated,
    SignalDetail,
    SignalPage,
    SourceCreate,
    SourceCreated,
    SourceHealth,
    SourceResponse,
    SourceTestResult,
    SourceUpdate,
)
from opspilot_api.observability.secrets_util import (
    SecretsConfigurationError,
    encrypt_secret,
    generate_public_id,
    generate_webhook_secret,
)
from opspilot_api.observability.signal_schema import CURRENT_SCHEMA_VERSION
from opspilot_api.observability.tools import signal_summary


class SecretsNotConfiguredError(DomainError):
    status_code = 503
    code = "secrets_not_configured"


class DemoDisabledError(DomainError):
    status_code = 404
    code = "demo_disabled"


_SOURCE_TYPES = {
    SourceProvider.GENERIC_WEBHOOK: "webhook",
    SourceProvider.AWS_CLOUDWATCH: "logs_metrics",
    SourceProvider.DEMO_FIXTURE: "fixture",
}
_WEBHOOK_BASE = "/api/v1/ingest/webhooks/"


def source_response(source: ObservabilitySource, service_name: str | None) -> SourceResponse:
    generic = source.provider == SourceProvider.GENERIC_WEBHOOK.value
    return SourceResponse(
        id=source.id,
        workspace_id=source.workspace_id,
        service_id=source.service_id,
        service_name=service_name,
        public_id=source.public_id,
        name=source.name,
        provider=source.provider,
        source_type=source.source_type,
        status=source.status,
        environment=source.environment,
        region=source.region,
        configuration=source.configuration,
        has_secret=bool(source.secret_reference),
        webhook_path=f"{_WEBHOOK_BASE}{source.public_id}" if generic else None,
        last_event_at=source.last_event_at,
        last_success_at=source.last_success_at,
        last_failure_at=source.last_failure_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


async def _service_name(session: AsyncSession, workspace_id: UUID, service_id: UUID) -> str | None:
    return await session.scalar(
        select(MonitoredService.name).where(
            MonitoredService.id == service_id, MonitoredService.workspace_id == workspace_id
        )
    )


async def require_source(
    session: AsyncSession, workspace_id: UUID, source_id: UUID
) -> ObservabilitySource:
    source = await session.scalar(
        select(ObservabilitySource).where(
            ObservabilitySource.id == source_id, ObservabilitySource.workspace_id == workspace_id
        )
    )
    if source is None:
        raise NotFoundError("source not found")
    return source


async def create_source(
    session: AsyncSession, settings: Settings, workspace_id: UUID, data: SourceCreate
) -> SourceCreated:
    service_name = await _service_name(session, workspace_id, data.service_id)
    if service_name is None:
        raise NotFoundError("service not found")
    try:
        configuration = validate_configuration(data.provider, data.configuration, settings)
    except ValueError as exc:
        raise ValidationError(str(exc)[:300]) from exc
    secret: str | None = None
    reference: str | None = None
    if data.provider is SourceProvider.GENERIC_WEBHOOK:
        secret = generate_webhook_secret()
        try:
            reference = encrypt_secret(settings, secret)
        except SecretsConfigurationError as exc:
            raise SecretsNotConfiguredError(
                "Webhook secrets cannot be stored: OBS_SECRET_ENCRYPTION_KEY is not configured"
            ) from exc
    source = ObservabilitySource(
        workspace_id=workspace_id,
        service_id=data.service_id,
        public_id=generate_public_id(),
        name=data.name,
        provider=data.provider.value,
        source_type=_SOURCE_TYPES[data.provider],
        status=SourceStatus.ACTIVE.value,
        environment=data.environment.value,
        region=data.region,
        configuration=configuration,
        secret_reference=reference,
    )
    session.add(source)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("a source with this name already exists") from exc
    await session.refresh(source)
    return SourceCreated(
        **source_response(source, service_name).model_dump(), webhook_secret=secret
    )


async def list_sources(session: AsyncSession, workspace_id: UUID) -> list[SourceResponse]:
    rows = await session.execute(
        select(ObservabilitySource, MonitoredService.name)
        .outerjoin(
            MonitoredService,
            (MonitoredService.id == ObservabilitySource.service_id)
            & (MonitoredService.workspace_id == workspace_id),
        )
        .where(ObservabilitySource.workspace_id == workspace_id)
        .order_by(ObservabilitySource.name)
    )
    return [source_response(source, name) for source, name in rows.all()]


async def get_source(session: AsyncSession, workspace_id: UUID, source_id: UUID) -> SourceResponse:
    source = await require_source(session, workspace_id, source_id)
    return source_response(source, await _service_name(session, workspace_id, source.service_id))


async def update_source(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    source_id: UUID,
    data: SourceUpdate,
) -> tuple[SourceResponse, bool]:
    source = await require_source(session, workspace_id, source_id)
    status_changed = False
    if data.name is not None:
        source.name = data.name.strip()
    if data.environment is not None:
        source.environment = data.environment.value
    if data.region is not None:
        source.region = data.region
    if data.configuration is not None:
        try:
            source.configuration = validate_configuration(
                SourceProvider(source.provider), data.configuration, settings
            )
        except ValueError as exc:
            raise ValidationError(str(exc)[:300]) from exc
    if data.status is not None and data.status != source.status:
        source.status = data.status
        status_changed = True
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("a source with this name already exists") from exc
    await session.refresh(source)
    return (
        source_response(source, await _service_name(session, workspace_id, source.service_id)),
        status_changed,
    )


async def rotate_secret(
    session: AsyncSession, settings: Settings, workspace_id: UUID, source_id: UUID
) -> SecretRotated:
    source = await require_source(session, workspace_id, source_id)
    if source.provider != SourceProvider.GENERIC_WEBHOOK.value:
        raise ValidationError("only generic webhook sources have a signing secret")
    secret = generate_webhook_secret()
    try:
        source.secret_reference = encrypt_secret(settings, secret)
    except SecretsConfigurationError as exc:
        raise SecretsNotConfiguredError("OBS_SECRET_ENCRYPTION_KEY is not configured") from exc
    await session.commit()  # the previous secret stops verifying immediately
    return SecretRotated(source_id=source.id, webhook_secret=secret, rotated_at=utcnow())


async def test_source(
    session: AsyncSession, settings: Settings, bus: EventBus, workspace_id: UUID, source_id: UUID
) -> SourceTestResult:
    source = await require_source(session, workspace_id, source_id)
    began = time.perf_counter()
    status, message = "ok", "Connection verified"
    try:
        if source.provider == SourceProvider.GENERIC_WEBHOOK.value:
            if not await asyncio.to_thread(bus.health):
                status, message = "unavailable", "The event broker is not reachable"
        else:
            provider = provider_for_source(settings, source)
            await asyncio.to_thread(provider.test_connection)
    except ProviderThrottledError:
        status, message = "throttled", "The provider is throttling requests; retry later"
    except ProviderAuthError:
        status, message = "auth_failed", "The provider rejected the credentials or role"
    except (ProviderConfigurationError, ProviderRequestError) as exc:
        status, message = "misconfigured", str(exc)[:200]
    except ObsProviderError:
        status, message = "unavailable", "The provider is temporarily unavailable"
    now = utcnow()
    if status == "ok":
        source.last_success_at = now
    else:
        source.last_failure_at = now
    await session.commit()
    return SourceTestResult(
        ok=status == "ok",
        status=status,
        message=message,
        latency_ms=int((time.perf_counter() - began) * 1000),
    )


# -- signals ----------------------------------------------------------------------------------
def encode_cursor(timestamp: datetime, identifier: UUID) -> str:
    return base64.urlsafe_b64encode(f"{timestamp.isoformat()}|{identifier}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, identifier = raw.split("|", 1)
        return datetime.fromisoformat(stamp), UUID(identifier)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("invalid cursor") from exc


async def list_signals(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    source_id: UUID | None = None,
    service_id: UUID | None = None,
    status: str | None = None,
    severity: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> SignalPage:
    statement = (
        select(SignalReceipt, ObservabilitySource.name, MonitoredService.name)
        .outerjoin(
            ObservabilitySource,
            (ObservabilitySource.id == SignalReceipt.source_id)
            & (ObservabilitySource.workspace_id == workspace_id),
        )
        .outerjoin(
            MonitoredService,
            (MonitoredService.id == SignalReceipt.service_id)
            & (MonitoredService.workspace_id == workspace_id),
        )
        .where(SignalReceipt.workspace_id == workspace_id)
    )
    if source_id:
        statement = statement.where(SignalReceipt.source_id == source_id)
    if service_id:
        statement = statement.where(SignalReceipt.service_id == service_id)
    if status:
        statement = statement.where(SignalReceipt.status == status)
    if severity:
        statement = statement.where(SignalReceipt.severity == severity)
    if start:
        statement = statement.where(SignalReceipt.received_at >= start)
    if end:
        statement = statement.where(SignalReceipt.received_at <= end)
    if cursor:
        stamp, identifier = decode_cursor(cursor)
        statement = statement.where(
            tuple_(SignalReceipt.received_at, SignalReceipt.id) < tuple_(stamp, identifier)
        )
    rows = (
        await session.execute(
            statement.order_by(SignalReceipt.received_at.desc(), SignalReceipt.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    page = rows[:limit]
    next_cursor = (
        encode_cursor(page[-1][0].received_at, page[-1][0].id)
        if len(rows) > limit and page
        else None
    )
    return SignalPage(
        items=[signal_summary(receipt, source, service) for receipt, source, service in page],
        next_cursor=next_cursor,
    )


async def get_signal(session: AsyncSession, workspace_id: UUID, signal_id: UUID) -> SignalDetail:
    row = (
        await session.execute(
            select(SignalReceipt, ObservabilitySource.name, MonitoredService.name)
            .outerjoin(
                ObservabilitySource,
                (ObservabilitySource.id == SignalReceipt.source_id)
                & (ObservabilitySource.workspace_id == workspace_id),
            )
            .outerjoin(
                MonitoredService,
                (MonitoredService.id == SignalReceipt.service_id)
                & (MonitoredService.workspace_id == workspace_id),
            )
            .where(SignalReceipt.workspace_id == workspace_id, SignalReceipt.signal_id == signal_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("signal not found")
    receipt, source_name, service_name = row
    summary = signal_summary(receipt, source_name, service_name)
    return SignalDetail(
        **summary.model_dump(),
        provider_event_id=receipt.provider_event_id,
        payload_hash=receipt.payload_hash,
        schema_version=receipt.schema_version,
        failure_message=receipt.failure_message,
        normalized=receipt.normalized,
    )


# -- pipeline ---------------------------------------------------------------------------------
async def dead_letters_recent(session: AsyncSession, workspace_id: UUID, seconds: int = 300) -> int:
    return (
        await session.scalar(
            select(func.count(DeadLetterSignal.id)).where(
                DeadLetterSignal.workspace_id == workspace_id,
                DeadLetterSignal.created_at >= utcnow() - timedelta(seconds=seconds),
            )
        )
        or 0
    )


async def pipeline_status(
    session: AsyncSession,
    settings: Settings,
    bus: EventBus,
    reasons_provider: Any,
    workspace_id: UUID,
    window_minutes: int,
) -> PipelineStatus:
    now = utcnow()
    since = now - timedelta(minutes=window_minutes)
    base = (SignalReceipt.workspace_id == workspace_id, SignalReceipt.received_at >= since)
    counts = dict(
        (
            await session.execute(
                select(SignalReceipt.status, func.count())
                .where(*base)
                .group_by(SignalReceipt.status)
            )
        ).all()
    )
    created = await session.scalar(
        select(func.count()).where(*base, SignalReceipt.ingest_result == "created")
    )
    correlated = await session.scalar(
        select(func.count()).where(*base, SignalReceipt.ingest_result == "correlated")
    )
    last_minute = await session.scalar(
        select(func.count()).where(
            SignalReceipt.workspace_id == workspace_id,
            SignalReceipt.received_at >= now - timedelta(seconds=60),
            SignalReceipt.status != ReceiptStatus.FAILED.value,
        )
    )
    latency = (
        await session.execute(
            text(
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ms),"
                " percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) FROM ("
                " SELECT extract(epoch FROM (processed_at - received_at)) * 1000 AS ms"
                " FROM signal_receipts WHERE workspace_id = :ws AND status = 'processed'"
                " AND received_at >= :since) t"
            ),
            {"ws": workspace_id, "since": since},
        )
    ).one()
    open_letters = await session.scalar(
        select(func.count(DeadLetterSignal.id)).where(
            DeadLetterSignal.workspace_id == workspace_id, DeadLetterSignal.resolved_at.is_(None)
        )
    )
    topics = Topics.from_settings(settings)
    healthy = await asyncio.to_thread(bus.health)
    lag = await asyncio.to_thread(consumer_lag, bus, topics) if healthy else None
    reasons = await asyncio.to_thread(reasons_provider, workspace_id)
    if not healthy:
        reasons.append("broker_unavailable")
    if lag is not None and lag > settings.OBS_LAG_THRESHOLD:
        reasons.append("consumer_lag")
    sources = (
        await session.scalars(
            select(ObservabilitySource)
            .where(ObservabilitySource.workspace_id == workspace_id)
            .order_by(ObservabilitySource.name)
        )
    ).all()
    accepted = sum(v for k, v in counts.items() if k != ReceiptStatus.FAILED.value)
    return PipelineStatus(
        window_minutes=window_minutes,
        generated_at=now,
        accepted=accepted,
        processed=counts.get(ReceiptStatus.PROCESSED.value, 0),
        duplicates=counts.get(ReceiptStatus.DUPLICATE.value, 0),
        failures=counts.get(ReceiptStatus.FAILED.value, 0)
        + counts.get(ReceiptStatus.DEAD_LETTERED.value, 0),
        dead_letters_open=open_letters or 0,
        events_per_second=round((last_minute or 0) / 60, 3),
        latency_p50_ms=round(latency[0], 2) if latency[0] is not None else None,
        latency_p95_ms=round(latency[1], 2) if latency[1] is not None else None,
        incidents_created=created or 0,
        events_correlated=correlated or 0,
        consumer_lag=lag,
        lag_available=lag is not None,
        broker_healthy=healthy,
        broker_backend=bus.backend,
        backpressure=BackpressureStatus(active=bool(reasons), reasons=sorted(set(reasons))),
        sources=[
            SourceHealth(
                id=s.id,
                name=s.name,
                provider=s.provider,
                status=s.status,
                last_event_at=s.last_event_at,
                last_success_at=s.last_success_at,
                last_failure_at=s.last_failure_at,
            )
            for s in sources
        ],
    )


# -- dead letters -----------------------------------------------------------------------------
def dead_letter_response(letter: DeadLetterSignal, source_name: str | None) -> DeadLetterResponse:
    return DeadLetterResponse(
        id=letter.id,
        workspace_id=letter.workspace_id,
        source_id=letter.source_id,
        source_name=source_name,
        signal_id=letter.signal_id,
        original_topic=letter.original_topic,
        partition=letter.partition,
        offset=letter.offset,
        failure_code=letter.failure_code,
        failure_class="retryable_exhausted"
        if letter.failure_code in RETRYABLE_CODES
        else "non_retryable",
        failure_message=letter.failure_message,
        attempt_count=letter.attempt_count,
        first_failed_at=letter.first_failed_at,
        last_failed_at=letter.last_failed_at,
        replayed_at=letter.replayed_at,
        resolved_at=letter.resolved_at,
        replay_count=letter.replay_count,
        replayable=letter.sanitized_payload is not None,
        payload_preview=letter.sanitized_payload,
    )


async def list_dead_letters(
    session: AsyncSession, workspace_id: UUID, cursor: str | None, limit: int
) -> DeadLetterPage:
    statement = (
        select(DeadLetterSignal, ObservabilitySource.name)
        .outerjoin(
            ObservabilitySource,
            (ObservabilitySource.id == DeadLetterSignal.source_id)
            & (ObservabilitySource.workspace_id == workspace_id),
        )
        .where(DeadLetterSignal.workspace_id == workspace_id)
    )
    if cursor:
        stamp, identifier = decode_cursor(cursor)
        statement = statement.where(
            tuple_(DeadLetterSignal.created_at, DeadLetterSignal.id) < tuple_(stamp, identifier)
        )
    rows = (
        await session.execute(
            statement.order_by(
                DeadLetterSignal.created_at.desc(), DeadLetterSignal.id.desc()
            ).limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    return DeadLetterPage(
        items=[dead_letter_response(letter, name) for letter, name in page],
        next_cursor=(
            encode_cursor(page[-1][0].created_at, page[-1][0].id)
            if len(rows) > limit and page
            else None
        ),
    )


async def replay_dead_letter(
    session: AsyncSession,
    settings: Settings,
    bus: EventBus,
    workspace_id: UUID,
    letter_id: UUID,
) -> ReplayResponse:
    """Republish the sanitized original once; further requests are audited no-ops."""
    letter = await session.scalar(
        select(DeadLetterSignal)
        .where(DeadLetterSignal.id == letter_id, DeadLetterSignal.workspace_id == workspace_id)
        .with_for_update()
    )
    if letter is None:
        raise NotFoundError("dead letter not found")
    if letter.sanitized_payload is None:
        raise ConflictError("this dead letter has no stored payload and cannot be replayed")
    if letter.replayed_at is not None:
        session.add(
            DeadLetterReplay(
                workspace_id=workspace_id, dead_letter_id=letter.id, outcome="already_replayed"
            )
        )
        await session.commit()
        return ReplayResponse(
            status="already_replayed", dead_letter_id=letter.id,
            replayed_at=letter.replayed_at, replay_count=letter.replay_count,
        )  # fmt: skip
    payload = letter.sanitized_payload
    key = f"{payload.get('workspace_id')}:{payload.get('service_id')}"
    await asyncio.to_thread(
        bus.publish,
        letter.original_topic,
        key,
        json.dumps(payload, default=str).encode(),
        {"schema_version": CURRENT_SCHEMA_VERSION, "replay_of": str(letter.id)},
    )
    now = utcnow()
    letter.replayed_at = now
    letter.replay_count += 1
    if letter.signal_id is not None:
        # Allow the consumers to reprocess a signal that had been dead-lettered.
        await session.execute(
            text(
                "UPDATE signal_receipts SET status = 'published', failure_code = NULL,"
                " failure_message = NULL WHERE workspace_id = :ws AND signal_id = :sid"
                " AND status = 'dead_lettered'"
            ),
            {"ws": workspace_id, "sid": letter.signal_id},
        )
    session.add(
        DeadLetterReplay(
            workspace_id=workspace_id, dead_letter_id=letter.id, outcome="replayed",
            target_topic=letter.original_topic,
        )
    )  # fmt: skip
    await session.commit()
    await asyncio.to_thread(
        publish_observability_event, settings, workspace_id, "observability.dead_letter_replayed",
        {"dead_letter_id": letter.id, "source_id": letter.source_id},
    )  # fmt: skip
    return ReplayResponse(
        status="replayed",
        dead_letter_id=letter.id,
        replayed_at=now,
        replay_count=letter.replay_count,
    )


# -- demo -------------------------------------------------------------------------------------
def demo_payload(index: int, event_id: str, kind: str) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    if kind == "invalid":
        return {"type": "not-a-real-signal-type", "event_id": event_id}
    if kind == "log":
        return {
            "type": "log", "level": "error", "event_id": event_id, "timestamp": now,
            "message": f"database connection pool exhausted request {index}",
        }  # fmt: skip
    if kind == "metric":
        return {
            "type": "metric", "event_id": event_id, "metric_name": "ErrorRate", "value": 12.5,
            "threshold": 5, "severity": "high", "occurred_at": now,
        }  # fmt: skip
    return {
        "type": "alert", "event_id": event_id, "title": "Checkout error rate high",
        "message": "5xx rate above threshold", "severity": "high", "occurred_at": now,
        "error_signature": "checkout-5xx",
    }  # fmt: skip


async def generate_demo(
    session: AsyncSession,
    settings: Settings,
    bus: EventBus,
    workspace_id: UUID,
    request: DemoGenerateRequest,
) -> DemoGenerateResponse:
    if settings.APP_ENV is AppEnvironment.PRODUCTION:
        raise DemoDisabledError("demo generation is disabled in production")
    source: ObservabilitySource | None
    if request.source_id is not None:
        source = await require_source(session, workspace_id, request.source_id)
    else:
        source = await session.scalar(
            select(ObservabilitySource).where(
                ObservabilitySource.workspace_id == workspace_id,
                ObservabilitySource.provider == SourceProvider.DEMO_FIXTURE.value,
                ObservabilitySource.status != SourceStatus.DISABLED.value,
            )
        )
        if source is None:
            raise ValidationError("create a demo_fixture source first (or pass source_id)")
    if source.status == SourceStatus.DISABLED.value:
        raise ValidationError("the source is disabled")
    demo_source_id = source.id
    accepted = duplicates = rejected = 0
    kinds = ["alert", "log", "metric"]
    batch = uuid.uuid4().hex[:8]
    previous: list[tuple[str, dict[str, Any]]] = []
    for index in range(request.count):
        roll = (index * 37) % 100
        if previous and roll < request.duplicate_percent:
            event_id, payload = previous[(index * 7) % len(previous)]
        else:
            kind = "invalid" if roll >= 100 - request.invalid_percent else kinds[index % 3]
            event_id = f"demo-{batch}-{index}"
            payload = demo_payload(index, event_id, kind)
            previous.append((event_id, payload))
        body = json.dumps(payload, sort_keys=True).encode()
        try:
            outcome = await accept_signal(
                settings, bus, session, source, payload, body, provider_event_id=event_id
            )
        except IngressRejected:
            rejected += 1
            continue
        if outcome.status == "duplicate":
            duplicates += 1
        else:
            accepted += 1
    return DemoGenerateResponse(
        source_id=demo_source_id, accepted=accepted, duplicates=duplicates, rejected=rejected
    )
