"""Persistence models for observability sources, signal receipts, dead letters, and query audits."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_api.incidents.models import Base, TimestampMixin


class SourceProvider(StrEnum):
    GENERIC_WEBHOOK = "generic_webhook"
    AWS_CLOUDWATCH = "aws_cloudwatch"
    DEMO_FIXTURE = "demo_fixture"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    FAILING = "failing"


class ReceiptStatus(StrEnum):
    RECEIVED = "received"
    PUBLISHED = "published"
    NORMALIZED = "normalized"
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    DEAD_LETTERED = "dead_lettered"
    FAILED = "failed"


def _workspace_fk() -> Mapped[UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )


class ObservabilitySource(TimestampMixin, Base):
    __tablename__ = "observability_sources"
    __table_args__ = (
        Index("uq_observability_source_public_id", "public_id", unique=True),
        Index("uq_observability_source_name", "workspace_id", "name", unique=True),
        Index("ix_observability_source_workspace", "workspace_id", "status"),
        Index("ix_observability_source_service", "workspace_id", "service_id"),
        CheckConstraint(
            "provider IN ('generic_webhook','aws_cloudwatch','demo_fixture')",
            name="ck_observability_source_provider",
        ),
        CheckConstraint(
            "status IN ('active','disabled','failing')", name="ck_observability_source_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = _workspace_fk()
    service_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("monitored_services.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Unguessable identifier used in the public webhook URL (not the primary key).
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    region: Mapped[str | None] = mapped_column(String(40))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Encrypted webhook secret ("enc:v1:...") or an external reference; never plaintext.
    secret_reference: Mapped[str | None] = mapped_column(String(600))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignalReceipt(Base):
    __tablename__ = "signal_receipts"
    __table_args__ = (
        Index("uq_signal_receipt_signal_id", "signal_id", unique=True),
        Index("uq_signal_receipt_ingress", "workspace_id", "source_id", "ingress_key", unique=True),
        Index(
            "uq_signal_receipt_provider_event",
            "source_id",
            "provider_event_id",
            unique=True,
            postgresql_where=text("provider_event_id IS NOT NULL"),
        ),
        Index("ix_signal_receipt_dedup", "workspace_id", "deduplication_key", "status"),
        Index("ix_signal_receipt_feed", "workspace_id", "received_at", "id"),
        Index("ix_signal_receipt_source", "workspace_id", "source_id", "received_at"),
        Index("ix_signal_receipt_service", "workspace_id", "service_id", "received_at"),
        Index("ix_signal_receipt_status", "workspace_id", "status", "received_at"),
        CheckConstraint(
            "status IN ('received','published','normalized','processed','duplicate',"
            "'dead_lettered','failed')",
            name="ck_signal_receipt_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = _workspace_fk()
    service_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("monitored_services.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("observability_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    signal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(200))
    # Accepted-delivery identity (event ID, idempotency key, or payload hash) used at ingress.
    ingress_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Final deduplication key assigned at normalization (initially equals ingress_key).
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signal_type: Mapped[str | None] = mapped_column(String(30))
    severity: Mapped[str | None] = mapped_column(String(4))
    title: Mapped[str | None] = mapped_column(String(300))
    normalized: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incident_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="SET NULL")
    )
    ingest_result: Mapped[str | None] = mapped_column(String(20))
    duplicate_of: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(String(500))


class DeadLetterSignal(Base):
    __tablename__ = "dead_letter_signals"
    __table_args__ = (
        Index("ix_dead_letter_feed", "workspace_id", "created_at", "id"),
        Index(
            "uq_dead_letter_origin",
            "original_topic",
            "partition",
            "offset",
            "payload_hash",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT")
    )
    source_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("observability_sources.id", ondelete="RESTRICT")
    )
    signal_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    original_topic: Mapped[str] = mapped_column(String(200), nullable=False)
    partition: Mapped[int | None] = mapped_column(Integer)
    offset: Mapped[int | None] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str | None] = mapped_column(String(20))
    failure_code: Mapped[str] = mapped_column(String(80), nullable=False)
    failure_message: Mapped[str] = mapped_column(String(500), nullable=False)
    # Redacted and size-bounded; null when too large (then the message is not replayable).
    sanitized_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DeadLetterReplay(Base):
    """Append-only audit trail: one row per replay request."""

    __tablename__ = "dead_letter_replays"
    __table_args__ = (Index("ix_dead_letter_replay_letter", "dead_letter_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = _workspace_fk()
    dead_letter_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("dead_letter_signals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    target_topic: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CloudWatchQueryAudit(Base):
    __tablename__ = "cloudwatch_query_audits"
    __table_args__ = (
        Index("ix_cloudwatch_audit_source", "workspace_id", "source_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = _workspace_fk()
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("observability_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    investigation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("investigations.id", ondelete="SET NULL")
    )
    query_type: Mapped[str] = mapped_column(String(30), nullable=False)
    log_group_hash: Mapped[str | None] = mapped_column(String(64))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
