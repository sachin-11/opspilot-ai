"""Durable signal acceptance: the only way signals enter the pipeline (webhooks and demo).

A signal is "accepted" only after (1) its receipt row is committed and (2) the raw topic record is
acknowledged by the broker. If publication fails the caller must answer 503, never 202.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.config import Settings
from opspilot_api.investigations.safety import redact_value
from opspilot_api.observability.bus import BusError, EventBus, Topics
from opspilot_api.observability.models import ObservabilitySource, ReceiptStatus, SignalReceipt
from opspilot_api.observability.signal_schema import (
    CURRENT_SCHEMA_VERSION,
    MAX_RAW_PAYLOAD_BYTES,
    RawSignalEnvelope,
    SignalProvider,
    partition_key,
)
from opspilot_api.observability.telemetry import ACCEPTED, DUPLICATES, REJECTED, span

logger = logging.getLogger(__name__)


class IngressRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class AcceptOutcome:
    status: str  # accepted | duplicate
    signal_id: UUID
    workspace_id: UUID
    source_id: UUID


def ingress_key_for(
    source_id: UUID,
    provider_event_id: str | None,
    idempotency_key: str | None,
    payload_hash: str,
    window_seconds: int,
    now: float,
) -> str:
    if provider_event_id:
        return "evt:" + hashlib.sha256(f"{source_id}:{provider_event_id}".encode()).hexdigest()[:40]
    if idempotency_key:
        return "idem:" + hashlib.sha256(f"{source_id}:{idempotency_key}".encode()).hexdigest()[:40]
    # Identical bytes inside one window are one delivery; the window bounds how long they dedupe.
    return f"hash:{payload_hash}:{int(now // window_seconds)}"


def event_id_from(payload: dict[str, Any], header: str | None) -> str | None:
    value = header or payload.get("event_id")
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    return text[:200] or None


async def _find_existing(
    session: AsyncSession,
    workspace_id: UUID,
    source_id: UUID,
    ingress_key: str,
    provider_event_id: str | None,
) -> SignalReceipt | None:
    clauses = [SignalReceipt.ingress_key == ingress_key]
    if provider_event_id:
        clauses.append(SignalReceipt.provider_event_id == provider_event_id)
    return await session.scalar(
        select(SignalReceipt).where(
            SignalReceipt.workspace_id == workspace_id,
            SignalReceipt.source_id == source_id,
            or_(*clauses),
        )
    )


async def accept_signal(
    settings: Settings,
    bus: EventBus,
    session: AsyncSession,
    source: ObservabilitySource,
    payload: dict[str, Any],
    body: bytes,
    provider_event_id: str | None = None,
    idempotency_key: str | None = None,
) -> AcceptOutcome:
    # Plain copies: commit/rollback expire ORM instances, and async sessions cannot lazy-load.
    await session.refresh(source)
    workspace_id, source_id, service_id = source.workspace_id, source.id, source.service_id
    provider, environment, region = source.provider, source.environment, source.region
    payload_hash = hashlib.sha256(body).hexdigest()
    now = datetime.now(UTC)
    key = ingress_key_for(
        source_id, provider_event_id, idempotency_key, payload_hash,
        settings.OBS_DEDUP_WINDOW_SECONDS, time.time(),
    )  # fmt: skip
    safe_payload = redact_value(payload)
    signal_id = uuid4()
    receipt = SignalReceipt(
        id=uuid4(),
        workspace_id=workspace_id,
        service_id=service_id,
        source_id=source_id,
        signal_id=signal_id,
        provider=provider,
        provider_event_id=provider_event_id,
        ingress_key=key,
        deduplication_key=key,
        payload_hash=payload_hash,
        schema_version=CURRENT_SCHEMA_VERSION,
        status=ReceiptStatus.RECEIVED.value,
        attempt_count=0,
        received_at=now,
    )
    session.add(receipt)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _find_existing(session, workspace_id, source_id, key, provider_event_id)
        if existing is None:
            raise IngressRejected(
                503, "storage_unavailable", "Signal could not be recorded", 5
            ) from None
        if not (
            existing.status == ReceiptStatus.FAILED.value
            and existing.failure_code == "publish_failed"
        ):
            DUPLICATES.labels(stage="ingress").inc()
            return AcceptOutcome("duplicate", existing.signal_id, workspace_id, source_id)
        # Our own earlier publish failed: this retry completes the same signal exactly once.
        receipt = existing
        signal_id = existing.signal_id
        receipt.status = ReceiptStatus.RECEIVED.value
        receipt.failure_code = None
        receipt.failure_message = None
        await session.commit()

    try:
        envelope = RawSignalEnvelope(
            schema_version=CURRENT_SCHEMA_VERSION,
            signal_id=signal_id,
            workspace_id=workspace_id,
            service_id=service_id,
            source_id=source_id,
            provider=SignalProvider(provider),
            ingress_key=key,
            provider_event_id=provider_event_id,
            payload=safe_payload,
            payload_hash=payload_hash,
            environment=environment,
            region=region,
            received_at=now,
        )
    except ValueError as exc:
        receipt.status = ReceiptStatus.FAILED.value
        receipt.failure_code = "payload_too_large"
        receipt.failure_message = "payload exceeds the raw envelope bound"
        receipt.failed_at = now
        await session.commit()
        REJECTED.labels(reason="envelope_invalid").inc()
        raise IngressRejected(
            413,
            "payload_too_large",
            f"Payload exceeds {MAX_RAW_PAYLOAD_BYTES} bytes after redaction",
        ) from exc

    topics = Topics.from_settings(settings)
    try:
        with span("kafka.publish", signal__id=signal_id, workspace__id=workspace_id,
                  source__id=source_id, topic=topics.raw):  # fmt: skip
            await asyncio.to_thread(
                bus.publish,
                topics.raw,
                partition_key(workspace_id, service_id),
                envelope.model_dump_json().encode(),
                {
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "signal_id": str(signal_id),
                    "workspace_id": str(workspace_id),
                    "source_id": str(source_id),
                    "content_type": "application/json",
                },
            )
    except BusError as exc:
        logger.warning("raw publish failed", extra={"error_class": type(exc).__name__})
        receipt.status = ReceiptStatus.FAILED.value
        receipt.failure_code = "publish_failed"
        receipt.failure_message = "broker did not acknowledge the signal"
        receipt.failed_at = datetime.now(UTC)
        source.last_failure_at = receipt.failed_at
        await session.commit()
        REJECTED.labels(reason="publish_failed").inc()
        raise IngressRejected(
            503, "broker_unavailable", "Signal could not be durably accepted", 5
        ) from exc

    # Guarded: a fast consumer may already have advanced the receipt past "received".
    await session.execute(
        update(SignalReceipt)
        .where(
            SignalReceipt.id == receipt.id,
            SignalReceipt.workspace_id == workspace_id,
            SignalReceipt.status == ReceiptStatus.RECEIVED.value,
        )
        .values(status=ReceiptStatus.PUBLISHED.value)
    )
    source.last_event_at = datetime.now(UTC)
    await session.commit()
    ACCEPTED.labels(provider=provider).inc()
    return AcceptOutcome("accepted", signal_id, workspace_id, source_id)
