"""Consumer-side pipeline: normalization, correlation hand-off, retry, dead-letter, and replay.

Delivery is **at-least-once**: offsets are committed only after the effect is durable, so a crash
between effect and commit redelivers the message. Every handler is therefore idempotent:

* raw handler: publish normalized -> mark receipt ``normalized`` (guarded) -> commit offset.
  A crash after publish republishes an identical normalized record; the normalized handler's
  receipt state machine absorbs it.
* normalized handler: Module 1 ``ingest_event`` is idempotent per idempotency key (it commits the
  incident, event, and timeline atomically); the receipt moves to ``processed`` in a second
  transaction. A crash between the two replays into the same Module 1 result.
Exactly-once across Kafka, PostgreSQL, and Redis is not claimed: no transaction spans them.
"""

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot_api.config import Settings
from opspilot_api.incidents.schemas import EventIngestRequest
from opspilot_api.incidents.service import DomainError, ingest_event
from opspilot_api.investigations.safety import redact_value, sanitize_error
from opspilot_api.observability.bus import BusMessage, EventBus, Topics
from opspilot_api.observability.events import publish_observability_event
from opspilot_api.observability.models import (
    DeadLetterSignal,
    ObservabilitySource,
    ReceiptStatus,
    SignalReceipt,
    SourceStatus,
)
from opspilot_api.observability.normalize import NormalizationError, normalize
from opspilot_api.observability.signal_schema import (
    CURRENT_SCHEMA_VERSION,
    InvalidSignalError,
    NormalizedSignal,
    RawSignalEnvelope,
    UnsupportedSchemaError,
    parse_normalized,
    parse_raw,
    partition_key,
)
from opspilot_api.observability.telemetry import (
    DEAD_LETTERS,
    DUPLICATES,
    END_TO_END_LATENCY,
    EVENTS_CORRELATED,
    INCIDENTS_CREATED,
    NORMALIZATION_FAILURES,
    PROCESSED,
    PROCESSING_FAILURES,
    span,
)

logger = logging.getLogger(__name__)
MAX_DEAD_LETTER_PAYLOAD_BYTES = 64 * 1024
RETRYABLE_CODES = {"retries_exhausted"}
_PAST_NORMALIZATION = {
    ReceiptStatus.NORMALIZED.value,
    ReceiptStatus.PROCESSED.value,
    ReceiptStatus.DUPLICATE.value,
}


class TerminalProcessingError(Exception):
    """A non-retryable failure: retrying cannot succeed, so the message is dead-lettered."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PipelineContext:
    settings: Settings
    bus: EventBus
    session_factory: async_sessionmaker[AsyncSession]

    @property
    def topics(self) -> Topics:
        return Topics.from_settings(self.settings)


async def _publish_event(
    ctx: PipelineContext, workspace_id: UUID, event: str, data: dict[str, Any]
) -> None:
    with span("websocket.publish", workspace__id=workspace_id, result=event):
        await asyncio.to_thread(
            publish_observability_event, ctx.settings, workspace_id, event, data
        )


async def _load_source(
    session: AsyncSession, workspace_id: UUID, source_id: UUID, service_id: UUID
) -> ObservabilitySource:
    source = await session.scalar(
        select(ObservabilitySource).where(
            ObservabilitySource.id == source_id,
            ObservabilitySource.workspace_id == workspace_id,
        )
    )
    if source is None or source.service_id != service_id:
        raise TerminalProcessingError(
            "source does not belong to the workspace/service", "invalid_tenant_relationship"
        )
    return source


async def _receipt_for_update(
    session: AsyncSession, workspace_id: UUID, signal_id: UUID
) -> SignalReceipt | None:
    return await session.scalar(
        select(SignalReceipt)
        .where(SignalReceipt.workspace_id == workspace_id, SignalReceipt.signal_id == signal_id)
        .with_for_update()
    )


def _create_receipt(
    raw: RawSignalEnvelope | NormalizedSignal, ingress_key: str, key: str
) -> SignalReceipt:
    return SignalReceipt(
        workspace_id=raw.workspace_id,
        service_id=raw.service_id,
        source_id=raw.source_id,
        signal_id=raw.signal_id,
        provider=raw.provider.value,
        provider_event_id=raw.provider_event_id,
        ingress_key=ingress_key,
        deduplication_key=key,
        payload_hash=raw.payload_hash,
        schema_version=raw.schema_version,
        status=ReceiptStatus.PUBLISHED.value,
        attempt_count=0,
        received_at=raw.received_at,
    )


def _is_replay(message: BusMessage) -> bool:
    return "replay_of" in message.headers


async def handle_raw(ctx: PipelineContext, message: BusMessage) -> None:
    """Validate, normalize, publish the normalized record; never touches incidents."""
    try:
        envelope = parse_raw(message.value)
    except (UnsupportedSchemaError, InvalidSignalError, ValueError) as exc:
        code = getattr(exc, "code", "invalid_signal")
        raise TerminalProcessingError(str(exc)[:200], code) from exc
    async with ctx.session_factory() as session:
        await _load_source(session, envelope.workspace_id, envelope.source_id, envelope.service_id)
        receipt = await _receipt_for_update(session, envelope.workspace_id, envelope.signal_id)
        if receipt is None:
            receipt = _create_receipt(envelope, envelope.ingress_key, envelope.ingress_key)
            session.add(receipt)
        elif receipt.status in _PAST_NORMALIZATION or (
            receipt.status == ReceiptStatus.DEAD_LETTERED.value and not _is_replay(message)
        ):
            return
        elif receipt.status == ReceiptStatus.DEAD_LETTERED.value:
            receipt.status = ReceiptStatus.PUBLISHED.value
            receipt.failure_code = None
            receipt.failure_message = None
        receipt.attempt_count += 1
        await session.commit()
        with span("normalize", signal__id=envelope.signal_id, workspace__id=envelope.workspace_id):
            try:
                signal = normalize(envelope)
            except NormalizationError as exc:
                NORMALIZATION_FAILURES.labels(code=exc.code).inc()
                raise TerminalProcessingError(str(exc)[:300], exc.code) from exc
        headers = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "signal_id": str(signal.signal_id),
            "workspace_id": str(signal.workspace_id),
            "source_id": str(signal.source_id),
        }
        if "replay_of" in message.headers:
            headers["replay_of"] = message.headers["replay_of"]
        with span("kafka.publish", signal__id=signal.signal_id, topic=ctx.topics.normalized):
            await asyncio.to_thread(
                ctx.bus.publish,
                ctx.topics.normalized,
                partition_key(signal.workspace_id, signal.service_id),
                signal.model_dump_json().encode(),
                headers,
            )
        await session.execute(
            update(SignalReceipt)
            .where(
                SignalReceipt.id == receipt.id,
                SignalReceipt.workspace_id == signal.workspace_id,
                SignalReceipt.status.in_(
                    [ReceiptStatus.RECEIVED.value, ReceiptStatus.PUBLISHED.value]
                ),
            )
            .values(
                status=ReceiptStatus.NORMALIZED.value,
                normalized_at=datetime.now(UTC),
                deduplication_key=signal.deduplication_key,
                signal_type=signal.signal_type.value,
                severity=signal.severity.value,
                title=signal.title,
                occurred_at=signal.occurred_at,
            )
        )
        await session.commit()


def _ingest_request(signal: NormalizedSignal, source: ObservabilitySource) -> EventIngestRequest:
    attributes: dict[str, Any] = {
        "signal_id": str(signal.signal_id),
        "source_id": str(signal.source_id),
        "provider": signal.provider.value,
        "environment": signal.environment.value,
        "region": signal.region,
        "resource_type": signal.resource_type,
        "resource_id": signal.resource_id,
        "metric_name": signal.metric_name,
        "metric_value": signal.metric_value,
        "threshold": signal.threshold,
        "trace_id": signal.trace_id,
    }
    return EventIngestRequest(
        service_id=signal.service_id,
        external_id=signal.provider_event_id,
        source=f"{signal.provider.value}:{source.name}"[:100],
        event_type=signal.signal_type.value,
        severity=signal.severity,
        summary=signal.title[:500],
        error_signature=signal.error_signature,
        attributes={k: v for k, v in attributes.items() if v is not None},
        occurred_at=signal.occurred_at,
    )


async def _resolve_replay(session: AsyncSession, workspace_id: UUID, message: BusMessage) -> None:
    replay_of = message.headers.get("replay_of")
    if not replay_of:
        return
    try:
        letter_id = UUID(replay_of)
    except ValueError:
        return
    await session.execute(
        update(DeadLetterSignal)
        .where(
            DeadLetterSignal.id == letter_id,
            DeadLetterSignal.workspace_id == workspace_id,
            DeadLetterSignal.resolved_at.is_(None),
        )
        .values(resolved_at=datetime.now(UTC))
    )
    await session.commit()


async def handle_normalized(ctx: PipelineContext, message: BusMessage) -> None:
    """Persist state, hand the signal to Module 1 correlation, publish WebSocket updates."""
    try:
        signal = parse_normalized(message.value)
    except (UnsupportedSchemaError, InvalidSignalError, ValueError) as exc:
        code = getattr(exc, "code", "invalid_signal")
        raise TerminalProcessingError(str(exc)[:200], code) from exc
    settings = ctx.settings
    async with ctx.session_factory() as session:
        source = await _load_source(
            session, signal.workspace_id, signal.source_id, signal.service_id
        )
        receipt = await _receipt_for_update(session, signal.workspace_id, signal.signal_id)
        if receipt is None:
            receipt = _create_receipt(signal, signal.deduplication_key, signal.deduplication_key)
            session.add(receipt)
            await session.flush()
        if receipt.status in {ReceiptStatus.PROCESSED.value, ReceiptStatus.DUPLICATE.value}:
            await _resolve_replay(session, signal.workspace_id, message)
            return  # redelivery of a message whose effect is already durable
        if receipt.status == ReceiptStatus.DEAD_LETTERED.value and not _is_replay(message):
            return
        receipt.attempt_count += 1
        receipt.status = ReceiptStatus.NORMALIZED.value
        receipt.failure_code = None
        receipt.failure_message = None
        receipt.deduplication_key = signal.deduplication_key
        receipt.signal_type = signal.signal_type.value
        receipt.severity = signal.severity.value
        receipt.title = signal.title
        receipt.occurred_at = signal.occurred_at
        receipt.normalized = signal.model_dump(mode="json")
        receipt.normalized_at = receipt.normalized_at or datetime.now(UTC)

        duplicate_query = select(SignalReceipt).where(
            SignalReceipt.workspace_id == signal.workspace_id,
            SignalReceipt.deduplication_key == signal.deduplication_key,
            SignalReceipt.signal_id != signal.signal_id,
            SignalReceipt.status == ReceiptStatus.PROCESSED.value,
        )
        if signal.deduplication_key.startswith("fp:"):
            window = timedelta(seconds=settings.OBS_DEDUP_WINDOW_SECONDS)
            duplicate_query = duplicate_query.where(
                SignalReceipt.processed_at > datetime.now(UTC) - window
            )
        original = await session.scalar(
            duplicate_query.order_by(SignalReceipt.processed_at).limit(1)
        )
        if original is not None:
            receipt.status = ReceiptStatus.DUPLICATE.value
            receipt.duplicate_of = original.signal_id
            receipt.incident_id = original.incident_id
            receipt.processed_at = datetime.now(UTC)
            await session.commit()
            DUPLICATES.labels(stage="normalized").inc()
            await _resolve_replay(session, signal.workspace_id, message)
            await _publish_event(
                ctx, signal.workspace_id, "observability.signal_duplicate",
                {"signal_id": signal.signal_id, "source_id": signal.source_id,
                 "incident_id": original.incident_id, "status": "duplicate"},
            )  # fmt: skip
            return
        await session.commit()  # persists the attempt count before the external effect

        idempotency_key = (
            f"sig:{signal.deduplication_key}"
            if signal.deduplication_key.startswith(("evt:", "idem:"))
            else f"sig:{signal.signal_id}"
        )
        try:
            with span("incident.correlation", signal__id=signal.signal_id,
                      workspace__id=signal.workspace_id):  # fmt: skip
                response = await ingest_event(
                    session, signal.workspace_id, idempotency_key, _ingest_request(signal, source)
                )
        except DomainError as exc:
            await session.rollback()
            raise TerminalProcessingError(str(exc)[:200], "invalid_tenant_relationship") from exc
        except PydanticValidationError as exc:
            await session.rollback()
            raise TerminalProcessingError(
                "signal rejected by event ingestion", "invalid_event"
            ) from exc

        receipt = await _receipt_for_update(session, signal.workspace_id, signal.signal_id)
        if receipt is None:  # pragma: no cover - defensive; the row was just written
            raise TerminalProcessingError("receipt vanished", "receipt_missing")
        now = datetime.now(UTC)
        receipt.status = ReceiptStatus.PROCESSED.value
        receipt.incident_id = response.incident_id
        receipt.ingest_result = response.result
        receipt.processed_at = now
        source_row = await session.get(ObservabilitySource, source.id)
        changed_status = False
        if source_row is not None:
            source_row.last_success_at = now
            source_row.last_event_at = now
            if source_row.status == SourceStatus.FAILING.value:
                source_row.status = SourceStatus.ACTIVE.value
                changed_status = True
        await session.commit()
        PROCESSED.inc()
        END_TO_END_LATENCY.observe(max(0.0, (now - signal.received_at).total_seconds()))
        (INCIDENTS_CREATED if response.result == "created" else EVENTS_CORRELATED).inc()
        await _resolve_replay(session, signal.workspace_id, message)
        await _publish_event(
            ctx, signal.workspace_id, "observability.signal_processed",
            {"signal_id": signal.signal_id, "source_id": signal.source_id,
             "incident_id": response.incident_id, "status": "processed"},
        )  # fmt: skip
        if changed_status:
            await _publish_event(
                ctx, signal.workspace_id, "observability.source_status_changed",
                {"source_id": signal.source_id, "status": "active"},
            )  # fmt: skip


async def dead_letter(
    ctx: PipelineContext, message: BusMessage, code: str, error: str, attempts: int
) -> None:
    """Persist (sanitized, bounded) and publish to the dead-letter topic; never drops silently."""
    payload_hash = hashlib.sha256(message.value).hexdigest()
    parsed: Any = None
    try:
        parsed = json.loads(message.value)
    except (ValueError, UnicodeDecodeError):
        parsed = None
    data = parsed if isinstance(parsed, dict) else {}
    sanitized: dict[str, Any] | None = None
    if isinstance(parsed, dict):
        candidate = redact_value(parsed)
        if len(json.dumps(candidate, default=str).encode()) <= MAX_DEAD_LETTER_PAYLOAD_BYTES:
            sanitized = candidate
    workspace_id = source_id = signal_id = None
    schema_version = str(data.get("schema_version"))[:20] if data.get("schema_version") else None
    now = datetime.now(UTC)
    async with ctx.session_factory() as session:
        try:
            candidate_ws, candidate_src = (
                UUID(str(data["workspace_id"])),
                UUID(str(data["source_id"])),
            )
            owned = await session.scalar(
                select(ObservabilitySource.id).where(
                    ObservabilitySource.id == candidate_src,
                    ObservabilitySource.workspace_id == candidate_ws,
                )
            )
            if owned is not None:  # only a verified tenant relationship is trusted
                workspace_id, source_id = candidate_ws, candidate_src
                signal_id = UUID(str(data["signal_id"])) if data.get("signal_id") else None
        except (KeyError, ValueError):
            pass
        letter = DeadLetterSignal(
            workspace_id=workspace_id,
            source_id=source_id,
            signal_id=signal_id,
            original_topic=message.topic,
            partition=message.partition,
            offset=message.offset,
            payload_hash=payload_hash,
            schema_version=schema_version,
            failure_code=code[:80],
            failure_message=sanitize_error(Exception(error)),
            sanitized_payload=sanitized,
            attempt_count=attempts,
            first_failed_at=now,
            last_failed_at=now,
        )
        session.add(letter)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(DeadLetterSignal).where(
                    DeadLetterSignal.original_topic == message.topic,
                    DeadLetterSignal.partition == message.partition,
                    DeadLetterSignal.offset == message.offset,
                    DeadLetterSignal.payload_hash == payload_hash,
                )
            )
            if existing is None:
                raise
            existing.attempt_count = max(existing.attempt_count, attempts)
            existing.last_failed_at = now
            letter = existing
            await session.commit()
        envelope = {
            "dead_letter_id": str(letter.id),
            "original_topic": message.topic,
            "partition": message.partition,
            "offset": message.offset,
            "failure_code": code,
            "failure_message": letter.failure_message,
            "payload_hash": payload_hash,
            "workspace_id": str(workspace_id) if workspace_id else None,
            "source_id": str(source_id) if source_id else None,
            "attempts": attempts,
            "payload": sanitized,
        }
        with span("kafka.publish", topic=ctx.topics.dead_letter, failure__code=code):
            await asyncio.to_thread(
                ctx.bus.publish, ctx.topics.dead_letter, message.key,
                json.dumps(envelope, default=str).encode(),
                {"schema_version": CURRENT_SCHEMA_VERSION},
            )  # fmt: skip
        source_changed = False
        if signal_id is not None and workspace_id is not None:
            await session.execute(
                update(SignalReceipt)
                .where(
                    SignalReceipt.workspace_id == workspace_id,
                    SignalReceipt.signal_id == signal_id,
                    SignalReceipt.status.notin_(
                        [ReceiptStatus.PROCESSED.value, ReceiptStatus.DUPLICATE.value]
                    ),
                )
                .values(
                    status=ReceiptStatus.DEAD_LETTERED.value,
                    failed_at=now,
                    failure_code=code[:80],
                    failure_message=letter.failure_message,
                )
            )
        if source_id is not None and workspace_id is not None:
            source = await session.get(ObservabilitySource, source_id)
            if source is not None:
                source.last_failure_at = now
                if source.status == SourceStatus.ACTIVE.value:
                    source.status = SourceStatus.FAILING.value
                    source_changed = True
        await session.commit()
    DEAD_LETTERS.labels(code=code[:40]).inc()
    if workspace_id is not None:
        await _publish_event(
            ctx, workspace_id, "observability.dead_letter_created",
            {"dead_letter_id": letter.id, "source_id": source_id, "signal_id": signal_id,
             "failure_code": code},
        )  # fmt: skip
        if source_changed and source_id is not None:
            await _publish_event(
                ctx, workspace_id, "observability.source_status_changed",
                {"source_id": source_id, "status": "failing"},
            )  # fmt: skip


Handler = Callable[[PipelineContext, BusMessage], Awaitable[None]]


def backoff_seconds(settings: Settings, attempt: int) -> float:
    base = settings.OBS_RETRY_BASE_DELAY_SECONDS
    return float(min(30.0, base * (2 ** (attempt - 1))) + random.uniform(0, base))  # noqa: S311


async def process_message(
    ctx: PipelineContext, group: str, message: BusMessage, handler: Handler, stage: str
) -> str:
    """Run one message to a durable outcome, then commit. Returns ``ok`` or the failure code.

    Retryable failures back off exponentially with jitter up to ``OBS_RETRY_MAX_ATTEMPTS`` and
    ``OBS_RETRY_MAX_AGE_SECONDS``, then dead-letter (no infinite loop). Terminal failures
    dead-letter immediately. The offset is committed only after the outcome is durable; if even
    dead-lettering fails (e.g. database down) the exception propagates *uncommitted*, so the
    message is redelivered rather than lost.
    """
    settings = ctx.settings
    started = time.time()
    produced_at = (message.timestamp_ms or int(started * 1000)) / 1000
    attempts = 0
    outcome = "ok"
    while True:
        attempts += 1
        try:
            await handler(ctx, message)
            break
        except TerminalProcessingError as exc:
            PROCESSING_FAILURES.labels(stage=stage, retryable="false").inc()
            await dead_letter(ctx, message, exc.code, str(exc), attempts)
            outcome = exc.code
            break
        except Exception as exc:
            PROCESSING_FAILURES.labels(stage=stage, retryable="true").inc()
            logger.warning(
                "message processing failed",
                extra={"stage": stage, "attempt": attempts, "error_class": type(exc).__name__},
            )
            age = time.time() - min(produced_at, started)
            if (
                attempts >= settings.OBS_RETRY_MAX_ATTEMPTS
                or age > settings.OBS_RETRY_MAX_AGE_SECONDS
            ):
                await dead_letter(
                    ctx, message, "retries_exhausted",
                    f"{type(exc).__name__}: retries exhausted at stage {stage}", attempts,
                )  # fmt: skip
                outcome = "retries_exhausted"
                break
            await asyncio.sleep(backoff_seconds(settings, attempts))
    await asyncio.to_thread(ctx.bus.commit, group, message)
    return outcome
