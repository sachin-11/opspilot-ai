"""add durable AI investigation engine

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_timeline_event_type", "incident_events", type_="check")
    op.create_check_constraint(
        "ck_timeline_event_type",
        "incident_events",
        "event_type IN ('incident_created','alert_received','status_changed','severity_changed',"
        "'note_added','incident_reopened','investigation_completed','investigation_rejected')",
    )
    op.create_table(
        "investigations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("retry_of_id", postgresql.UUID(as_uuid=True)),
        sa.Column("graph_thread_id", sa.String(200), nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("prompt_version", sa.String(30), nullable=False),
        sa.Column("primary_provider", sa.String(50), nullable=False),
        sa.Column("primary_model", sa.String(120), nullable=False),
        sa.Column("fallback_provider", sa.String(50)),
        sa.Column("fallback_model", sa.String(120)),
        sa.Column("title", sa.String(240)),
        sa.Column("executive_summary", sa.Text()),
        sa.Column("root_cause", sa.Text()),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("remediation_plan", postgresql.JSONB()),
        sa.Column("verification_plan", postgresql.JSONB()),
        sa.Column("total_input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6)),
        sa.Column("revision_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("review_requested_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(80)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retry_of_id"], ["investigations.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('queued','running','awaiting_review','completed',"
            "'rejected','failed','cancelled')",
            name="ck_investigation_status",
        ),
        sa.CheckConstraint(
            "trigger IN ('manual','incident_created','incident_updated')",
            name="ck_investigation_trigger",
        ),
        sa.CheckConstraint("version > 0 AND revision_count >= 0", name="ck_investigation_versions"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_investigation_confidence",
        ),
    )
    op.create_index(
        "ix_investigations_workspace_incident",
        "investigations",
        ["workspace_id", "incident_id", "created_at"],
    )
    op.create_index(
        "uq_investigations_one_active_incident",
        "investigations",
        ["workspace_id", "incident_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running','awaiting_review')"),
    )
    op.create_table(
        "investigation_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_name", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("summary", sa.String(1000)),
        sa.Column("structured_output", postgresql.JSONB()),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "investigation_id", "node_name", "attempt", name="uq_investigation_step_attempt"
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','skipped')",
            name="ck_investigation_step_status",
        ),
    )
    op.create_index(
        "ix_investigation_steps_order", "investigation_steps", ["investigation_id", "created_at"]
    )
    op.create_table(
        "evidence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_type", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(200)),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("excerpt", sa.String(2000), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "investigation_id", "content_hash", name="uq_evidence_investigation_hash"
        ),
    )
    op.create_index(
        "ix_evidence_investigation_collected",
        "evidence_items",
        ["investigation_id", "collected_at"],
    )
    op.create_table(
        "investigation_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("comment", sa.String(2000)),
        sa.Column("actor_id", sa.String(120)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "action IN ('approved','revision_requested','rejected')",
            name="ck_investigation_review_action",
        ),
    )
    op.create_index(
        "ix_reviews_investigation_created",
        "investigation_reviews",
        ["investigation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("investigation_reviews")
    op.drop_table("evidence_items")
    op.drop_table("investigation_steps")
    op.drop_table("investigations")
    op.drop_constraint("ck_timeline_event_type", "incident_events", type_="check")
    op.create_check_constraint(
        "ck_timeline_event_type",
        "incident_events",
        "event_type IN ('incident_created','alert_received','status_changed','severity_changed',"
        "'note_added','incident_reopened')",
    )
