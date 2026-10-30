"""Thin HTTP adapters for knowledge documents, ingestion jobs, and retrieval."""

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import ValidationError as PydanticValidationError
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from opspilot_api.config import Settings
from opspilot_api.incidents.service import ValidationError, publish_notification
from opspilot_api.knowledge.models import DocumentStatus
from opspilot_api.knowledge.query_enhancement import EnhancementError
from opspilot_api.knowledge.queue import enqueue_ingestion
from opspilot_api.knowledge.retrieval import search_knowledge
from opspilot_api.knowledge.schemas import (
    ActionResponse,
    DocumentDetail,
    DocumentMetadata,
    DocumentSummary,
    JobResponse,
    QueuedDocumentResponse,
    SearchRequest,
    SearchResponse,
)
from opspilot_api.knowledge.service import (
    add_version,
    archive_document,
    cancel_job,
    create_document,
    create_reindex_job,
    document_detail,
    list_documents,
    require_job,
    retry_job,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["knowledge"])


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def _value(values: Mapping[str, Any], name: str, default: Any = None) -> Any:
    value = values.get(name, default)
    return value if value != "" else default


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    raw = str(value)
    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("tags must be an array")
        return [str(item) for item in parsed]
    return [item.strip() for item in raw.split(",") if item.strip()]


async def _payload(
    request: Request, settings: Settings, include_metadata: bool
) -> tuple[DocumentMetadata | None, bytes, str | None, str | None]:
    content_type = request.headers.get("content-type", "")
    values: Mapping[str, Any]
    upload: UploadFile | None = None
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        values = form
        candidate = form.get("file")
        if isinstance(candidate, UploadFile):
            upload = candidate
    elif content_type.startswith("application/json"):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON request must be an object")
        values = body
    else:
        raise ValueError("use multipart/form-data or application/json")
    direct = _value(values, "content")
    if upload is not None:
        content = await upload.read(settings.KNOWLEDGE_MAX_FILE_BYTES + 1)
        filename = upload.filename
        mime_type = upload.content_type
    elif direct is not None:
        content = str(direct).encode("utf-8")
        mime_type = str(_value(values, "mime_type", "text/markdown"))
        filename = str(_value(values, "filename", "document.md"))
    else:
        raise ValueError("file or direct content is required")
    metadata = None
    if include_metadata:
        metadata = DocumentMetadata(
            title=str(_value(values, "title", "")),
            slug=str(_value(values, "slug", "")),
            description=_value(values, "description"),
            document_type=str(_value(values, "document_type", "runbook")),
            service_id=_value(values, "service_id"),
            tags=_tags(_value(values, "tags")),
        )
    return metadata, content, mime_type, filename


async def _notify(
    request: Request, workspace_id: UUID, event_type: str, data: dict[str, object]
) -> None:
    try:
        await publish_notification(request.app.state.redis, workspace_id, None, event_type, data)
    except RedisError as exc:
        logger.warning("knowledge notification failed", extra={"error_class": type(exc).__name__})


@router.post(
    "/workspaces/{workspace_id}/knowledge/documents",
    response_model=QueuedDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_document(
    request: Request, workspace_id: UUID, session: SessionDependency
) -> QueuedDocumentResponse:
    settings: Settings = request.app.state.settings
    try:
        metadata, content, mime_type, filename = await _payload(request, settings, True)
    except (ValueError, PydanticValidationError) as exc:
        raise ValidationError(str(exc)) from exc
    if metadata is None:
        raise RuntimeError("document metadata was not parsed")
    queued = await create_document(
        session, settings, workspace_id, metadata, content, mime_type, filename
    )
    enqueue_ingestion(settings, workspace_id, queued.job_id)
    await _notify(
        request,
        workspace_id,
        "knowledge.document_uploaded",
        {"document_id": str(queued.document_id), "job_id": str(queued.job_id)},
    )
    return queued


@router.get(
    "/workspaces/{workspace_id}/knowledge/documents",
    response_model=list[DocumentSummary],
)
async def get_documents(
    workspace_id: UUID,
    session: SessionDependency,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    document_type: str | None = None,
    service_id: UUID | None = None,
) -> list[DocumentSummary]:
    return await list_documents(session, workspace_id, status_filter, document_type, service_id)


@router.get(
    "/workspaces/{workspace_id}/knowledge/documents/{document_id}",
    response_model=DocumentDetail,
)
async def get_document(
    workspace_id: UUID, document_id: UUID, session: SessionDependency
) -> DocumentDetail:
    return await document_detail(session, workspace_id, document_id)


@router.post(
    "/workspaces/{workspace_id}/knowledge/documents/{document_id}/versions",
    response_model=QueuedDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_version(
    request: Request,
    workspace_id: UUID,
    document_id: UUID,
    session: SessionDependency,
) -> QueuedDocumentResponse:
    settings: Settings = request.app.state.settings
    try:
        _, content, mime_type, filename = await _payload(request, settings, False)
    except (ValueError, PydanticValidationError) as exc:
        raise ValidationError(str(exc)) from exc
    queued = await add_version(
        session, settings, workspace_id, document_id, content, mime_type, filename
    )
    enqueue_ingestion(settings, workspace_id, queued.job_id)
    await _notify(
        request,
        workspace_id,
        "knowledge.document_uploaded",
        {"document_id": str(document_id), "job_id": str(queued.job_id)},
    )
    return queued


@router.post(
    "/workspaces/{workspace_id}/knowledge/documents/{document_id}/reindex",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_reindex(
    request: Request,
    workspace_id: UUID,
    document_id: UUID,
    session: SessionDependency,
) -> JobResponse:
    settings: Settings = request.app.state.settings
    job = await create_reindex_job(session, workspace_id, document_id)
    enqueue_ingestion(settings, workspace_id, job.id)
    await _notify(
        request,
        workspace_id,
        "knowledge.document_reindexed",
        {"document_id": str(document_id), "job_id": str(job.id)},
    )
    return JobResponse.model_validate(job)


@router.post(
    "/workspaces/{workspace_id}/knowledge/documents/{document_id}/archive",
    response_model=ActionResponse,
)
async def post_archive(
    request: Request,
    workspace_id: UUID,
    document_id: UUID,
    session: SessionDependency,
) -> ActionResponse:
    document = await archive_document(session, workspace_id, document_id)
    await _notify(
        request,
        workspace_id,
        "knowledge.document_archived",
        {"document_id": str(document_id)},
    )
    return ActionResponse(document_id=document.id, status=DocumentStatus(document.status))


@router.get("/workspaces/{workspace_id}/knowledge/jobs/{job_id}", response_model=JobResponse)
async def get_job(workspace_id: UUID, job_id: UUID, session: SessionDependency) -> JobResponse:
    return JobResponse.model_validate(await require_job(session, workspace_id, job_id))


@router.post(
    "/workspaces/{workspace_id}/knowledge/jobs/{job_id}/cancel",
    response_model=JobResponse,
)
async def post_cancel_job(
    request: Request,
    workspace_id: UUID,
    job_id: UUID,
    session: SessionDependency,
) -> JobResponse:
    job = await cancel_job(session, workspace_id, job_id)
    await _notify(
        request,
        workspace_id,
        "knowledge.ingestion_cancelled",
        {"document_id": str(job.document_id), "job_id": str(job.id)},
    )
    return JobResponse.model_validate(job)


@router.post(
    "/workspaces/{workspace_id}/knowledge/jobs/{job_id}/retry",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_retry_job(
    request: Request,
    workspace_id: UUID,
    job_id: UUID,
    session: SessionDependency,
) -> JobResponse:
    settings: Settings = request.app.state.settings
    job = await retry_job(session, workspace_id, job_id)
    enqueue_ingestion(settings, workspace_id, job.id)
    return JobResponse.model_validate(job)


@router.post("/workspaces/{workspace_id}/knowledge/search", response_model=SearchResponse)
async def post_search(request: Request, workspace_id: UUID, data: SearchRequest) -> SearchResponse:
    settings: Settings = request.app.state.settings
    try:
        return await search_knowledge(settings, workspace_id, data)
    except (ValueError, EnhancementError) as exc:
        raise ValidationError(str(exc)) from exc
