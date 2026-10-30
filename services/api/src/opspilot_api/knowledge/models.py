"""Persistence models for versioned operational knowledge."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_api.incidents.models import Base, TimestampMixin

EMBEDDING_DIMENSIONS = 64


class DocumentType(StrEnum):
    RUNBOOK = "runbook"
    POSTMORTEM = "postmortem"
    ARCHITECTURE = "architecture"
    TROUBLESHOOTING = "troubleshooting"
    POLICY = "policy"
    OTHER = "other"


class DocumentStatus(StrEnum):
    DRAFT = "draft"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"


class VersionStatus(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class IngestionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        Index("uq_knowledge_document_workspace_slug", "workspace_id", "slug", unique=True),
        Index("ix_knowledge_document_workspace_status", "workspace_id", "status", "updated_at"),
        Index("ix_knowledge_document_service", "workspace_id", "service_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("monitored_services.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    document_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "knowledge_document_versions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_knowledge_document_current_version",
        ),
    )
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeDocumentVersion(Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        Index(
            "uq_knowledge_version_number",
            "document_id",
            "version_number",
            unique=True,
        ),
        Index("ix_knowledge_version_workspace_document", "workspace_id", "document_id"),
        Index("ix_knowledge_version_content", "workspace_id", "content_hash", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_text_hash: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    embedding_provider: Mapped[str | None] = mapped_column(String(50))
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index(
            "uq_knowledge_chunk_version_index",
            "document_version_id",
            "chunk_index",
            unique=True,
        ),
        Index("ix_knowledge_chunk_scope", "workspace_id", "document_id", "document_version_id"),
        Index("ix_knowledge_chunk_service", "workspace_id", "service_id"),
        Index(
            "ix_knowledge_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_knowledge_chunk_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_document_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    service_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("monitored_services.id", ondelete="RESTRICT")
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    page_number: Mapped[int | None] = mapped_column(Integer)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(content, ''))", persisted=True),
        nullable=False,
    )
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeIngestionJob(TimestampMixin, Base):
    __tablename__ = "knowledge_ingestion_jobs"
    __table_args__ = (
        Index("ix_knowledge_job_workspace_status", "workspace_id", "status", "updated_at"),
        Index("ix_knowledge_job_version", "document_version_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_document_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(50), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    chunks_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_chunks: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))


class RetrievalLog(Base):
    __tablename__ = "retrieval_logs"
    __table_args__ = (Index("ix_retrieval_log_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("investigations.id", ondelete="SET NULL")
    )
    query: Mapped[str] = mapped_column(String(1000), nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_variant: Mapped[str] = mapped_column(String(50), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_chunk_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    embedding_latency_ms: Mapped[float | None] = mapped_column(Float)
    rerank_latency_ms: Mapped[float | None] = mapped_column(Float)
    rerank_fallback_used: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
