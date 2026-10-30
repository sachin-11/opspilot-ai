"""Workspace-scoped knowledge document, version, and ingestion-job services."""

import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.config import Settings
from opspilot_api.incidents.models import MonitoredService, Workspace
from opspilot_api.incidents.service import ConflictError, NotFoundError, utcnow
from opspilot_api.knowledge.extractors import validate_and_detect
from opspilot_api.knowledge.models import (
    DocumentStatus,
    IngestionStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
    VersionStatus,
)
from opspilot_api.knowledge.retrieval import Candidate, citation_label
from opspilot_api.knowledge.schemas import (
    ChunkPreview,
    DocumentDetail,
    DocumentMetadata,
    DocumentSummary,
    JobResponse,
    QueuedDocumentResponse,
    VersionResponse,
)
from opspilot_api.knowledge.storage import LocalStorage, ObjectStorage, StorageError


def storage_for(settings: Settings) -> ObjectStorage:
    if settings.KNOWLEDGE_STORAGE_BACKEND == "local":
        return LocalStorage(settings.KNOWLEDGE_STORAGE_ROOT)
    raise StorageError("S3 storage requires an application-provided adapter")


async def _require_workspace(session: AsyncSession, workspace_id: UUID) -> None:
    if await session.scalar(select(Workspace.id).where(Workspace.id == workspace_id)) is None:
        raise NotFoundError("workspace not found")


async def _validate_service(
    session: AsyncSession, workspace_id: UUID, service_id: UUID | None
) -> None:
    if service_id is None:
        return
    found = await session.scalar(
        select(MonitoredService.id).where(
            MonitoredService.id == service_id,
            MonitoredService.workspace_id == workspace_id,
        )
    )
    if found is None:
        raise NotFoundError("monitored service not found in workspace")


async def require_document(
    session: AsyncSession, workspace_id: UUID, document_id: UUID
) -> KnowledgeDocument:
    document = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.workspace_id == workspace_id,
        )
    )
    if document is None:
        raise NotFoundError("knowledge document not found")
    return document


async def require_job(
    session: AsyncSession, workspace_id: UUID, job_id: UUID
) -> KnowledgeIngestionJob:
    job = await session.scalar(
        select(KnowledgeIngestionJob).where(
            KnowledgeIngestionJob.id == job_id,
            KnowledgeIngestionJob.workspace_id == workspace_id,
        )
    )
    if job is None:
        raise NotFoundError("knowledge ingestion job not found")
    return job


async def _next_version_number(session: AsyncSession, document_id: UUID) -> int:
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(document_id), 0)))
    )
    current = await session.scalar(
        select(func.max(KnowledgeDocumentVersion.version_number)).where(
            KnowledgeDocumentVersion.document_id == document_id
        )
    )
    return int(current or 0) + 1


async def _create_version(
    session: AsyncSession,
    settings: Settings,
    document: KnowledgeDocument,
    content: bytes,
    mime_type: str | None,
    filename: str | None,
) -> tuple[KnowledgeDocumentVersion, KnowledgeIngestionJob]:
    detected = validate_and_detect(content, mime_type, filename, settings)
    content_hash = hashlib.sha256(content).hexdigest()
    storage = storage_for(settings)
    storage_key = storage.put(document.workspace_id, content, content_hash)
    try:
        version = KnowledgeDocumentVersion(
            id=uuid4(),
            workspace_id=document.workspace_id,
            document_id=document.id,
            version_number=await _next_version_number(session, document.id),
            original_filename=filename[:255] if filename else None,
            mime_type=detected,
            storage_key=storage_key,
            content_hash=content_hash,
            byte_size=len(content),
            chunk_count=0,
            status=VersionStatus.UPLOADED.value,
        )
        job = KnowledgeIngestionJob(
            id=uuid4(),
            workspace_id=document.workspace_id,
            document_id=document.id,
            document_version_id=version.id,
            status=IngestionStatus.QUEUED.value,
            current_stage="queued",
            attempt=1,
            chunks_processed=0,
        )
        document.status = DocumentStatus.PROCESSING.value
        document.updated_at = utcnow()
        session.add_all([version, job])
        await session.commit()
        await session.refresh(version)
        await session.refresh(job)
        return version, job
    except BaseException:
        await session.rollback()
        storage.delete(storage_key)
        raise


async def create_document(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    metadata: DocumentMetadata,
    content: bytes,
    mime_type: str | None,
    filename: str | None,
) -> QueuedDocumentResponse:
    await _require_workspace(session, workspace_id)
    await _validate_service(session, workspace_id, metadata.service_id)
    document = KnowledgeDocument(
        id=uuid4(),
        workspace_id=workspace_id,
        service_id=metadata.service_id,
        title=metadata.title,
        slug=metadata.slug,
        description=metadata.description,
        document_type=metadata.document_type.value,
        status=DocumentStatus.DRAFT.value,
        tags=metadata.tags,
    )
    session.add(document)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("document slug already exists in workspace") from exc
    version, job = await _create_version(session, settings, document, content, mime_type, filename)
    return QueuedDocumentResponse(document_id=document.id, version_id=version.id, job_id=job.id)


async def add_version(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    document_id: UUID,
    content: bytes,
    mime_type: str | None,
    filename: str | None,
) -> QueuedDocumentResponse:
    document = await require_document(session, workspace_id, document_id)
    if document.status == DocumentStatus.ARCHIVED.value:
        raise ConflictError("archived document cannot receive a new version")
    version, job = await _create_version(session, settings, document, content, mime_type, filename)
    return QueuedDocumentResponse(document_id=document.id, version_id=version.id, job_id=job.id)


async def list_documents(
    session: AsyncSession,
    workspace_id: UUID,
    status: DocumentStatus | None = None,
    document_type: str | None = None,
    service_id: UUID | None = None,
) -> list[DocumentSummary]:
    statement = (
        select(KnowledgeDocument, MonitoredService.name, KnowledgeDocumentVersion)
        .outerjoin(
            MonitoredService,
            (MonitoredService.id == KnowledgeDocument.service_id)
            & (MonitoredService.workspace_id == workspace_id),
        )
        .outerjoin(
            KnowledgeDocumentVersion,
            (KnowledgeDocumentVersion.id == KnowledgeDocument.current_version_id)
            & (KnowledgeDocumentVersion.workspace_id == workspace_id),
        )
        .where(KnowledgeDocument.workspace_id == workspace_id)
        .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id)
    )
    if status is None:
        statement = statement.where(KnowledgeDocument.status != DocumentStatus.ARCHIVED.value)
    else:
        statement = statement.where(KnowledgeDocument.status == status.value)
    if document_type:
        statement = statement.where(KnowledgeDocument.document_type == document_type)
    if service_id:
        statement = statement.where(KnowledgeDocument.service_id == service_id)
    rows = (await session.execute(statement)).all()
    return [
        DocumentSummary(
            **DocumentSummary.model_validate(row[0]).model_dump(
                exclude={"service_name", "current_version_number", "chunk_count"}
            ),
            service_name=row[1],
            current_version_number=row[2].version_number if row[2] else None,
            chunk_count=row[2].chunk_count if row[2] else 0,
        )
        for row in rows
    ]


async def document_detail(
    session: AsyncSession, workspace_id: UUID, document_id: UUID
) -> DocumentDetail:
    document = await require_document(session, workspace_id, document_id)
    service_name = None
    if document.service_id:
        service_name = await session.scalar(
            select(MonitoredService.name).where(
                MonitoredService.id == document.service_id,
                MonitoredService.workspace_id == workspace_id,
            )
        )
    versions = list(
        (
            await session.scalars(
                select(KnowledgeDocumentVersion)
                .where(
                    KnowledgeDocumentVersion.workspace_id == workspace_id,
                    KnowledgeDocumentVersion.document_id == document_id,
                )
                .order_by(KnowledgeDocumentVersion.version_number.desc())
            )
        ).all()
    )
    jobs = list(
        (
            await session.scalars(
                select(KnowledgeIngestionJob)
                .where(
                    KnowledgeIngestionJob.workspace_id == workspace_id,
                    KnowledgeIngestionJob.document_id == document_id,
                )
                .order_by(KnowledgeIngestionJob.created_at.desc())
            )
        ).all()
    )
    chunks: list[KnowledgeChunk] = []
    if document.current_version_id:
        chunks = list(
            (
                await session.scalars(
                    select(KnowledgeChunk)
                    .where(
                        KnowledgeChunk.workspace_id == workspace_id,
                        KnowledgeChunk.document_id == document_id,
                        KnowledgeChunk.document_version_id == document.current_version_id,
                    )
                    .order_by(KnowledgeChunk.chunk_index)
                    .limit(20)
                )
            ).all()
        )
    current = next((item for item in versions if item.id == document.current_version_id), None)
    summary = DocumentSummary.model_validate(document).model_dump()
    summary.update(
        service_name=service_name,
        current_version_number=current.version_number if current else None,
        chunk_count=current.chunk_count if current else 0,
    )
    return DocumentDetail(
        **summary,
        versions=[VersionResponse.model_validate(item) for item in versions],
        jobs=[JobResponse.model_validate(item) for item in jobs],
        chunks=[
            ChunkPreview(
                id=chunk.id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                heading_path=chunk.heading_path,
                page_number=chunk.page_number,
                token_count=chunk.token_count,
                citation_label=citation_label(Candidate(chunk, document, service_name)),
            )
            for chunk in chunks
        ],
    )


async def create_reindex_job(
    session: AsyncSession, workspace_id: UUID, document_id: UUID
) -> KnowledgeIngestionJob:
    document = await require_document(session, workspace_id, document_id)
    if document.status == DocumentStatus.ARCHIVED.value or document.current_version_id is None:
        raise ConflictError("only a ready, active document can be reindexed")
    current = await session.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.id == document.current_version_id,
            KnowledgeDocumentVersion.workspace_id == workspace_id,
            KnowledgeDocumentVersion.status == VersionStatus.READY.value,
        )
    )
    if current is None:
        raise ConflictError("current ready version not found")
    existing = await session.scalar(
        select(KnowledgeIngestionJob).where(
            KnowledgeIngestionJob.document_id == document.id,
            KnowledgeIngestionJob.status.in_(
                [IngestionStatus.QUEUED.value, IngestionStatus.RUNNING.value]
            ),
        )
    )
    if existing:
        return existing
    version = KnowledgeDocumentVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        document_id=document.id,
        version_number=await _next_version_number(session, document.id),
        original_filename=current.original_filename,
        mime_type=current.mime_type,
        storage_key=current.storage_key,
        content_hash=current.content_hash,
        byte_size=current.byte_size,
        chunk_count=0,
        status=VersionStatus.UPLOADED.value,
    )
    job = KnowledgeIngestionJob(
        id=uuid4(),
        workspace_id=workspace_id,
        document_id=document_id,
        document_version_id=version.id,
        status=IngestionStatus.QUEUED.value,
        current_stage="reindex_queued",
        attempt=1,
        chunks_processed=0,
    )
    document.status = DocumentStatus.PROCESSING.value
    session.add_all([version, job])
    await session.commit()
    await session.refresh(job)
    return job


async def archive_document(
    session: AsyncSession, workspace_id: UUID, document_id: UUID
) -> KnowledgeDocument:
    document = await require_document(session, workspace_id, document_id)
    if document.status == DocumentStatus.ARCHIVED.value:
        return document
    document.status = DocumentStatus.ARCHIVED.value
    document.archived_at = utcnow()
    document.updated_at = utcnow()
    await session.commit()
    await session.refresh(document)
    return document


async def cancel_job(
    session: AsyncSession, workspace_id: UUID, job_id: UUID
) -> KnowledgeIngestionJob:
    job = await require_job(session, workspace_id, job_id)
    if job.status == IngestionStatus.CANCELLED.value:
        return job
    if job.status in {IngestionStatus.COMPLETED.value, IngestionStatus.FAILED.value}:
        raise ConflictError("terminal ingestion job cannot be cancelled")
    job.status = IngestionStatus.CANCELLED.value
    job.current_stage = "cancelled"
    job.completed_at = utcnow()
    job.updated_at = utcnow()
    document = await require_document(session, workspace_id, job.document_id)
    document.status = (
        DocumentStatus.READY.value
        if document.current_version_id is not None
        else DocumentStatus.FAILED.value
    )
    document.updated_at = utcnow()
    await session.commit()
    await session.refresh(job)
    return job


async def retry_job(
    session: AsyncSession, workspace_id: UUID, job_id: UUID
) -> KnowledgeIngestionJob:
    previous = await require_job(session, workspace_id, job_id)
    if previous.status != IngestionStatus.FAILED.value:
        raise ConflictError("only failed ingestion jobs can be retried")
    job = KnowledgeIngestionJob(
        id=uuid4(),
        workspace_id=workspace_id,
        document_id=previous.document_id,
        document_version_id=previous.document_version_id,
        status=IngestionStatus.QUEUED.value,
        current_stage="retry_queued",
        attempt=previous.attempt + 1,
        chunks_processed=0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def recover_stale_jobs(session: AsyncSession, settings: Settings) -> int:
    cutoff = utcnow() - timedelta(seconds=max(60, settings.AI_MAX_RUNTIME_SECONDS * 2))
    result = await session.execute(
        update(KnowledgeIngestionJob)
        .where(
            KnowledgeIngestionJob.status == IngestionStatus.RUNNING.value,
            KnowledgeIngestionJob.updated_at < cutoff,
        )
        .values(
            status=IngestionStatus.FAILED.value,
            current_stage="failed",
            failed_at=utcnow(),
            failure_code="stale_worker",
            failure_message="Ingestion worker stopped before completion.",
            updated_at=utcnow(),
        )
        .returning(KnowledgeIngestionJob.id)
    )
    ids = list(result.scalars())
    await session.commit()
    return len(ids)
