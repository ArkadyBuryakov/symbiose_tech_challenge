"""Initial catalogue schema: datasets, versions, publication jobs, outbox.

Revision ID: 0001
Revises:
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # Enum-like columns are plain text with CHECK constraints rather than
    # Postgres ENUM types: adding a value to a native enum needs a DDL lock and
    # cannot be done inside a transaction on older versions, while a CHECK is a
    # cheap, reversible migration.
    op.create_table(
        "datasets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False, server_default="public"),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "latest_seq",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Highest sequence ever allocated; never decreases, never reused.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("visibility IN ('public','private')", name="datasets_visibility_check"),
        sa.CheckConstraint("latest_seq >= 0", name="datasets_latest_seq_check"),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$'", name="datasets_slug_check"),
        sa.UniqueConstraint("tenant_id", "slug", name="datasets_tenant_slug_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "datasets_tenant_created_idx",
        "datasets",
        ["tenant_id", sa.text("created_at DESC")],
        schema=SCHEMA,
    )
    # Anonymous listing of public datasets is a hot, unfiltered-by-tenant query.
    op.create_index(
        "datasets_public_idx",
        "datasets",
        ["visibility"],
        schema=SCHEMA,
        postgresql_where=sa.text("visibility = 'public'"),
    )

    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("spec_sha256", sa.Text(), nullable=False,
                  comment="SHA-256 of the canonical JSON spec; part of the version's identity."),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False,
                  comment="Immutable content-addressed key in the publish bucket."),
        sa.Column("source_key", sa.Text(), nullable=False,
                  comment="Staging key this version was published from (provenance)."),
        sa.Column("source_etag", sa.Text(), nullable=True),
        sa.Column("spec", postgresql.JSONB(), nullable=True,
                  comment="Layer/style spec captured with this version."),
        sa.Column("pmtiles_header", postgresql.JSONB(), nullable=False,
                  comment="Bounds, center and zoom range parsed from the archive header."),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="AVAILABLE"),
        sa.Column("worker_build", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('AVAILABLE','RETIRED')", name="versions_status_check"),
        sa.CheckConstraint("seq >= 1", name="versions_seq_check"),
        sa.CheckConstraint("size_bytes >= 0", name="versions_size_check"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="versions_sha256_check"),
        sa.CheckConstraint("spec_sha256 ~ '^[0-9a-f]{64}$'", name="versions_spec_sha256_check"),
        sa.ForeignKeyConstraint(
            ["dataset_id"], [f"{SCHEMA}.datasets.id"],
            name="versions_dataset_fk", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dataset_id", "seq", name="versions_dataset_seq_key"),
        schema=SCHEMA,
    )
    # One live version row per (bytes, spec) per dataset. This is what makes a
    # redelivered Kafka message (or a concurrent duplicate) unable to create a
    # second version for the same content: the second insert violates the index.
    op.create_index(
        "versions_dataset_key_live_key",
        "dataset_versions",
        ["dataset_id", "sha256", "spec_sha256"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status <> 'RETIRED'"),
    )
    op.create_index(
        "versions_dataset_seq_desc_idx",
        "dataset_versions",
        ["dataset_id", sa.text("seq DESC")],
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "datasets_current_version_fk",
        "datasets",
        "dataset_versions",
        ["current_version_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "publication_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_version_id", sa.Uuid(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')", name="jobs_status_check"
        ),
        sa.CheckConstraint(
            "result IS NULL OR result IN ('CREATED','DEDUPLICATED','POINTER_MOVED')",
            name="jobs_result_check",
        ),
        sa.CheckConstraint("attempts >= 0", name="jobs_attempts_check"),
        sa.ForeignKeyConstraint(
            ["dataset_id"], [f"{SCHEMA}.datasets.id"],
            name="jobs_dataset_fk", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["result_version_id"], [f"{SCHEMA}.dataset_versions.id"],
            name="jobs_version_fk", ondelete="SET NULL",
        ),
        # The API contract for POST /publications: replaying a request with the
        # same Idempotency-Key returns the original job instead of creating one.
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="jobs_tenant_idempotency_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "jobs_dataset_created_idx",
        "publication_jobs",
        ["dataset_id", sa.text("created_at DESC")],
        schema=SCHEMA,
    )
    # Drives the reconciler: find PENDING jobs that were never produced and
    # RUNNING jobs whose worker died and left the lease to expire.
    op.create_index(
        "jobs_recoverable_idx",
        "publication_jobs",
        ["status", "updated_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('PENDING','RUNNING')"),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("headers", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    # The relay only ever scans unsent rows; a partial index keeps that scan
    # small no matter how much history accumulates.
    op.create_index(
        "outbox_unsent_idx",
        "outbox",
        ["created_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("sent_at IS NULL"),
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.touch_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("datasets", "publication_jobs"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_touch_updated_at
            BEFORE UPDATE ON {SCHEMA}.{table}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.touch_updated_at();
            """
        )


def downgrade() -> None:
    for table in ("datasets", "publication_jobs"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_touch_updated_at ON {SCHEMA}.{table}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.touch_updated_at()")
    op.drop_constraint("datasets_current_version_fk", "datasets", schema=SCHEMA, type_="foreignkey")
    op.drop_table("outbox", schema=SCHEMA)
    op.drop_table("publication_jobs", schema=SCHEMA)
    op.drop_table("dataset_versions", schema=SCHEMA)
    op.drop_table("datasets", schema=SCHEMA)
