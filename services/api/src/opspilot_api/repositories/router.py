"""REST endpoints for the read-only GitHub integration (no mutation of GitHub state exists)."""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.config import Settings
from opspilot_api.incidents.service import ValidationError
from opspilot_api.repositories import service
from opspilot_api.repositories.events import publish_repository_event
from opspilot_api.repositories.models import (
    DeliveryStatus,
    GitHubWebhookDelivery,
    RepositoryStatus,
)
from opspilot_api.repositories.provider import RepositoryProvider
from opspilot_api.repositories.queue import (
    enqueue_installation_sync,
    enqueue_repository_sync,
    enqueue_webhook,
    get_provider,
)
from opspilot_api.repositories.retrieval import search_code
from opspilot_api.repositories.schemas import (
    CodeSearchRequest,
    CodeSearchResponse,
    CommitResponse,
    DeploymentResponse,
    InstallationListResponse,
    InstallationSyncRequest,
    MappingCreate,
    MappingResponse,
    QueuedResponse,
    RepositoryDetail,
    RepositorySummary,
    SuspectChangesResponse,
)
from opspilot_api.repositories.webhooks import normalize_event, verify_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["repositories"])


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def _provider(request: Request) -> RepositoryProvider:
    override: RepositoryProvider | None = getattr(request.app.state, "repository_provider", None)
    return override or get_provider(request.app.state.settings)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def _publish(request: Request, workspace_id: UUID, event: str, data: dict[str, Any]) -> None:
    await asyncio.to_thread(publish_repository_event, _settings(request), workspace_id, event, data)


@router.get(
    "/workspaces/{workspace_id}/integrations/github/installations",
    response_model=InstallationListResponse,
)
async def get_installations(
    request: Request, workspace_id: UUID, session: SessionDependency
) -> InstallationListResponse:
    return await service.list_installations(session, _settings(request), workspace_id)


@router.post(
    "/workspaces/{workspace_id}/integrations/github/installations/sync",
    response_model=QueuedResponse,
    status_code=202,
)
async def post_installation_sync(
    request: Request, workspace_id: UUID, data: InstallationSyncRequest
) -> QueuedResponse:
    settings = _settings(request)
    installation_id = await asyncio.to_thread(
        service.connect_installation,
        settings,
        _provider(request),
        workspace_id,
        data.provider_installation_id,
    )
    await asyncio.to_thread(enqueue_installation_sync, settings, workspace_id, installation_id)
    return QueuedResponse(status="queued", installation_id=installation_id)


@router.get("/workspaces/{workspace_id}/repositories", response_model=list[RepositorySummary])
async def get_repositories(
    workspace_id: UUID,
    session: SessionDependency,
    status_filter: Annotated[RepositoryStatus | None, Query(alias="status")] = None,
    service_id: UUID | None = None,
) -> list[RepositorySummary]:
    return await service.list_repositories(session, workspace_id, status_filter, service_id)


@router.post("/workspaces/{workspace_id}/repositories/search", response_model=CodeSearchResponse)
async def post_repository_search(
    request: Request, workspace_id: UUID, data: CodeSearchRequest
) -> CodeSearchResponse:
    try:
        return await search_code(_settings(request), workspace_id, data)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@router.get(
    "/workspaces/{workspace_id}/repositories/{repository_id}", response_model=RepositoryDetail
)
async def get_repository(
    workspace_id: UUID, repository_id: UUID, session: SessionDependency
) -> RepositoryDetail:
    return await service.repository_detail(session, workspace_id, repository_id)


async def _queue_sync(
    request: Request,
    workspace_id: UUID,
    repository_id: UUID,
    session: AsyncSession,
    force: bool,
) -> QueuedResponse:
    repository = await service.require_syncable(session, workspace_id, repository_id)
    await asyncio.to_thread(
        enqueue_repository_sync,
        _settings(request),
        workspace_id,
        repository.id,
        force_reindex=force,
    )
    return QueuedResponse(status="queued", repository_id=repository.id)


@router.post(
    "/workspaces/{workspace_id}/repositories/{repository_id}/sync",
    response_model=QueuedResponse,
    status_code=202,
)
async def post_repository_sync(
    request: Request, workspace_id: UUID, repository_id: UUID, session: SessionDependency
) -> QueuedResponse:
    return await _queue_sync(request, workspace_id, repository_id, session, force=False)


@router.post(
    "/workspaces/{workspace_id}/repositories/{repository_id}/reindex",
    response_model=QueuedResponse,
    status_code=202,
)
async def post_repository_reindex(
    request: Request, workspace_id: UUID, repository_id: UUID, session: SessionDependency
) -> QueuedResponse:
    return await _queue_sync(request, workspace_id, repository_id, session, force=True)


@router.get(
    "/workspaces/{workspace_id}/repositories/{repository_id}/commits",
    response_model=list[CommitResponse],
)
async def get_commits(
    workspace_id: UUID,
    repository_id: UUID,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[CommitResponse]:
    return await service.list_commits(session, workspace_id, repository_id, limit)


@router.get(
    "/workspaces/{workspace_id}/repositories/{repository_id}/deployments",
    response_model=list[DeploymentResponse],
)
async def get_deployments(
    workspace_id: UUID,
    repository_id: UUID,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[DeploymentResponse]:
    return await service.list_deployments(session, workspace_id, repository_id, limit)


@router.post(
    "/workspaces/{workspace_id}/services/{service_id}/repositories",
    response_model=MappingResponse,
    status_code=201,
)
async def post_mapping(
    request: Request,
    workspace_id: UUID,
    service_id: UUID,
    data: MappingCreate,
    session: SessionDependency,
) -> MappingResponse:
    mapping = await service.create_mapping(session, workspace_id, service_id, data)
    await _publish(
        request,
        workspace_id,
        "repository.mapping_changed",
        {"repository_id": mapping.repository_id, "service_id": service_id, "status": "created"},
    )
    return mapping


@router.get(
    "/workspaces/{workspace_id}/services/{service_id}/repositories",
    response_model=list[MappingResponse],
)
async def get_mappings(
    workspace_id: UUID, service_id: UUID, session: SessionDependency
) -> list[MappingResponse]:
    return await service.list_mappings(session, workspace_id, service_id)


@router.delete(
    "/workspaces/{workspace_id}/services/{service_id}/repositories/{mapping_id}", status_code=204
)
async def delete_mapping(
    request: Request,
    workspace_id: UUID,
    service_id: UUID,
    mapping_id: UUID,
    session: SessionDependency,
) -> Response:
    repository_id = await service.delete_mapping(session, workspace_id, service_id, mapping_id)
    await _publish(
        request,
        workspace_id,
        "repository.mapping_changed",
        {
            "repository_id": repository_id,
            "service_id": service_id,
            "mapping_id": mapping_id,
            "status": "removed",
        },
    )
    return Response(status_code=204)


@router.get(
    "/workspaces/{workspace_id}/incidents/{incident_id}/suspect-changes",
    response_model=SuspectChangesResponse,
)
async def get_suspect_changes(
    request: Request, workspace_id: UUID, incident_id: UUID
) -> SuspectChangesResponse:
    return await service.suspect_changes_async(_settings(request), workspace_id, incident_id)


# -- webhook (outside workspace routing; GitHub calls it directly) ---------------------------
def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "correlation_id": None}},
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


@router.post("/integrations/github/webhook", status_code=202)
async def github_webhook(request: Request) -> JSONResponse:
    settings = _settings(request)
    secret = settings.GITHUB_WEBHOOK_SECRET
    if secret is None:
        return _error(503, "webhook_not_configured", "GitHub webhook secret is not configured")
    body = await _read_bounded(request, settings.GITHUB_WEBHOOK_MAX_BYTES)
    if body is None:
        return _error(413, "content_too_large", "Webhook payload exceeds the configured limit")
    # Signature first, over the raw bytes, before any parsing or database access.
    if not verify_signature(
        secret.get_secret_value(), body, request.headers.get("x-hub-signature-256")
    ):
        return _error(401, "invalid_signature", "Webhook signature verification failed")
    delivery_id = request.headers.get("x-github-delivery", "").strip()
    event_type = request.headers.get("x-github-event", "").strip()
    if not delivery_id or len(delivery_id) > 100:
        return _error(400, "missing_delivery_id", "X-GitHub-Delivery header is required")
    if not event_type or len(event_type) > 60:
        return _error(400, "missing_event", "X-GitHub-Event header is required")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _error(400, "invalid_payload", "Webhook payload is not valid JSON")
    if not isinstance(payload, dict):
        return _error(400, "invalid_payload", "Webhook payload must be a JSON object")
    event = normalize_event(event_type, payload)
    ignored = not event.supported
    async with request.app.state.session_factory() as session:
        delivery = GitHubWebhookDelivery(
            provider_delivery_id=delivery_id,
            event_type=event_type,
            action=event.action,
            installation_id=event.installation_id,
            repository_id=event.repository_id,
            status=(DeliveryStatus.IGNORED if ignored else DeliveryStatus.RECEIVED).value,
            payload_hash=hashlib.sha256(body).hexdigest(),
            normalized=event.fields,
            failure_code=event.ignore_reason if ignored else None,
        )
        session.add(delivery)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await _redeliver(request, delivery_id)
        await session.refresh(delivery)
        record_id = delivery.id
    if ignored:
        return JSONResponse(status_code=202, content={"status": "ignored"})
    try:
        await asyncio.to_thread(enqueue_webhook, settings, record_id)
    except Exception as exc:
        logger.error("webhook enqueue failed", extra={"error_class": type(exc).__name__})
        async with request.app.state.session_factory() as session:
            stored = await session.get(GitHubWebhookDelivery, record_id)
            if stored is not None:
                stored.status = DeliveryStatus.FAILED.value
                stored.failure_code = "enqueue_failed"
                await session.commit()
        return _error(503, "queue_unavailable", "Webhook could not be queued; retry the delivery")
    return JSONResponse(status_code=202, content={"status": "queued"})


async def _redeliver(request: Request, delivery_id: str) -> JSONResponse:
    """A repeated delivery ID never duplicates work, except after our own enqueue failure."""
    settings = _settings(request)
    async with request.app.state.session_factory() as session:
        existing = await session.scalar(
            select(GitHubWebhookDelivery).where(
                GitHubWebhookDelivery.provider_delivery_id == delivery_id
            )
        )
        if (
            existing is not None
            and existing.status == DeliveryStatus.FAILED.value
            and existing.failure_code == "enqueue_failed"
        ):
            existing.status = DeliveryStatus.RECEIVED.value
            existing.failure_code = None
            await session.commit()
            await asyncio.to_thread(enqueue_webhook, settings, existing.id)
            return JSONResponse(status_code=202, content={"status": "queued"})
    return JSONResponse(status_code=200, content={"status": "duplicate"})
