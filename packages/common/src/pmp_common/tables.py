"""SQLAlchemy Core table definitions for the ``catalog`` schema.

Shared by the async backend and the synchronous worker so the two cannot
disagree about column names or types. The tables are *described* here; they are
*created* by Alembic (``migrations/versions/``), which stays the single source
of truth for the schema — these definitions are deliberately not used for
autogeneration.

Core (not the ORM) is used throughout: every statement in this system is a
deliberate, reviewable query — row locks, partial-unique upserts, leases — and
an identity map would only get in the way.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from .db import CATALOG_SCHEMA

__all__ = [
    "catalog_metadata",
    "dataset_versions",
    "datasets",
    "outbox",
    "publication_jobs",
]

catalog_metadata = MetaData(schema=CATALOG_SCHEMA)

datasets = Table(
    "datasets",
    catalog_metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("slug", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("visibility", Text, nullable=False),
    Column("current_version_id", Uuid, nullable=True),
    Column("latest_seq", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

dataset_versions = Table(
    "dataset_versions",
    catalog_metadata,
    Column("id", Uuid, primary_key=True),
    Column("dataset_id", Uuid, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("object_key", Text, nullable=False),
    Column("source_key", Text, nullable=False),
    Column("source_etag", Text, nullable=True),
    Column("spec", JSONB, nullable=True),
    Column("pmtiles_header", JSONB, nullable=False),
    Column("job_id", Uuid, nullable=False),
    Column("status", Text, nullable=False),
    Column("worker_build", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

publication_jobs = Table(
    "publication_jobs",
    catalog_metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("dataset_id", Uuid, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("source_key", Text, nullable=False),
    Column("spec", JSONB, nullable=True),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("lease_owner", Text, nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("result_version_id", Uuid, nullable=True),
    Column("result", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("trace_id", Text, nullable=True),
    Column("requested_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

outbox = Table(
    "outbox",
    catalog_metadata,
    Column("id", Uuid, primary_key=True),
    Column("topic", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("headers", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
)
