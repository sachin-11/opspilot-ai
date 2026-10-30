"""Observability REST API and the signed public webhook ingress."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis import Redis
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.service import NotFoundError
from opspilot_api.observability import service
from opspilot_api.observability.backpressure import Backpressure
from opspilot_api.observability.bus import EventBus, get_bus
from opspilot_api.observability.events import publish_observability_event
from opspilot_api.observability.ingress import IngressRejected, accept_signal, event_id_from
from opspilot_api.observability.models import ObservabilitySource, SourceProvider, SourceStatus
from opspilot_api.observability.schemas import (
    DeadLetterPage,
    DemoGenerateRequest,
    DemoGenerateResponse,
    IncidentObservability,
    PipelineStatus,
    ReplayResponse,
    SecretRotated,
    SignalDetail,
    SignalPage,
    SourceCreate,
    SourceCreated,
    SourceResponse,
    SourceTestResult,
    SourceUpdate,
    WebhookAccepted,
)
from opspilot_api.observability.secrets_util import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SecretsConfigurationError,
    SignatureResult,
    decrypt_secret,
    verify_signature,
)
from opspilot_api.observability.telemetry import REJECTED, SIGNATURE_FAILURES, span
from opspilot_api.observability.tools import collect_incident_observability

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["observability"])


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _bus(request: Request) -> EventBus:
    override: EventBus | None = getattr(request.app.state, "event_bus", None)
    return override or get_bus(_settings(request))


def _redis(request: Request) -> Redis:
    client: Redis | None = getattr(request.app.state, "sync_redis", None)
    if client is None:
        client = Redis.from_url(
            _settings(request).REDIS_URL.get_secret_value(),
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        request.app.state.sync_redis = client
    return client


def _backpressure(request: Request) -> Backpressure:
    return Backpressure(_settings(request), _bus(request), _redis(request))


async def _publish(request: Request, workspace_id: UUID, event: str, data: dict[str, Any]) -> None:
    await asyncio.to_thread(
        publish_observability_event, _settings(request), workspace_id, event, data
    )


# -- sources ---------------------------------------------------------------------------------
@router.post(
    "/workspaces/{workspace_id}/observability/sources",
    response_model=SourceCreated,
    status_code=201,
)
async def post_source(
    request: Request, workspace_id: UUID, data: SourceCreate, session: SessionDependency
) -> SourceCreated:
    return await service.create_source(session, _settings(request), workspace_id, data)


@router.get("/workspaces/{workspace_id}/observability/sources", response_model=list[SourceResponse])
async def get_sources(workspace_id: UUID, session: SessionDependency) -> list[SourceResponse]:
    return await service.list_sources(session, workspace_id)


@router.get(
    "/workspaces/{workspace_id}/observability/sources/{source_id}", response_model=SourceResponse
)
async def get_one_source(
    workspace_id: UUID, source_id: UUID, session: SessionDependency
) -> SourceResponse:
    return await service.get_source(session, workspace_id, source_id)


@router.patch(
    "/workspaces/{workspace_id}/observability/sources/{source_id}", response_model=SourceResponse
)
async def patch_source(
    request: Request,
    workspace_id: UUID,
    source_id: UUID,
    data: SourceUpdate,
    session: SessionDependency,
) -> SourceResponse:
    source, changed = await service.update_source(
        session, _settings(request), workspace_id, source_id, data
    )
    if changed:
        await _publish(
            request, workspace_id, "observability.source_status_changed",
            {"source_id": source_id, "status": source.status.value},
        )  # fmt: skip
    return source


@router.post(
    "/workspaces/{workspace_id}/observability/sources/{source_id}/test",
    response_model=SourceTestResult,
)
async def post_source_test(
    request: Request, workspace_id: UUID, source_id: UUID, session: SessionDependency
) -> SourceTestResult:
    return await service.test_source(
        session, _settings(request), _bus(request), workspace_id, source_id
    )


@router.post(
    "/workspaces/{workspace_id}/observability/sources/{source_id}/rotate-secret",
    response_model=SecretRotated,
)
async def post_rotate_secret(
    request: Request, workspace_id: UUID, source_id: UUID, session: SessionDependency
) -> SecretRotated:
    return await service.rotate_secret(session, _settings(request), workspace_id, source_id)


# -- signals and pipeline ---------------------------------------------------------------------
@router.get("/workspaces/{workspace_id}/observability/signals", response_model=SignalPage)
async def get_signals(
    workspace_id: UUID,
    session: SessionDependency,
    source_id: UUID | None = None,
    service_id: UUID | None = None,
    status: str | None = Query(default=None, max_length=30),
    severity: str | None = Query(default=None, max_length=4),
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    cursor: str | None = Query(default=None, max_length=200),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SignalPage:
    return await service.list_signals(
        session, workspace_id, source_id=source_id, service_id=service_id, status=status,
        severity=severity, start=from_time, end=to_time, cursor=cursor, limit=limit,
    )  # fmt: skip


@router.get(
    "/workspaces/{workspace_id}/observability/signals/{signal_id}", response_model=SignalDetail
)
async def get_one_signal(
    workspace_id: UUID, signal_id: UUID, session: SessionDependency
) -> SignalDetail:
    return await service.get_signal(session, workspace_id, signal_id)


@router.get("/workspaces/{workspace_id}/observability/pipeline", response_model=PipelineStatus)
async def get_pipeline(
    request: Request,
    workspace_id: UUID,
    session: SessionDependency,
    window_minutes: Annotated[int, Query(ge=1, le=1440)] = 15,
) -> PipelineStatus:
    backpressure = _backpressure(request)

    def reasons(workspace: UUID) -> list[str]:
        return backpressure.active_reasons(workspace)

    return await service.pipeline_status(
        session, _settings(request), _bus(request), reasons, workspace_id, window_minutes
    )


@router.get("/workspaces/{workspace_id}/observability/dead-letters", response_model=DeadLetterPage)
async def get_dead_letters(
    workspace_id: UUID,
    session: SessionDependency,
    cursor: str | None = Query(default=None, max_length=200),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DeadLetterPage:
    return await service.list_dead_letters(session, workspace_id, cursor, limit)


@router.post(
    "/workspaces/{workspace_id}/observability/dead-letters/{dead_letter_id}/replay",
    response_model=ReplayResponse,
)
async def post_replay(
    request: Request, workspace_id: UUID, dead_letter_id: UUID, session: SessionDependency
) -> ReplayResponse:
    return await service.replay_dead_letter(
        session, _settings(request), _bus(request), workspace_id, dead_letter_id
    )


@router.post(
    "/workspaces/{workspace_id}/observability/demo/generate", response_model=DemoGenerateResponse
)
async def post_demo(
    request: Request, workspace_id: UUID, data: DemoGenerateRequest, session: SessionDependency
) -> DemoGenerateResponse:
    return await service.generate_demo(
        session, _settings(request), _bus(request), workspace_id, data
    )


@router.get(
    "/workspaces/{workspace_id}/incidents/{incident_id}/observability",
    response_model=IncidentObservability,
)
async def get_incident_observability(
    request: Request, workspace_id: UUID, incident_id: UUID
) -> IncidentObservability:
    settings = _settings(request)

    def run() -> IncidentObservability:
        from sqlalchemy import create_engine

        engine = create_engine(settings.database_url, pool_pre_ping=True)
        try:
            with Session(engine) as sync_session:
                return collect_incident_observability(
                    sync_session, settings, workspace_id, incident_id
                )
        except LookupError as exc:
            raise NotFoundError("incident not found") from exc
        finally:
            engine.dispose()

    return await asyncio.to_thread(run)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus text exposition; counters and histograms contain no payloads or secrets."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# -- public signed webhook ---------------------------------------------------------------------
def _error(
    status_code: int, code: str, message: str, retry_after: int | None = None
) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "correlation_id": None}},
        headers=headers,
    )


async def _read_bounded(request: Request, limit: int) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


@router.post("/ingest/webhooks/{public_id}", response_model=WebhookAccepted, status_code=202)
async def ingest_webhook(request: Request, public_id: str) -> JSONResponse:
    """Verify first, then durably accept; incidents are created asynchronously by consumers."""
    settings = _settings(request)
    try:
        return await _ingest(request, settings, public_id)
    except (OperationalError, InterfaceError, DBAPIError) as exc:
        logger.error("webhook storage unavailable", extra={"error_class": type(exc).__name__})
        REJECTED.labels(reason="storage_unavailable").inc()
        return _error(503, "storage_unavailable", "Signal could not be durably accepted", 5)


async def _ingest(request: Request, settings: Settings, public_id: str) -> JSONResponse:
    async with request.app.state.session_factory() as session:
        source = await session.scalar(
            select(ObservabilitySource).where(ObservabilitySource.public_id == public_id[:64])
        )
        if source is None or source.provider != SourceProvider.GENERIC_WEBHOOK.value:
            return _error(404, "not_found", "Unknown webhook source")
        if source.status == SourceStatus.DISABLED.value:
            return _error(403, "source_disabled", "This source is disabled")
        with span("webhook.receive", source__id=source.id, workspace__id=source.workspace_id):
            body = await _read_bounded(request, settings.OBS_WEBHOOK_MAX_BYTES)
            if body is None:
                REJECTED.labels(reason="too_large").inc()
                return _error(413, "content_too_large", "Webhook payload exceeds the size limit")
            try:
                secret = decrypt_secret(settings, source.secret_reference or "")
            except SecretsConfigurationError:
                return _error(
                    503, "secrets_not_configured", "Webhook verification is unavailable", 30
                )
            # Signature first: nothing is parsed, queued, or rate-counted for unauthenticated calls.
            result = verify_signature(
                secret,
                request.headers.get(TIMESTAMP_HEADER),
                request.headers.get(SIGNATURE_HEADER),
                body,
                time.time(),
                settings.OBS_WEBHOOK_REPLAY_WINDOW_SECONDS,
            )
            if result is not SignatureResult.OK:
                SIGNATURE_FAILURES.labels(reason=result.value).inc()
                REJECTED.labels(reason=f"signature_{result.value}").inc()
                return _error(
                    401, f"signature_{result.value}", "Webhook signature verification failed"
                )
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                REJECTED.labels(reason="invalid_json").inc()
                return _error(400, "invalid_payload", "Webhook payload is not valid JSON")
            if not isinstance(payload, dict):
                REJECTED.labels(reason="invalid_json").inc()
                return _error(400, "invalid_payload", "Webhook payload must be a JSON object")
            recent = await service.dead_letters_recent(session, source.workspace_id)
            decision = await asyncio.to_thread(
                _backpressure(request).check, source.workspace_id, source.id, recent
            )
            if not decision.allowed:
                REJECTED.labels(reason=decision.reason or "backpressure").inc()
                return _error(
                    decision.status_code,
                    decision.reason or "backpressure",
                    "Ingestion is applying backpressure; retry later",
                    decision.retry_after,
                )
            try:
                outcome = await accept_signal(
                    settings, _bus(request), session, source, payload, body,
                    provider_event_id=event_id_from(
                        payload, request.headers.get("x-opspilot-event-id")
                    ),
                    idempotency_key=request.headers.get("idempotency-key"),
                )  # fmt: skip
            except IngressRejected as exc:
                return _error(exc.status_code, exc.code, str(exc), exc.retry_after)
    event = (
        "observability.signal_duplicate"
        if outcome.status == "duplicate"
        else "observability.signal_received"
    )
    await _publish(
        request, outcome.workspace_id, event,
        {"signal_id": outcome.signal_id, "source_id": outcome.source_id, "status": outcome.status},
    )  # fmt: skip
    return JSONResponse(
        status_code=202 if outcome.status == "accepted" else 200,
        content={"status": outcome.status, "signal_id": str(outcome.signal_id)},
    )
