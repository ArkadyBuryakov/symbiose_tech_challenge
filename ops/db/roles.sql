-- Per-service database roles (idempotent).
--
-- Each service connects with the least privilege it needs. Passwords are
-- applied separately by bootstrap.py so that secrets never appear in a SQL
-- file or in a container log.
--
-- On AWS these same roles exist in RDS and are granted `rds_iam`, so services
-- authenticate with short-lived IAM tokens instead of passwords. The only
-- application-side change is DB_AUTH=iam.

-- auth service: owns the `auth` schema (BetterAuth manages its own tables) and
-- must never be able to read the catalogue.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'auth_svc') THEN
        CREATE ROLE auth_svc LOGIN;
    END IF;
END $$;

-- backend: reads the catalogue, writes datasets and jobs, moves the dataset
-- pointer for explicit rollback. It may NOT insert version rows.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backend_svc') THEN
        CREATE ROLE backend_svc LOGIN;
    END IF;
END $$;

-- worker: the only writer of dataset_versions and of the outbox.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worker_svc') THEN
        CREATE ROLE worker_svc LOGIN;
    END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION auth_svc;
CREATE SCHEMA IF NOT EXISTS catalog;

-- Nobody creates objects in `public`.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
