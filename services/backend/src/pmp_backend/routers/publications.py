"""Publication endpoints.

``POST /publications`` is the only write in the system that starts work. Its
contract:

* ``Idempotency-Key`` is required, so a client that retries after a timeout
  gets the *same* job back instead of publishing twice.
* The caller may only reference staged objects under their own tenant prefix.
* The job row is committed before the Kafka message is produced; if the produce
  fails the job stays PENDING and the worker's reconciler re-emits it. The
  response is ``202`` either way — the work is queued, not done.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from pmp_common.enums import JobStatus, Visibility
from pmp_common.logging import bind_log_context, get_logger
from pmp_common.metrics import PUBLICATION_REQUESTS
from pmp_common.problem import Conflict, Forbidden, NotFound
from pmp_common.s3 import staging_prefix
from pmp_common.tracing import current_trace_id

from .. import repository as repo
from ..deps import Conn, Producer
from ..identity import CurrentIdentity
from ..job_events import JobEventHub
from ..publisher import emit_publication_requested
from ..schemas import CreatePublicationRequest, JobResponse, Page, PublicationAccepted

router = APIRouter(prefix="/publications", tags=["publications"])
log = get_logger(__name__)

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
        description="Client-generated key. Replaying it returns the original job.",
    ),
]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PublicationAccepted,
    summary="Request publication of a staged PMTiles archive",
)
async def create_publication(
    body: CreatePublicationRequest,
    idempotency_key: IdempotencyKey,
    identity: CurrentIdentity,
    conn: Conn,
    producer: Producer,
    response: Response,
) -> PublicationAccepted:
    tenant_id = identity.require_tenant()

    # A tenant may only publish objects it staged itself. Without this check a
    # caller could publish another tenant's staged upload by guessing its key.
    if not body.source_key.startswith(staging_prefix(tenant_id)):
        PUBLICATION_REQUESTS.labels("rejected").inc()
        raise Forbidden(
            "source_key must be under this tenant's staging prefix.",
            code="source-key-outside-tenant",
            expected_prefix=staging_prefix(tenant_id),
        )

    existing = await repo.find_job_by_idempotency_key(
        conn, tenant_id=tenant_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        PUBLICATION_REQUESTS.labels("idempotent_replay").inc()
        response.headers["Location"] = f"/api/v1/publications/{existing.id}"
        log.info("publication.idempotent_replay", job_id=str(existing.id))
        return PublicationAccepted(
            job_id=existing.id,
            dataset_id=existing.dataset_id,
            status=JobStatus(existing.status),
            idempotent_replay=True,
        )

    dataset = await repo.upsert_dataset(
        conn,
        tenant_id=tenant_id,
        slug=body.dataset_slug,
        name=body.name or body.dataset_slug,
        # Private unless asked otherwise: publishing must never expose data by accident.
        visibility=body.visibility or Visibility.PRIVATE,
    )
    job = await repo.create_job(
        conn,
        tenant_id=tenant_id,
        dataset_id=dataset.id,
        idempotency_key=idempotency_key,
        source_key=body.source_key,
        spec=body.spec,
        requested_by=identity.user_id,
        trace_id=current_trace_id(),
    )
    bind_log_context(job_id=str(job.id), dataset_id=str(dataset.id))

    # Commit before producing: a lost message is recoverable by the reconciler,
    # a message pointing at an uncommitted job is not.
    await conn.commit()
    await emit_publication_requested(producer, job=job, dataset=dataset)

    PUBLICATION_REQUESTS.labels("accepted").inc()
    response.headers["Location"] = f"/api/v1/publications/{job.id}"
    return PublicationAccepted(job_id=job.id, dataset_id=dataset.id, status=JobStatus.PENDING)


@router.get("", response_model=Page[JobResponse], summary="List publication jobs")
async def list_publications(
    identity: CurrentIdentity,
    conn: Conn,
    dataset_id: Annotated[UUID | None, Query()] = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[JobResponse]:
    rows = await repo.list_jobs(
        conn,
        tenant_id=identity.require_tenant(),
        dataset_id=dataset_id,
        status=job_status,
        limit=limit,
        offset=offset,
    )
    return Page(items=[JobResponse.model_validate(row) for row in rows], limit=limit, offset=offset)


# Comment lines keep the gateway (30 s read timeout) and edge (60 s) from
# closing an idle stream. Streams end after STREAM_MAX_SECONDS so the browser's
# EventSource reconnects through the gateway, which re-verifies the session:
# that bounds how long a revoked session keeps receiving events.
KEEPALIVE_SECONDS = 15.0
STREAM_MAX_SECONDS = 300.0


@router.get(
    "/events",
    response_class=StreamingResponse,
    summary="Live job updates (Server-Sent Events)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def publication_events(request: Request, identity: CurrentIdentity) -> StreamingResponse:
    """Stream ``event: job`` messages, each the full :class:`JobResponse` of a
    job of the caller's tenant whose status changed, driven by the Kafka
    ``publication.requested`` / ``publication.results`` topics."""
    tenant_id = identity.require_tenant()
    hub: JobEventHub = request.app.state.job_events
    engine = request.app.state.engine
    queue = hub.subscribe(tenant_id)

    async def stream() -> AsyncIterator[str]:
        deadline = time.monotonic() + STREAM_MAX_SECONDS
        try:
            yield "retry: 2000\n\n"
            while time.monotonic() < deadline:
                try:
                    job_id = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # A connection per event, not per stream: a stream is open for
                # minutes and must not pin a pool slot.
                async with engine.connect() as conn:
                    row = await repo.get_job(conn, job_id, tenant_id=tenant_id)
                if row is not None:
                    job = JobResponse.model_validate(row).model_dump_json()
                    yield f"event: job\ndata: {job}\n\n"
        finally:
            hub.unsubscribe(tenant_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{job_id}", response_model=JobResponse, summary="Get one publication job")
async def get_publication(
    job_id: Annotated[UUID, Path()],
    identity: CurrentIdentity,
    conn: Conn,
) -> JobResponse:
    row = await repo.get_job(conn, job_id, tenant_id=identity.require_tenant())
    if row is None:
        raise NotFound(f"No publication job {job_id} for this tenant.")
    return JobResponse.model_validate(row)


@router.post(
    "/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PublicationAccepted,
    summary="Retry a failed publication job",
)
async def retry_publication(
    job_id: Annotated[UUID, Path()],
    identity: CurrentIdentity,
    conn: Conn,
    producer: Producer,
) -> PublicationAccepted:
    """Re-queue a FAILED job.

    Only FAILED jobs are retryable: a PENDING or RUNNING job is already the
    reconciler's responsibility, and re-queuing a SUCCEEDED job would be a
    no-op that muddies the audit trail.
    """
    tenant_id = identity.require_tenant()

    # The conditional UPDATE is the concurrency control; only on a miss do we
    # read the job to tell "not found" from "not retryable".
    job = await repo.mark_job_pending_for_retry(conn, job_id=job_id, tenant_id=tenant_id)
    if job is None:
        existing = await repo.get_job(conn, job_id, tenant_id=tenant_id)
        if existing is None:
            raise NotFound(f"No publication job {job_id} for this tenant.")
        raise Conflict(
            f"Only FAILED jobs can be retried; this job is {existing.status}.",
            code="job-not-retryable",
            current_status=existing.status,
        )

    dataset = await repo.get_dataset(conn, job.dataset_id, tenant_id=tenant_id)
    assert dataset is not None  # FK: a job's dataset always exists
    await conn.commit()
    await emit_publication_requested(producer, job=job, dataset=dataset)

    log.info("publication.retried", job_id=str(job.id))
    return PublicationAccepted(job_id=job.id, dataset_id=dataset.id, status=JobStatus.PENDING)
