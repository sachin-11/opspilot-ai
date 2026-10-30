"""add multi-tenant incident management core

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"], unique=True)
    op.create_table(
        "monitored_services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_service_workspace_slug"),
        sa.CheckConstraint(
            "environment IN ('development','staging','production')", name="ck_service_environment"
        ),
    )
    op.create_index("ix_services_workspace", "monitored_services", ["workspace_id"])
    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True)),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("severity", sa.String(4), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("mitigated_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["monitored_services.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "severity IN ('sev1','sev2','sev3','sev4')", name="ck_incident_severity"
        ),
        sa.CheckConstraint(
            "status IN ('open','investigating','mitigated','resolved')", name="ck_incident_status"
        ),
        sa.CheckConstraint("source IN ('manual','event_ingestion')", name="ck_incident_source"),
        sa.CheckConstraint("version > 0", name="ck_incident_version"),
    )
    op.create_index(
        "ix_incidents_workspace_status_created",
        "incidents",
        ["workspace_id", "status", "created_at"],
    )
    op.create_index("ix_incidents_workspace_severity", "incidents", ["workspace_id", "severity"])
    op.create_index(
        "ix_incidents_unresolved_fingerprint",
        "incidents",
        ["workspace_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("fingerprint IS NOT NULL AND status <> 'resolved'"),
    )
    op.create_table(
        "incident_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(120)),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "event_type IN ('incident_created','alert_received','status_changed',"
            "'severity_changed','note_added','incident_reopened')",
            name="ck_timeline_event_type",
        ),
        sa.CheckConstraint(
            "actor_type IN ('user','system','integration')", name="ck_timeline_actor_type"
        ),
    )
    op.create_index(
        "ix_timeline_incident_occurred", "incident_events", ["incident_id", "occurred_at"]
    )
    op.create_table(
        "ingested_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("external_id", sa.String(200)),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(4), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("error_signature", sa.String(300)),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["monitored_services.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_event_workspace_idempotency"
        ),
        sa.CheckConstraint(
            "severity IN ('sev1','sev2','sev3','sev4')", name="ck_ingested_severity"
        ),
    )
    op.create_index(
        "ix_ingested_workspace_idempotency", "ingested_events", ["workspace_id", "idempotency_key"]
    )


def downgrade() -> None:
    op.drop_table("ingested_events")
    op.drop_table("incident_events")
    op.drop_table("incidents")
    op.drop_table("monitored_services")
    op.drop_table("workspaces")
