"""Persistence models for read-only GitHub repository, code, commit, and deployment data."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_api.incidents.models import Base, TimestampMixin

EMBEDDING_DIMENSIONS = 64

# Identifier punctuation and camelCase boundaries become word breaks so natural-language queries
# match code symbols; redaction preserves line counts so line-number citations stay exact.
CODE_SEARCH_EXPRESSION = (
    "to_tsvector('english', regexp_replace(regexp_replace("
    "coalesce(path, '') || ' ' || coalesce(symbol_name, '') || ' ' || coalesce(content, ''), "
    "'([a-z0-9])([A-Z])', '\\1 \\2', 'g'), '[_./-]', ' ', 'g'))"
)


class InstallationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class RepositoryStatus(StrEnum):
    ACTIVE = "active"
    SYNCING = "syncing"
    FAILED = "failed"
    REMOVED = "removed"
    ARCHIVED = "archived"


class SnapshotStatus(StrEnum):
    QUEUED = "queued"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


def _workspace_fk() -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )


def _repository_fk() -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )


class GitHubInstallation(TimestampMixin, Base):
    __tablename__ = "github_installations"
    __table_args__ = (
        Index("uq_github_installation_provider", "provider_installation_id", unique=True),
        Index("ix_github_installation_workspace", "workspace_id", "status"),
        CheckConstraint(
            "status IN ('active','suspended','removed')", name="ck_github_installation_status"
        ),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    provider_installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_login: Mapped[str] = mapped_column(String(120), nullable=False)
    account_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    repository_selection: Mapped[str] = mapped_column(String(20), nullable=False)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Last observed provider rate-limit headers; never contains credentials.
    rate_limit: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        # A provider repository belongs to exactly one workspace.
        Index("uq_repository_provider", "provider_repository_id", unique=True),
        Index("uq_repository_workspace_full_name", "workspace_id", "full_name", unique=True),
        Index("ix_repository_workspace_status", "workspace_id", "status"),
        Index("ix_repository_installation", "workspace_id", "github_installation_id"),
        CheckConstraint(
            "status IN ('active','syncing','failed','removed','archived')",
            name="ck_repository_status",
        ),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    github_installation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("github_installations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(320), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Only a fully ready snapshot may be referenced; switching it is the atomic activation point.
    active_snapshot_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "repository_snapshots.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_repository_active_snapshot",
        ),
    )
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ServiceRepositoryMapping(TimestampMixin, Base):
    __tablename__ = "service_repository_mappings"
    __table_args__ = (
        Index(
            "uq_service_repository_mapping",
            "workspace_id",
            "service_id",
            "repository_id",
            "environment",
            "branch_pattern",
            "path_prefix_key",
            unique=True,
        ),
        Index("ix_mapping_service", "workspace_id", "service_id"),
        Index("ix_mapping_repository", "workspace_id", "repository_id"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    service_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("monitored_services.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repository_id: Mapped[UUID] = _repository_fk()
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    branch_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    path_prefix: Mapped[str | None] = mapped_column(String(500))
    # Normalized (never NULL) copy so NULL prefixes participate in the uniqueness check.
    path_prefix_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")


class RepositorySnapshot(Base):
    __tablename__ = "repository_snapshots"
    __table_args__ = (
        Index("uq_snapshot_commit_branch", "repository_id", "commit_sha", "branch", unique=True),
        Index("ix_snapshot_repository_status", "workspace_id", "repository_id", "status"),
        CheckConstraint(
            "status IN ('queued','indexing','ready','failed','cancelled')",
            name="ck_snapshot_status",
        ),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    repository_id: Mapped[UUID] = _repository_fk()
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Skipped-file counts by reason, parse failures, and byte totals; no file contents.
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RepositoryFile(Base):
    __tablename__ = "repository_files"
    __table_args__ = (
        Index("uq_repository_file_snapshot_path", "snapshot_id", "path", unique=True),
        Index("ix_repository_file_scope", "workspace_id", "repository_id", "snapshot_id"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    repository_id: Mapped[UUID] = _repository_fk()
    snapshot_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("repository_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    language: Mapped[str | None] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_binary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CodeChunk(Base):
    __tablename__ = "code_chunks"
    __table_args__ = (
        Index("uq_code_chunk_snapshot_hash", "snapshot_id", "content_hash", unique=True),
        Index("ix_code_chunk_scope", "workspace_id", "repository_id", "snapshot_id"),
        Index("ix_code_chunk_file", "repository_file_id", "start_line"),
        Index(
            "ix_code_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_code_chunk_search_vector", "search_vector", postgresql_using="gin"),
        CheckConstraint("start_line >= 1 AND end_line >= start_line", name="ck_code_chunk_lines"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    repository_id: Mapped[UUID] = _repository_fk()
    snapshot_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("repository_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repository_file_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("repository_files.id", ondelete="RESTRICT"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    language: Mapped[str | None] = mapped_column(String(40))
    symbol_name: Mapped[str | None] = mapped_column(String(300))
    symbol_type: Mapped[str | None] = mapped_column(String(40))
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed(CODE_SEARCH_EXPRESSION, persisted=True), nullable=False
    )
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RepositoryCommit(TimestampMixin, Base):
    __tablename__ = "repository_commits"
    __table_args__ = (
        Index("uq_repository_commit_sha", "repository_id", "provider_commit_sha", unique=True),
        Index("ix_repository_commit_time", "workspace_id", "repository_id", "committed_at"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    repository_id: Mapped[UUID] = _repository_fk()
    provider_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(200))
    author_email_hash: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(2000), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parent_shas: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    changed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_url: Mapped[str] = mapped_column(String(500), nullable=False)


class CommitFileChange(Base):
    __tablename__ = "commit_file_changes"
    __table_args__ = (
        Index("uq_commit_file_change", "commit_id", "path", unique=True),
        Index("ix_commit_file_scope", "workspace_id", "repository_id", "commit_id"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    repository_id: Mapped[UUID] = _repository_fk()
    commit_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("repository_commits.id", ondelete="RESTRICT"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    previous_path: Mapped[str | None] = mapped_column(String(1000))
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patch_excerpt: Mapped[str | None] = mapped_column(Text)
    patch_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RepositoryPullRequest(TimestampMixin, Base):
    __tablename__ = "repository_pull_requests"
    __table_args__ = (
        Index("uq_repository_pull_request", "repository_id", "number", unique=True),
        Index("ix_repository_pull_request_scope", "workspace_id", "repository_id", "updated_at"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    repository_id: Mapped[UUID] = _repository_fk()
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    is_merged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    author_login: Mapped[str | None] = mapped_column(String(120))
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    provider_url: Mapped[str] = mapped_column(String(500), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RepositoryDeployment(TimestampMixin, Base):
    __tablename__ = "repository_deployments"
    __table_args__ = (
        Index("uq_repository_deployment", "repository_id", "provider_deployment_id", unique=True),
        Index("ix_repository_deployment_time", "workspace_id", "repository_id", "deployed_at"),
    )

    id: Mapped[UUID] = _uuid_pk()
    workspace_id: Mapped[UUID] = _workspace_fk()
    repository_id: Mapped[UUID] = _repository_fk()
    provider_deployment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubWebhookDelivery(Base):
    __tablename__ = "github_webhook_deliveries"
    __table_args__ = (
        Index("uq_github_delivery", "provider_delivery_id", unique=True),
        Index("ix_github_delivery_status", "status", "received_at"),
        CheckConstraint(
            "status IN ('received','processed','ignored','failed')",
            name="ck_github_delivery_status",
        ),
    )

    id: Mapped[UUID] = _uuid_pk()
    provider_delivery_id: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    action: Mapped[str | None] = mapped_column(String(60))
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    repository_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Selected normalized fields only; the raw payload is never stored.
    normalized: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))
