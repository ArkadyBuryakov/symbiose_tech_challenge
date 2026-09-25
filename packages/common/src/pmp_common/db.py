"""Postgres engine factories.

The password is supplied by a *provider* callable rather than baked into the
URL. Locally the provider returns the static ``DB_PASSWORD``; with
``DB_AUTH=iam`` it returns a short-lived RDS IAM auth token. Because the
provider is consulted on every new connection, token rotation needs no code
change — only ``DB_AUTH=iam`` in the environment.

Both a sync engine (worker, Alembic) and an async engine (backend) are built
from the same settings and the same psycopg 3 driver.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import DbSettings

__all__ = [
    "CATALOG_SCHEMA",
    "PasswordProvider",
    "make_async_engine",
    "make_password_provider",
    "make_sync_engine",
]

CATALOG_SCHEMA = "catalog"
PasswordProvider = Callable[[], str | None]

# RDS IAM auth tokens are valid for 15 minutes; refresh well before that.
_IAM_TOKEN_TTL_SECONDS = 10 * 60


def make_password_provider(settings: DbSettings) -> PasswordProvider:
    """Return a callable that yields the password for a fresh connection."""
    if settings.auth == "password":
        password = settings.password
        return lambda: password
    return _make_iam_token_provider(settings)


def _make_iam_token_provider(settings: DbSettings) -> PasswordProvider:
    """RDS IAM authentication.

    STUB: not exercised locally (no RDS), kept deliberately tiny and isolated so
    that switching to ``DB_AUTH=iam`` on AWS is a configuration change. The IAM
    policy required by each service is listed in ``docs/aws-mapping.md``.
    """
    cache: dict[str, tuple[float, str]] = {}

    def provider() -> str:
        now = time.monotonic()
        cached = cache.get("token")
        if cached and now < cached[0]:
            return cached[1]

        import boto3  # imported lazily: only the IAM path needs boto3

        client = boto3.client("rds")
        token: str = client.generate_db_auth_token(
            DBHostname=settings.host, Port=settings.port, DBUsername=settings.user
        )
        cache["token"] = (now + _IAM_TOKEN_TTL_SECONDS, token)
        return token

    return provider


def _engine_kwargs(
    settings: DbSettings, application_name: str, pool_size: int | None
) -> dict[str, Any]:
    options = (
        f"-c statement_timeout={settings.statement_timeout_ms} "
        f"-c search_path={CATALOG_SCHEMA},public"
    )
    return {
        "url": settings.url(driver="psycopg", password=None),
        "pool_size": pool_size if pool_size is not None else settings.pool_size,
        "max_overflow": settings.pool_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": settings.pool_recycle_seconds,
        "connect_args": {
            "connect_timeout": settings.connect_timeout,
            "sslmode": settings.sslmode,
            "application_name": application_name,
            "options": options,
        },
    }


def _install_password_provider(engine: Engine | AsyncEngine, settings: DbSettings) -> None:
    """Inject a freshly resolved password into every new DBAPI connection."""
    provider = make_password_provider(settings)
    target = engine.sync_engine if isinstance(engine, AsyncEngine) else engine

    @event.listens_for(target, "do_connect")
    def _do_connect(_dialect: Any, _rec: Any, _cargs: Any, cparams: dict[str, Any]) -> None:
        password = provider()
        if password is not None:
            cparams["password"] = password


def make_sync_engine(
    settings: DbSettings, *, application_name: str, pool_size: int | None = None
) -> Engine:
    """Engine for the worker and for Alembic."""
    engine = create_engine(**_engine_kwargs(settings, application_name, pool_size))
    _install_password_provider(engine, settings)
    return engine


def make_async_engine(
    settings: DbSettings, *, application_name: str, pool_size: int | None = None
) -> AsyncEngine:
    """Engine for the async FastAPI backend."""
    engine = create_async_engine(**_engine_kwargs(settings, application_name, pool_size))
    _install_password_provider(engine, settings)
    return engine
