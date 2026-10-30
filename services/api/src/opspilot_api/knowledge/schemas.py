"""Typed API, ingestion, and retrieval contracts for operational knowledge."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opspilot_api.knowledge.models import (
    DocumentStatus,
    DocumentType,
    IngestionStatus,
    VersionStatus,
)


class RetrievalVariant(StrEnum):
    LEXICAL = "lexical"
    VECTOR = "vector"
    HYBRID_RRF = "hybrid_rrf"
    HYBRID_RRF_RERANK = "hybrid_rrf_rerank"
    # Experimental: disabled unless KNOWLEDGE_EXPERIMENTAL_QUERY_ENHANCEMENT=true.
    HYBRID_RRF_REWRITE = "hybrid_rrf_rewrite"
    HYBRID_RRF_HYDE = "hybrid_rrf_hyde"


BASELINE_VARIANTS = (
    RetrievalVariant.LEXICAL,
    RetrievalVariant.VECTOR,
    RetrievalVariant.HYBRID_RRF,
    RetrievalVariant.HYBRID_RRF_RERANK,
)
EXPERIMENTAL_VARIANTS = (RetrievalVariant.HYBRID_RRF_REWRITE, RetrievalVariant.HYBRID_RRF_HYDE)


class DocumentMetadata(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    document_type: DocumentType
    service_id: UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("title", "description")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized = sorted({value.strip().lower() for value in values if value.strip()})
        if any(len(value) > 50 for value in normalized):
            raise ValueError("tags must be at most 50 characters")
        return normalized


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class VersionResponse(ORMModel):
    id: UUID
    version_number: int
    original_filename: str | None
    mime_type: str
    content_hash: str
    byte_size: int
    page_count: int | None
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    chunk_count: int
    status: VersionStatus
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    processed_at: datetime | None


class JobResponse(ORMModel):
    id: UUID
    workspace_id: UUID
    document_id: UUID
    document_version_id: UUID
    status: IngestionStatus
    current_stage: str
    attempt: int
    chunks_processed: int
    total_chunks: int | None
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentSummary(ORMModel):
    id: UUID
    workspace_id: UUID
    service_id: UUID | None
    title: str
    slug: str
    description: str | None
    document_type: DocumentType
    status: DocumentStatus
    current_version_id: UUID | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    service_name: str | None = None
    current_version_number: int | None = None
    chunk_count: int = 0


class ChunkPreview(ORMModel):
    id: UUID
    chunk_index: int
    content: str
    heading_path: list[str]
    page_number: int | None
    token_count: int
    citation_label: str


class DocumentDetail(DocumentSummary):
    versions: list[VersionResponse]
    jobs: list[JobResponse]
    chunks: list[ChunkPreview]


class QueuedDocumentResponse(BaseModel):
    document_id: UUID
    version_id: UUID
    job_id: UUID
    status: Literal["queued"] = "queued"


class ActionResponse(BaseModel):
    document_id: UUID
    status: DocumentStatus


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    service_id: UUID | None = None
    document_types: list[DocumentType] = Field(default_factory=list, max_length=6)
    tags: list[str] = Field(default_factory=list, max_length=20)
    variant: RetrievalVariant = RetrievalVariant.HYBRID_RRF
    candidate_count: int = Field(default=20, ge=5, le=200)
    top_k: int = Field(default=5, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized

    @model_validator(mode="after")
    def candidate_bound(self) -> "SearchRequest":
        if self.top_k > self.candidate_count:
            raise ValueError("top_k must not exceed candidate_count")
        return self


class SearchResult(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    document_title: str
    document_type: DocumentType
    service_id: UUID | None
    service_name: str | None
    heading_path: list[str]
    page_number: int | None
    excerpt: str
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    citation_label: str


class QueryRewrite(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class HydePassage(BaseModel):
    passage: str = Field(min_length=1, max_length=1500)


class SearchDiagnostics(BaseModel):
    variant: RetrievalVariant
    experimental: bool = False
    candidate_count: int
    latency_ms: float
    embedding_latency_ms: float | None
    rerank_latency_ms: float | None
    rerank_fallback_used: bool
    low_relevance: bool


class SearchResponse(BaseModel):
    results: list[SearchResult]
    diagnostics: SearchDiagnostics


class ExtractedBlock(BaseModel):
    content: str
    heading_path: list[str] = Field(default_factory=list)
    page_number: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class ExtractedDocument(BaseModel):
    blocks: list[ExtractedBlock]
    page_count: int | None = None


class ChunkDraft(BaseModel):
    chunk_index: int
    content: str
    content_hash: str
    token_count: int
    heading_path: list[str]
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    metadata: dict[str, Any] = Field(default_factory=dict)
