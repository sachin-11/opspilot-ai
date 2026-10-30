"""Idempotent RQ ingestion pipeline for extraction, redaction, chunking, and embeddings."""

import hashlib
import logging
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.schemas import RealtimeEnvelope
from opspilot_api.incidents.service import utcnow
from opspilot_api.investigations.safety import redact_text, sanitize_error
from opspilot_api.knowledge.chunking import chunk_document
from opspilot_api.knowledge.embeddings import EmbeddingRouter
from opspilot_api.knowledge.extractors import extract_document
from opspilot_api.knowledge.models import (
    DocumentStatus,
    IngestionStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
    VersionStatus,
)
from opspilot_api.knowledge.service import storage_for

logger = logging.getLogger(__name__)


class IngestionCancelled(Exception):
    pass


def _publish(
    settings: Settings,
    document: KnowledgeDocument,
    job: KnowledgeIngestionJob,
    event_type: str,
) -> None:
    envelope = RealtimeEnvelope(
        event_id=uuid4(),
        type=event_type,
        workspace_id=document.workspace_id,
        incident_id=None,
        occurred_at=utcnow(),
        data={
            "document_id": str(document.id),
            "job_id": str(job.id),
            "status": job.status,
            "stage": job.current_stage,
            "chunks_processed": job.chunks_processed,
            "total_chunks": job.total_chunks,
        },
    )
    connection = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        connection.publish(
            f"opspilot:workspace:{document.workspace_id}:incidents",
            envelope.model_dump_json(),
        )
    except Exception as exc:
        logger.warning(
            "knowledge event publication failed", extra={"error_class": type(exc).__name__}
        )
    finally:
        connection.close()


def _load(
    session: Session, workspace_id: UUID, job_id: UUID
) -> tuple[KnowledgeIngestionJob, KnowledgeDocumentVersion, KnowledgeDocument]:
    job = session.scalar(
        select(KnowledgeIngestionJob).where(
            KnowledgeIngestionJob.id == job_id,
            KnowledgeIngestionJob.workspace_id == workspace_id,
        )
    )
    if job is None:
        raise LookupError("knowledge ingestion job not found in workspace")
    version = session.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.id == job.document_version_id,
            KnowledgeDocumentVersion.workspace_id == workspace_id,
        )
    )
    document = session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == job.document_id,
            KnowledgeDocument.workspace_id == workspace_id,
        )
    )
    if version is None or document is None:
        raise LookupError("knowledge version or document not found in workspace")
    return job, version, document


def _stage(
    session: Session,
    settings: Settings,
    job: KnowledgeIngestionJob,
    version: KnowledgeDocumentVersion,
    document: KnowledgeDocument,
    stage: str,
    version_status: VersionStatus,
) -> None:
    session.refresh(job)
    if job.status == IngestionStatus.CANCELLED.value:
        raise IngestionCancelled
    job.current_stage = stage
    job.updated_at = utcnow()
    version.status = version_status.value
    session.commit()
    _publish(settings, document, job, "knowledge.ingestion_progress")


def _cached_embeddings(
    session: Session,
    settings: Settings,
    workspace_id: UUID,
    hashes: list[str],
) -> dict[str, list[float]]:
    if not hashes:
        return {}
    rows = session.execute(
        select(KnowledgeChunk.content_hash, KnowledgeChunk.embedding).where(
            KnowledgeChunk.workspace_id == workspace_id,
            KnowledgeChunk.content_hash.in_(hashes),
            KnowledgeChunk.embedding_provider == settings.EMBEDDING_PROVIDER,
            KnowledgeChunk.embedding_model == settings.EMBEDDING_MODEL,
            KnowledgeChunk.embedding_dimensions == settings.EMBEDDING_DIMENSIONS,
        )
    )
    return {str(row[0]): list(row[1]) for row in rows}


def _fail(settings: Settings, workspace_id: UUID, job_id: UUID, exc: BaseException) -> None:
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            try:
                job, version, document = _load(session, workspace_id, job_id)
            except LookupError:
                return
            if job.status == IngestionStatus.CANCELLED.value:
                _publish(settings, document, job, "knowledge.ingestion_cancelled")
                return
            now = utcnow()
            job.status = IngestionStatus.FAILED.value
            job.current_stage = "failed"
            job.failed_at = now
            job.updated_at = now
            job.failure_code = getattr(exc, "code", type(exc).__name__.lower())
            job.failure_message = sanitize_error(exc)
            version.status = VersionStatus.FAILED.value
            version.failure_code = job.failure_code
            version.failure_message = job.failure_message
            if document.current_version_id is None:
                document.status = DocumentStatus.FAILED.value
            else:
                document.status = DocumentStatus.READY.value
            document.updated_at = now
            session.commit()
            _publish(settings, document, job, "knowledge.ingestion_failed")
    finally:
        engine.dispose()


def run_ingestion(settings: Settings, workspace_id: UUID, job_id: UUID) -> None:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            job, version, document = _load(session, workspace_id, job_id)
            if job.status == IngestionStatus.COMPLETED.value:
                return
            if job.status == IngestionStatus.CANCELLED.value:
                return
            job.status = IngestionStatus.RUNNING.value
            job.current_stage = "extracting"
            job.started_at = job.started_at or utcnow()
            job.updated_at = utcnow()
            document.status = DocumentStatus.PROCESSING.value
            session.commit()
            _publish(settings, document, job, "knowledge.ingestion_started")

            _stage(
                session,
                settings,
                job,
                version,
                document,
                "extracting",
                VersionStatus.EXTRACTING,
            )
            if version.storage_key is None:
                raise ValueError("knowledge version has no private storage object")
            content = storage_for(settings).get(version.storage_key)
            if hashlib.sha256(content).hexdigest() != version.content_hash:
                raise ValueError("stored content hash verification failed")
            extracted = extract_document(content, version.mime_type, settings)
            redacted_blocks = [
                block.model_copy(update={"content": redact_text(block.content)})
                for block in extracted.blocks
            ]
            extracted = extracted.model_copy(update={"blocks": redacted_blocks})
            extracted_text = "\n\n".join(block.content for block in redacted_blocks)
            version.extracted_text_hash = hashlib.sha256(extracted_text.encode()).hexdigest()
            version.page_count = extracted.page_count

            _stage(
                session,
                settings,
                job,
                version,
                document,
                "chunking",
                VersionStatus.CHUNKING,
            )
            drafts = chunk_document(extracted, settings)
            if not drafts:
                raise ValueError("chunking produced no meaningful content")
            total_tokens = sum(item.token_count for item in drafts)
            if total_tokens > settings.EMBEDDING_MAX_TOKENS_PER_JOB:
                raise ValueError("document exceeds embedding token limit")
            job.total_chunks = len(drafts)
            session.commit()

            _stage(
                session,
                settings,
                job,
                version,
                document,
                "embedding",
                VersionStatus.EMBEDDING,
            )
            cache = _cached_embeddings(
                session, settings, workspace_id, [item.content_hash for item in drafts]
            )
            missing = [item for item in drafts if item.content_hash not in cache]
            vectors = EmbeddingRouter(settings).embed([item.content for item in missing])
            cache.update(
                {item.content_hash: vector for item, vector in zip(missing, vectors, strict=True)}
            )

            session.execute(
                delete(KnowledgeChunk).where(
                    KnowledgeChunk.workspace_id == workspace_id,
                    KnowledgeChunk.document_version_id == version.id,
                )
            )
            for draft in drafts:
                session.add(
                    KnowledgeChunk(
                        id=uuid4(),
                        workspace_id=workspace_id,
                        document_id=document.id,
                        document_version_id=version.id,
                        service_id=document.service_id,
                        chunk_index=draft.chunk_index,
                        content=draft.content,
                        content_hash=draft.content_hash,
                        token_count=draft.token_count,
                        heading_path=draft.heading_path,
                        page_number=draft.page_number,
                        start_offset=draft.start_offset,
                        end_offset=draft.end_offset,
                        embedding=cache[draft.content_hash],
                        embedding_provider=settings.EMBEDDING_PROVIDER,
                        embedding_model=settings.EMBEDDING_MODEL,
                        embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
                        chunk_metadata=draft.metadata,
                    )
                )
            now = utcnow()
            version.embedding_provider = settings.EMBEDDING_PROVIDER
            version.embedding_model = settings.EMBEDDING_MODEL
            version.embedding_dimensions = settings.EMBEDDING_DIMENSIONS
            version.chunk_count = len(drafts)
            version.status = VersionStatus.READY.value
            version.processed_at = now
            version.failure_code = None
            version.failure_message = None
            document.current_version_id = version.id
            document.status = DocumentStatus.READY.value
            document.updated_at = now
            job.status = IngestionStatus.COMPLETED.value
            job.current_stage = "completed"
            job.chunks_processed = len(drafts)
            job.total_chunks = len(drafts)
            job.completed_at = now
            job.updated_at = now
            session.commit()
            _publish(settings, document, job, "knowledge.ingestion_completed")
    except IngestionCancelled:
        _fail(settings, workspace_id, job_id, IngestionCancelled("ingestion cancelled"))
    except Exception as exc:
        _fail(settings, workspace_id, job_id, exc)
        raise
    finally:
        engine.dispose()
