"""A version is identified by its bytes *and* its spec.

Adds ``dataset_versions.spec_sha256`` and makes the live-uniqueness index cover
``(dataset_id, sha256, spec_sha256)``, so republishing identical bytes with a
changed layer/style spec creates a new version instead of being deduplicated
(which silently discarded the new spec).

Existing rows are backfilled with the same canonical digest the worker uses
(``pmp_common.versioning.spec_digest``), so old and new rows compare correctly.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pmp_common.versioning import spec_digest

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.add_column(
        "dataset_versions",
        sa.Column(
            "spec_sha256",
            sa.Text(),
            nullable=True,
            comment="SHA-256 of the canonical JSON spec; part of the version's identity.",
        ),
        schema=SCHEMA,
    )

    # Backfill in Python, not SQL: the digest must be byte-for-byte the one the
    # worker computes, and Postgres's jsonb text form is not canonical JSON.
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"SELECT id, spec FROM {SCHEMA}.dataset_versions")).all()
    for row in rows:
        conn.execute(
            sa.text(f"UPDATE {SCHEMA}.dataset_versions SET spec_sha256 = :d WHERE id = :id"),
            {"d": spec_digest(row.spec), "id": row.id},
        )

    op.alter_column("dataset_versions", "spec_sha256", nullable=False, schema=SCHEMA)
    op.create_check_constraint(
        "versions_spec_sha256_check",
        "dataset_versions",
        "spec_sha256 ~ '^[0-9a-f]{64}$'",
        schema=SCHEMA,
    )

    op.drop_index("versions_dataset_sha_live_key", table_name="dataset_versions", schema=SCHEMA)
    op.create_index(
        "versions_dataset_key_live_key",
        "dataset_versions",
        ["dataset_id", "sha256", "spec_sha256"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status <> 'RETIRED'"),
    )


def downgrade() -> None:
    # Only safe while no dataset has two live versions sharing bytes.
    op.drop_index("versions_dataset_key_live_key", table_name="dataset_versions", schema=SCHEMA)
    op.create_index(
        "versions_dataset_sha_live_key",
        "dataset_versions",
        ["dataset_id", "sha256"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status <> 'RETIRED'"),
    )
    op.drop_constraint("versions_spec_sha256_check", "dataset_versions", schema=SCHEMA)
    op.drop_column("dataset_versions", "spec_sha256", schema=SCHEMA)
