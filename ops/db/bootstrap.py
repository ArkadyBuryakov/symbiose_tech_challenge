"""One-shot database bootstrap: roles, schemas, migrations, grants.

Run by the ``migrate`` compose service before any application container starts
(and, on AWS, by a Helm pre-upgrade Job). It is idempotent, so ``make up`` on an
existing volume is a no-op.

Order matters:

1. ``roles.sql``  - roles and schemas must exist before anything references them.
2. role auth     - passwords applied with a parameterised statement so they
   never appear in a log line or a SQL file, or, on RDS, ``rds_iam``.
3. ``alembic upgrade head`` - creates/updates the ``catalog`` tables, running as
   the privileged bootstrap role.
4. ``grants.sql`` - least-privilege grants, applied last because they reference
   tables the migration just created. Re-running picks up new tables.

The BetterAuth tables in the ``auth`` schema are migrated separately by the
``migrate-auth`` container: they are owned by the Node service and its CLI is
the only thing that knows their shape.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from pmp_common.config import DbSettings
from pmp_common.logging import configure_logging, get_logger

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

log = get_logger("migrate")

# role name -> environment variable holding its password
SERVICE_ROLES = {
    "auth_svc": "AUTH_DB_PASSWORD",
    "backend_svc": "BACKEND_DB_PASSWORD",
    "worker_svc": "WORKER_DB_PASSWORD",
}


def _connect(settings: DbSettings) -> psycopg.Connection:
    return psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.name,
        user=settings.user,
        password=settings.password,
        connect_timeout=settings.connect_timeout,
        autocommit=True,
    )


def apply_roles_and_schemas(conn: psycopg.Connection) -> None:
    conn.execute((HERE / "roles.sql").read_text())
    log.info("migrate.roles_applied", roles=sorted(SERVICE_ROLES))


def apply_role_auth(conn: psycopg.Connection) -> None:
    """Let each service role log in: a password from the environment, or IAM.

    ``SERVICE_DB_AUTH=iam`` (RDS) grants ``rds_iam`` instead, so the roles
    authenticate with IAM tokens and have no password at all. It is separate
    from ``DB_AUTH`` because this job itself logs in as the master user, whose
    password RDS keeps in Secrets Manager.
    """
    if os.environ.get("SERVICE_DB_AUTH", "password") == "iam":
        for role in SERVICE_ROLES:
            conn.execute(sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(role)))
        log.info("migrate.iam_granted", roles=sorted(SERVICE_ROLES))
        return

    for role, env_var in SERVICE_ROLES.items():
        password = os.environ.get(env_var)
        if not password:
            raise SystemExit(f"{env_var} must be set so role {role} can log in")
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    log.info("migrate.passwords_applied", count=len(SERVICE_ROLES))


def run_alembic() -> None:
    log.info("migrate.alembic_start")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"alembic upgrade failed with exit code {result.returncode}")
    log.info("migrate.alembic_done")


def apply_grants(conn: psycopg.Connection, database: str) -> None:
    statements = (
        (HERE / "grants.sql").read_text().format(database=sql.Identifier(database).as_string(conn))
    )
    conn.execute(statements)
    log.info("migrate.grants_applied")


def main() -> None:
    configure_logging(
        service="migrate",
        level=os.environ.get("LOG_LEVEL", "INFO"),
        fmt=os.environ.get("LOG_FORMAT", "json"),
    )
    settings = DbSettings()

    with _connect(settings) as conn:
        apply_roles_and_schemas(conn)
        apply_role_auth(conn)

    run_alembic()

    with _connect(settings) as conn:
        apply_grants(conn, settings.name)

    log.info("migrate.complete", database=settings.name)


if __name__ == "__main__":
    main()
