"""Pydantic API shapes for the read-only GitHub integration."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opspilot_api.incidents.models import Environment
from opspilot_api.repositories.models import (
    InstallationStatus,
    RepositoryStatus,
    SnapshotStatus,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CodeVariant(StrEnum):
    LEXICAL = "lexical"
    VECTOR = "vector"
    HYBRID_RRF = "hybrid_rrf"
    HYBRID_RRF_RERANK = "hybrid_rrf_rerank"


class InstallationSyncRequest(BaseModel):
    provider_installation_id: int = Field(ge=1)


class InstallationResponse(ORMModel):
    id: UUID
    workspace_id: UUID
    provider_installation_id: int
    account_login: str
    account_type: str
    status: InstallationStatus
    repository_selection: str
    permissions: dict[str, Any]
    rate_limit: dict[str, Any]
    installed_at: datetime
    suspended_at: datetime | None
    removed_at: datetime | None
    updated_at: datetime
    repository_count: int = 0
    last_synced_at: datetime | None = None


class InstallationListResponse(BaseModel):
    provider: str
    provider_configured: bool
    configuration_hint: str | None
    installations: list[InstallationResponse]


class SnapshotSummary(ORMModel):
    id: UUID
    commit_sha: str
    branch: str
    status: SnapshotStatus
    file_count: int
    chunk_count: int
    stats: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime


class MappedServiceSummary(BaseModel):
    mapping_id: UUID
    service_id: UUID
    service_name: str
    environment: Environment
    branch_pattern: str
    path_prefix: str | None


class RepositorySummary(ORMModel):
    id: UUID
    workspace_id: UUID
    github_installation_id: UUID
    provider_repository_id: int
    owner: str
    name: str
    full_name: str
    default_branch: str
    is_private: bool
    is_archived: bool
    status: RepositoryStatus
    failure_code: str | None
    failure_message: str | None
    last_synced_at: datetime | None
    updated_at: datetime
    active_snapshot: SnapshotSummary | None = None
    connected_services: list[MappedServiceSummary] = Field(default_factory=list)


class RepositoryDetail(RepositorySummary):
    recent_snapshots: list[SnapshotSummary] = Field(default_factory=list)
    commit_count: int = 0
    deployment_count: int = 0


class QueuedResponse(BaseModel):
    status: str
    detail: str | None = None
    installation_id: UUID | None = None
    repository_id: UUID | None = None
    snapshot_id: UUID | None = None


class MappingCreate(BaseModel):
    repository_id: UUID
    environment: Environment
    branch_pattern: str = Field(default="main", min_length=1, max_length=255)
    path_prefix: str | None = Field(default=None, max_length=500)

    @field_validator("branch_pattern")
    @classmethod
    def _branch(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char in value for char in ("\\", " ", "\n", "\x00")) or ".." in value:
            raise ValueError("branch_pattern must be a branch name or glob")
        return value

    @field_validator("path_prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().lstrip("/")
        if "\\" in value or any(part in {"..", "."} for part in value.split("/")):
            raise ValueError("path_prefix must be a relative repository path")
        return value if value.endswith("/") else value + "/"


class MappingResponse(ORMModel):
    id: UUID
    workspace_id: UUID
    service_id: UUID
    repository_id: UUID
    repository_full_name: str
    environment: Environment
    branch_pattern: str
    path_prefix: str | None
    created_at: datetime
    updated_at: datetime


class CodeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    repository_id: UUID | None = None
    service_id: UUID | None = None
    environment: Environment | None = None
    branch: str | None = Field(default=None, max_length=255)
    path_prefix: str | None = Field(default=None, max_length=500)
    language: str | None = Field(default=None, max_length=40)
    symbol_type: str | None = Field(default=None, max_length=40)
    variant: CodeVariant = CodeVariant.HYBRID_RRF
    candidate_count: int = Field(default=20, ge=5, le=100)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized

    @model_validator(mode="after")
    def _bounds(self) -> "CodeSearchRequest":
        if self.top_k > self.candidate_count:
            raise ValueError("top_k must not exceed candidate_count")
        return self


class CodeSearchResult(BaseModel):
    chunk_id: UUID
    repository_id: UUID
    repository_full_name: str
    snapshot_id: UUID
    commit_sha: str
    branch: str
    path: str
    language: str | None
    symbol_name: str | None
    symbol_type: str | None
    start_line: int
    end_line: int
    excerpt: str
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    citation_label: str


class CodeSearchDiagnostics(BaseModel):
    variant: CodeVariant
    candidate_count: int
    latency_ms: float
    embedding_latency_ms: float | None
    rerank_latency_ms: float | None
    rerank_fallback_used: bool
    low_relevance: bool


class CodeSearchResponse(BaseModel):
    results: list[CodeSearchResult]
    diagnostics: CodeSearchDiagnostics


class CommitFileSummary(BaseModel):
    path: str
    previous_path: str | None
    change_type: str
    additions: int
    deletions: int


class CommitResponse(BaseModel):
    id: UUID
    repository_id: UUID
    repository_full_name: str
    sha: str
    author_name: str | None
    message: str
    committed_at: datetime
    changed_files: int
    additions: int
    deletions: int
    provider_url: str
    files: list[CommitFileSummary] = Field(default_factory=list)


class DeploymentResponse(BaseModel):
    id: UUID
    repository_id: UUID
    repository_full_name: str
    provider_deployment_id: int
    commit_sha: str
    environment: str
    status: str
    description: str | None
    deployed_at: datetime | None
    completed_at: datetime | None


class SuspectChange(BaseModel):
    kind: str  # commit | deployment
    repository_id: UUID
    repository_full_name: str
    commit_sha: str
    commit_id: UUID | None
    deployment_id: UUID | None
    title: str
    score: float
    confidence: str  # high | medium | low
    matched_signals: list[str]
    contradicting_signals: list[str]
    minutes_before_incident: int
    changed_files: list[str]
    environment: str | None
    citation_label: str


class SuspectChangesResponse(BaseModel):
    incident_id: UUID
    opened_at: datetime
    service_id: UUID | None
    has_repository_mapping: bool
    candidates: list[SuspectChange]
    deployments: list[DeploymentResponse]
    code_excerpts: list[CodeSearchResult]
    notice: str
