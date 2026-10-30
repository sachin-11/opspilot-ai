"""Helpers for observability tests (memory bus + live PostgreSQL/Redis; no external services)."""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import MonitoredService, Workspace
from opspilot_api.infrastructure import create_db_engine, create_session_factory
from opspilot_api.observability.bus import EventBus, InMemoryEventBus
from opspilot_api.observability.consumer import drain
from opspilot_api.observability.models import ObservabilitySource, SourceProvider, SourceStatus
from opspilot_api.observability.pipeline import PipelineContext
from opspilot_api.observability.secrets_util import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    compute_signature,
    encrypt_secret,
    generate_public_id,
    generate_webhook_secret,
)

from .repo_helpers import live_settings, new_service, new_workspace

KEY = Fernet.generate_key().decode()


def obs_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "OBS_SECRET_ENCRYPTION_KEY": SecretStr(KEY),
        "OBS_RETRY_BASE_DELAY_SECONDS": 0.0,
        "EVENT_BUS_BACKEND": "memory",
        "KAFKA_TOPIC_PREFIX": f"obs-test-{uuid.uuid4().hex[:8]}",
        "OBS_WORKSPACE_RATE_PER_SECOND": 10_000,
        "OBS_SOURCE_RATE_PER_SECOND": 10_000,
        **overrides,
    }
    return live_settings(**values)


def make_workspace(session: Session) -> tuple[Workspace, MonitoredService]:
    workspace = new_workspace(session, "obs")
    service = new_service(session, workspace, "Checkout")
    session.commit()
    return workspace, service


def make_source(
    session: Session,
    settings: Settings,
    workspace: Workspace,
    service: MonitoredService,
    provider: SourceProvider = SourceProvider.GENERIC_WEBHOOK,
    configuration: dict[str, Any] | None = None,
    name: str | None = None,
) -> tuple[ObservabilitySource, str | None]:
    secret = generate_webhook_secret() if provider is SourceProvider.GENERIC_WEBHOOK else None
    source = ObservabilitySource(
        workspace_id=workspace.id,
        service_id=service.id,
        public_id=generate_public_id(),
        name=name or f"{provider.value}-{uuid.uuid4().hex[:6]}",
        provider=provider.value,
        source_type="webhook" if secret else "fixture",
        status=SourceStatus.ACTIVE.value,
        environment="production",
        configuration=configuration or {},
        secret_reference=encrypt_secret(settings, secret) if secret else None,
    )
    session.add(source)
    session.commit()
    return source, secret


def signed_headers(
    secret: str, body: bytes, timestamp: int | None = None, **extra: str
) -> dict[str, str]:
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    return {
        TIMESTAMP_HEADER: stamp,
        SIGNATURE_HEADER: compute_signature(secret, stamp, body),
        "content-type": "application/json",
        **extra,
    }


def alert_payload(
    event_id: str | None = None, title: str = "Checkout 5xx rate high"
) -> dict[str, Any]:
    from datetime import UTC, datetime

    payload: dict[str, Any] = {
        "type": "alert",
        "title": title,
        "message": "5xx above threshold",
        "severity": "high",
        "occurred_at": datetime.now(UTC).isoformat(),
        "error_signature": "checkout-5xx",
    }
    if event_id:
        payload["event_id"] = event_id
    return payload


def body_of(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def run_async[T](
    fn: Callable[[PipelineContext], Awaitable[T]], settings: Settings, bus: EventBus
) -> T:
    """Run an async pipeline function with a fresh engine bound to its own event loop."""

    async def runner() -> T:
        engine = create_db_engine(settings)
        try:
            return await fn(PipelineContext(settings, bus, create_session_factory(engine)))
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def run_pipeline(settings: Settings, bus: EventBus, rounds: int = 3) -> dict[str, int]:
    async def go(ctx: PipelineContext) -> dict[str, int]:
        handled = {"raw": 0, "normalized": 0}
        for _ in range(rounds):
            handled["raw"] += await drain(ctx, "raw", 0.0)
            handled["normalized"] += await drain(ctx, "normalized", 0.0)
        return handled

    return run_async(go, settings, bus)


def memory_bus(settings: Settings) -> InMemoryEventBus:
    bus = InMemoryEventBus(settings.KAFKA_PARTITIONS)
    from opspilot_api.observability.bus import Topics

    bus.ensure_topics(Topics.from_settings(settings).all(), settings.KAFKA_PARTITIONS)
    return bus
