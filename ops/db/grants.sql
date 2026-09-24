-- Least-privilege grants, applied after Alembic has created the catalogue.
-- Re-running this file is safe and is what picks up tables added by later
-- migrations.

-- ---------------------------------------------------------------- auth
-- The auth service is confined to its own schema.
GRANT CONNECT ON DATABASE {database} TO auth_svc;
GRANT USAGE, CREATE ON SCHEMA auth TO auth_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO auth_svc;
REVOKE ALL ON SCHEMA catalog FROM auth_svc;

-- ---------------------------------------------------------------- backend
GRANT CONNECT ON DATABASE {database} TO backend_svc;
GRANT USAGE ON SCHEMA catalog TO backend_svc;

GRANT SELECT, INSERT, UPDATE ON catalog.datasets TO backend_svc;
GRANT SELECT, INSERT, UPDATE ON catalog.publication_jobs TO backend_svc;
-- Read-only on versions: only the worker creates them. Rollback moves the
-- dataset pointer, it does not touch version rows.
GRANT SELECT ON catalog.dataset_versions TO backend_svc;
-- The backend produces `publication.requested` directly after committing the
-- job; it does not use the outbox.
REVOKE ALL ON catalog.outbox FROM backend_svc;

-- ---------------------------------------------------------------- worker
GRANT CONNECT ON DATABASE {database} TO worker_svc;
GRANT USAGE ON SCHEMA catalog TO worker_svc;

GRANT SELECT, UPDATE ON catalog.publication_jobs TO worker_svc;
GRANT SELECT, INSERT, UPDATE ON catalog.dataset_versions TO worker_svc;
-- Pointer + latest_seq updates; the worker never creates datasets.
GRANT SELECT, UPDATE ON catalog.datasets TO worker_svc;
GRANT SELECT, INSERT, UPDATE, DELETE ON catalog.outbox TO worker_svc;

-- Both services read the Alembic version table (harmless, and it keeps
-- `\dt` from erroring in a debug session).
GRANT SELECT ON catalog.alembic_version TO backend_svc, worker_svc;
