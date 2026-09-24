"""Backend application factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from pmp_common.db import make_async_engine
from pmp_common.kafka import AsyncProducer
from pmp_common.logging import configure_logging, get_logger
from pmp_common.tracing import configure_tracing, instrument_fastapi, instrument_psycopg
from pmp_common.web import create_app

from .identity import build_identity_resolver
from .publisher import PublicationPublisher
from .routers import admin, datasets, demo, publications
from .settings import BackendSettings, get_settings
from .storage import Storage

__all__ = ["build_app"]

log = get_logger(__name__)

API_PREFIX = "/api/v1"


def build_app(settings: BackendSettings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        service=settings.service_name,
        level=settings.observability.log_level,
        fmt=settings.observability.log_format,
        git_sha=settings.git_sha,
    )
    configure_tracing(
        service=settings.service_name,
        version=settings.git_sha,
        endpoint=settings.observability.otlp_endpoint,
        sample_ratio=settings.observability.trace_sample_ratio,
        environment=settings.environment,
    )
    instrument_psycopg()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Long-lived resources: one engine, one Kafka producer, one S3 client.
        app.state.settings = settings
        app.state.engine = make_async_engine(settings.db, application_name="pmp-backend")
        app.state.storage = Storage(settings.s3, public_base_url=settings.public_base_url)
        app.state.identity_resolver = build_identity_resolver(settings)

        producer = AsyncProducer(settings.kafka, client_id=f"backend-{settings.git_sha}")
        producer.start()
        app.state.producer = producer
        app.state.publisher = PublicationPublisher(
            producer, topic=settings.kafka.topic_publication_requested
        )

        log.info(
            "backend.started",
            auth_mode=settings.auth_mode,
            demo_uploads=settings.demo_upload_enabled,
            environment=settings.environment,
        )
        if settings.auth_mode == "dev_stub":
            log.warning(
                "backend.auth_disabled",
                detail="BACKEND_AUTH_MODE=dev_stub: every request runs as a fixed identity",
                user_id=settings.dev_stub_user_id,
                tenant_id=settings.dev_stub_tenant_id,
            )
        try:
            yield
        finally:
            await producer.close()
            await app.state.engine.dispose()
            log.info("backend.stopped")

    async def readiness() -> None:
        """Ready means: the catalogue is reachable and Kafka metadata resolves.

        Both are required to accept a publication request, so a backend that
        cannot reach either should be taken out of rotation rather than
        returning 500s.
        """
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        if not app.state.producer.healthy():
            raise RuntimeError("kafka producer cannot reach the cluster")

    app = create_app(
        title="PMTiles Catalogue API",
        service=settings.service_name,
        version=settings.git_sha,
        readiness=readiness,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
        description=(
            "Datasets, publication jobs and immutable versions.\n\n"
            "Every endpoint is tenant-scoped through the internal identity token "
            "minted by the gateway; this service never sees end-user credentials."
        ),
    )

    for router in (publications.router, datasets.router, admin.router, demo.router):
        app.include_router(router, prefix=API_PREFIX)

    instrument_fastapi(app)
    return app
