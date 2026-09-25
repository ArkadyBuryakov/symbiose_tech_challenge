"""FastAPI dependencies: database connections, settings, and collaborators.

Long-lived resources (engine, Kafka producer, S3 client) are created once in
the application lifespan and handed out from ``app.state``; per-request
resources (a database connection) are created here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pmp_common.kafka import AsyncProducer

from .settings import BackendSettings
from .storage import Storage

__all__ = ["Conn", "Producer", "Settings", "Store"]


def get_settings_dep(request: Request) -> BackendSettings:
    settings: BackendSettings = request.app.state.settings
    return settings


def get_producer(request: Request) -> AsyncProducer:
    producer: AsyncProducer = request.app.state.producer
    return producer


def get_storage(request: Request) -> Storage:
    storage: Storage = request.app.state.storage
    return storage


async def get_connection(request: Request) -> AsyncIterator[AsyncConnection]:
    """One database connection per request, in "commit as you go" style.

    ``engine.connect()`` rather than ``engine.begin()`` because a handler
    sometimes needs to commit *before* it finishes — ``POST /publications``
    commits the job row and only then produces to Kafka. Anything still
    uncommitted at the end of a successful request is committed here; any
    exception rolls back.
    """
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        try:
            yield conn
        except Exception:
            await conn.rollback()
            raise
        else:
            await conn.commit()


Conn = Annotated[AsyncConnection, Depends(get_connection)]
Settings = Annotated[BackendSettings, Depends(get_settings_dep)]
Producer = Annotated[AsyncProducer, Depends(get_producer)]
Store = Annotated[Storage, Depends(get_storage)]
