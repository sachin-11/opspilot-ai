"""add operational knowledge base and hybrid retrieval

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True)),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("document_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("tags", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["monitored_services.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "document_type IN ('runbook','postmortem','architecture',"
            "'troubleshooting','policy','other')",
            name="ck_knowledge_document_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft','processing','ready','failed','archived')",
            name="ck_knowledge_document_status",
        ),
    )
    op.create_index(
        "uq_knowledge_document_workspace_slug",
        "knowledge_documents",
        ["workspace_id", "slug"],
        unique=True,
    )
    op.create_index(
        "ix_knowledge_document_workspace_status",
        "knowledge_documents",
        ["workspace_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_knowledge_document_service",
        "knowledge_documents",
        ["workspace_id", "service_id"],
    )
    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(255)),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("storage_key", sa.String(500)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("extracted_text_hash", sa.String(64)),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("embedding_provider", sa.String(50)),
        sa.Column("embedding_model", sa.String(120)),
        sa.Column("embedding_dimensions", sa.Integer()),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("failure_code", sa.String(80)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "version_number > 0 AND byte_size >= 0", name="ck_knowledge_version_values"
        ),
        sa.CheckConstraint(
            "status IN ('uploaded','extracting','chunking','embedding','ready','failed')",
            name="ck_knowledge_version_status",
        ),
    )
    op.create_index(
        "uq_knowledge_version_number",
        "knowledge_document_versions",
        ["document_id", "version_number"],
        unique=True,
    )
    op.create_index(
        "ix_knowledge_version_workspace_document",
        "knowledge_document_versions",
        ["workspace_id", "document_id"],
    )
    op.create_index(
        "ix_knowledge_version_content",
        "knowledge_document_versions",
        ["workspace_id", "content_hash", "status"],
    )
    op.create_foreign_key(
        "fk_knowledge_document_current_version",
        "knowledge_documents",
        "knowledge_document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True)),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("heading_path", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("start_offset", sa.Integer()),
        sa.Column("end_offset", sa.Integer()),
        sa.Column("embedding", Vector(64), nullable=False),
        sa.Column("embedding_provider", sa.String(50), nullable=False),
        sa.Column("embedding_model", sa.String(120), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(content, ''))", persisted=True),
            nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["knowledge_document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["service_id"], ["monitored_services.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "chunk_index >= 0 AND token_count > 0 AND embedding_dimensions = 64",
            name="ck_knowledge_chunk_values",
        ),
    )
    op.create_index(
        "uq_knowledge_chunk_version_index",
        "knowledge_chunks",
        ["document_version_id", "chunk_index"],
        unique=True,
    )
    op.create_index(
        "ix_knowledge_chunk_scope",
        "knowledge_chunks",
        ["workspace_id", "document_id", "document_version_id"],
    )
    op.create_index(
        "ix_knowledge_chunk_service", "knowledge_chunks", ["workspace_id", "service_id"]
    )
    op.create_index(
        "ix_knowledge_chunk_search_vector",
        "knowledge_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_chunk_embedding_hnsw",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "knowledge_ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_stage", sa.String(50), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("chunks_processed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_chunks", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(80)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["knowledge_document_versions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_knowledge_job_status",
        ),
    )
    op.create_index(
        "ix_knowledge_job_workspace_status",
        "knowledge_ingestion_jobs",
        ["workspace_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_knowledge_job_version",
        "knowledge_ingestion_jobs",
        ["document_version_id", "created_at"],
    )
    op.create_table(
        "retrieval_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("query", sa.String(1000), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("retrieval_variant", sa.String(50), nullable=False),
        sa.Column("filters", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("result_chunk_ids", postgresql.JSONB(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("embedding_latency_ms", sa.Float()),
        sa.Column("rerank_latency_ms", sa.Float()),
        sa.Column("rerank_fallback_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_retrieval_log_workspace_created", "retrieval_logs", ["workspace_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("retrieval_logs")
    op.drop_table("knowledge_ingestion_jobs")
    op.drop_table("knowledge_chunks")
    op.drop_constraint(
        "fk_knowledge_document_current_version", "knowledge_documents", type_="foreignkey"
    )
    op.drop_table("knowledge_document_versions")
    op.drop_table("knowledge_documents")
