"""Lease heartbeat.

A job's lease answers one question: *is the worker that claimed this still
alive?* That forces a trade-off — a long lease means a crashed worker blocks
its job for a long time, a short one risks stealing a job from a worker that is
merely busy.

Renewing the lease while the job runs removes the trade-off. The lease can then
be short (recovery within a minute) while a job that legitimately takes an hour
keeps its claim, because it keeps saying so.
"""

from __future__ import annotations

import threading
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from pmp_common.logging import get_logger

from .catalog import renew_lease

__all__ = ["LeaseHeartbeat"]

log = get_logger(__name__)


class LeaseHeartbeat:
    """Renews one job's lease in the background for as long as it is held."""

    def __init__(self, engine: Engine, *, job_id: UUID, owner: str, lease_seconds: int) -> None:
        self._engine = engine
        self._job_id = job_id
        self._owner = owner
        self._lease_seconds = lease_seconds
        # Renew well inside the lease so a single failed renewal is not fatal.
        self._interval = max(lease_seconds / 3, 1.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> LeaseHeartbeat:
        self._thread = threading.Thread(target=self._run, name=f"lease-{self._job_id}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._engine.begin() as conn:
                    still_ours = renew_lease(
                        conn,
                        job_id=self._job_id,
                        owner=self._owner,
                        lease_seconds=self._lease_seconds,
                    )
            except SQLAlchemyError as exc:
                log.warning("worker.lease_renew_failed", job_id=str(self._job_id), error=str(exc))
                continue
            if not still_ours:
                # Somebody else owns the job now, or it reached a terminal
                # state. Stop renewing; the work in flight will find out when it
                # tries to commit.
                log.warning("worker.lease_lost", job_id=str(self._job_id))
                return
