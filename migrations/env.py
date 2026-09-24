"""Alembic environment for the ``catalog`` schema.

Migrations run as a one-shot container before any service starts (locally the
``migrate`` compose service; on AWS a Helm pre-upgrade Job). They connect with a
privileged role; the per-service roles only receive the grants applied
afterwards by ``ops/db/grants.sql``.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from pmp_common.config import DbSettings
from pmp_common.db import CATALOG_SCHEMA

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # migrations are hand-written; no autogenerate


def _url() -> str:
    settings = DbSettings()
    from pmp_common.db import make_password_provider

    return settings.url(driver="psycopg", password=make_password_provider(settings)())


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=CATALOG_SCHEMA,
        include_schemas=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{CATALOG_SCHEMA}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=CATALOG_SCHEMA,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
