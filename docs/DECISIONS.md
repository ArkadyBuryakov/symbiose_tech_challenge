# Decision log

Running record of the non-obvious choices, what else was considered, and why.
Newest sections are appended as the phases land.

---

## Phase 0 — scaffold

### uv workspace monorepo with a shared `pmp-common` package
**Decision.** One `uv.lock` for the whole Python side; `packages/common` holds
config, events, logging, metrics, tracing, identity, Kafka/DB/S3 helpers, and
each service is a workspace member that depends on it through extras
(`pmp-common[web,db,kafka,s3]`).
**Alternatives.** Separate repos or separate lock files per service; copying the
shared code; publishing `pmp-common` to a private index.
**Why.** The event schemas and the internal-identity token are contracts between
services — a single lock file makes it impossible for the backend and the worker
to disagree about them. Extras keep the gateway image free of boto3 and the
worker image free of FastAPI.

### One parameterised Dockerfile for the four Python services
**Decision.** `ops/docker/python.Dockerfile` is built four times with
`PACKAGE`/`SERVICE_DIR`/`MODULE` build args.
**Alternatives.** Four near-identical Dockerfiles.
**Why.** The services differ only in which workspace package is installed and
which module runs. A base-image bump or a hardening change is then one edit
rather than four, and the four images cannot drift apart.

### `python.Dockerfile` installs with `--no-editable` into a self-contained venv
**Decision.** The builder stage produces `/app/.venv` with real wheels; the
runtime stage copies only that.
**Alternatives.** Editable installs plus the source tree in the final image.
**Why.** No uv, no build toolchain, and no source in the runtime image, and the
dependency layer caches independently of source edits.

### Redpanda instead of Apache Kafka locally
**Decision.** `redpandadata/redpanda` single node, plus Redpanda Console.
**Alternatives.** Apache Kafka in KRaft mode; Bitnami Kafka.
**Why.** One process, no ZooKeeper, starts in a couple of seconds and is
Kafka-protocol compatible, so the `confluent-kafka` clients are unchanged
against MSK. The trade-off is that broker-side behaviour is not byte-identical
to Apache Kafka; nothing in this system depends on broker internals.

### MinIO pulled from quay.io, not Docker Hub
**Decision.** `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z`, pinned.
**Alternatives.** `minio/minio` on Docker Hub (no longer published); LocalStack;
`adobe/s3mock`.
**Why.** The Docker Hub repository is gone, so the usual tag is not pullable.
MinIO supports everything the platform needs from S3 — presigned PUT, ranged GET
with `206`, `CopyObject`, multipart copy and `x-amz-checksum-sha256` — which the
lighter mocks do not all do.

### Publish bucket is anonymously readable *inside the compose network*
**Decision.** `mc anonymous set download publish`; the S3 port is never
published to the host, so nginx (`edge`) is the only route in.
**Alternatives.** Teaching nginx to sign SigV4 (njs); a small signing proxy.
**Why.** It is the local equivalent of CloudFront + Origin Access Control: the
CDN is the only reader and the bucket has no public access. Private-tile
authorisation happens at the edge either way (signed cookies), so no
authorisation is lost. On AWS the bucket policy restricts reads to the
distribution and this line disappears.

### Two one-shot migration containers, not one
**Decision.** `migrate` (Python: roles, schemas, Alembic for `catalog`, grants)
and, from phase 4, a separate Node container for the BetterAuth tables.
**Alternatives.** A single image containing both runtimes.
**Why.** BetterAuth owns the shape of its own tables and its CLI is the only
thing that knows them; putting a Node runtime into the Python migration image to
avoid one container is a bad trade.

### Enum-like columns are `text` + `CHECK`, not Postgres `ENUM`
**Decision.** `status`, `visibility` and `result` are text with check
constraints.
**Alternatives.** Native `CREATE TYPE ... AS ENUM`.
**Why.** Adding a value to a native enum needs DDL that is awkward to run inside
a migration transaction and impossible to roll back cleanly; a `CHECK` is a
cheap, reversible migration.

### UUIDv7 for every generated identifier
**Decision.** `pmp_common.ids.new_uuid()` returns a UUIDv7.
**Alternatives.** UUIDv4; bigserial.
**Why.** Time-ordered keys give better index locality than v4 and make job and
version listings sort by creation time without an extra column, while staying
generatable client-side (a `bigserial` would need a round trip).

### Hand-rolled asyncio wrapper around `confluent_kafka.Producer`
**Decision.** `pmp_common.kafka.AsyncProducer`: the synchronous `Producer`, one
background poll thread, and an `asyncio.Future` resolved from the delivery
callback via `loop.call_soon_threadsafe`.
**Alternatives.** `confluent_kafka.aio.AIOProducer`, which is present and no
longer experimental in the pinned 2.15.1.
**Why.** `AIOProducer` batches produce calls behind a buffer timeout
(`batch_size=1000`, `buffer_timeout=1.0s` by default). The backend produces one
message per accepted request and wants its acknowledgement promptly, so that
batching would have to be tuned away. The wrapper is ~50 lines, has no tuning
surface, and keeps `produce()` latency equal to the broker round trip.

### Ed25519 for the internal identity token, RSA for the tile cookies
**Decision.** Internal JWTs are `EdDSA`; CloudFront cookie signatures are
RSA-SHA1.
**Alternatives.** RS256 for both.
**Why.** Ed25519 keys are small, verification is fast and there are no parameter
choices to get wrong. The cookie signature algorithm is not ours to choose —
CloudFront specifies RSA-SHA1 — so that key stays RSA-2048.

### Structured logging through `PrintLoggerFactory`, not the stdlib logger
**Decision.** structlog renders JSON directly to stdout; stdlib logging is
bridged into the same renderer for third-party libraries.
**Alternatives.** `structlog.stdlib.LoggerFactory` throughout.
**Why.** It skips stdlib formatting on the hot path. The cost is that
`structlog.stdlib.add_logger_name` cannot be used (the printer has no `.name`),
so `get_logger(__name__)` binds `module` as an initial value on the lazy proxy
instead — binding it eagerly would freeze the pre-configuration renderer into
module-level loggers.

---

## Phase 1 — core API

### `Idempotency-Key` is required on `POST /publications`
**Decision.** The header is mandatory; `(tenant_id, idempotency_key)` is unique,
and a replay returns the original job with `idempotent_replay: true`.
**Alternatives.** Optional header; deduplicating on `(dataset, source_key)`.
**Why.** Publication is expensive and asynchronous, so a client that times out
*will* retry. Making the key mandatory means the safe behaviour is the only
behaviour. Deduplicating on the source key instead would wrongly collapse two
genuinely separate publications of the same staged object.

### Commit the job, then produce to Kafka
**Decision.** `POST /publications` commits the `PENDING` job row and only then
produces `publication.requested`, outside the transaction.
**Alternatives.** Produce inside the transaction; a full outbox on the backend
side as well.
**Why.** The two failure modes are not symmetric. Commit-then-produce can lose
the message, which the worker's reconciler repairs by re-emitting PENDING jobs.
Produce-then-commit can deliver a message referencing a job that was rolled
back, which nothing can repair. A backend-side outbox would close the gap
completely, but it would need the relay to run somewhere — and the worker
already has one, so the reconciler covers this case at no extra cost. The
worker's own results *do* go through an outbox, because there the write and the
event must be atomic.

### The backend never inserts version rows
**Decision.** `backend_svc` has `SELECT` on `dataset_versions` and no more; only
`worker_svc` inserts. Rollback is a pointer update on `datasets`.
**Alternatives.** Letting the backend write versions for "simple" cases.
**Why.** One writer means the versioning rules exist in exactly one place, and
the grant makes that structural rather than a convention.

### Tenant scoping is a parameter, not a filter applied later
**Decision.** Every repository function takes `tenant_id` (or an explicit
`is_admin=True`) and applies it inside the query.
**Alternatives.** Row-level security in Postgres; filtering results in the
router.
**Why.** There is no code path that forgets the predicate, because there is no
query without it. RLS would be stronger still, but it needs a per-request
`SET LOCAL` on a pooled connection — real complexity for a second control while
the first one is a one-line parameter.

### `BACKEND_AUTH_MODE=dev_stub`
**Decision.** A development identity mode that refuses to start unless
`ENVIRONMENT=local`, so the API could be built and exercised before the gateway
and auth service existed.
**Alternatives.** Building the gateway first; leaving the endpoints unprotected.
**Why.** It keeps the phases independently runnable. The guard in settings is
what makes it safe: the mode cannot be switched on in any other environment.

### `/datasets/{id}/current` is an indirection, not a redirect
**Decision.** It returns a *relative* edge path to the immutable archive, and is
cached for ~30 s.
**Alternatives.** A 302 to the object; an absolute URL.
**Why.** The relative path makes the same response correct behind `localhost`
and behind a CloudFront domain. The short cache on this response plus
`immutable` on the archive gives the best of both: a version switch is visible
within 30 seconds, while the tiles themselves are never revalidated.

---

## Phase 2 — worker

### The claim is a conditional UPDATE, not a lock table or a queue
**Decision.** `UPDATE ... SET status='RUNNING' WHERE id = :job AND (status='PENDING'
OR (status='RUNNING' AND lease_expires_at < now())) RETURNING *`.
**Alternatives.** An advisory lock; a separate `job_leases` table; relying on
Kafka partition ownership alone.
**Why.** One statement is simultaneously the claim, the mutual exclusion and the
crash recovery. A redelivered message (or a duplicate produce, or two workers on
the same partition during a rebalance) returns no row, and the consumer simply
commits the offset — the duplicate becomes a no-op with no extra machinery.
Kafka partition ownership alone is not enough, because a rebalance can hand a
partition to a second consumer while the first is still working.

### Leases are heartbeaten while a job runs
**Decision.** Default lease 60 s, renewed every 20 s by a background thread for
as long as the job holds it.
**Alternatives.** A long fixed lease sized to the worst-case job.
**Why.** A fixed lease forces a bad trade: long enough for a slow job means a
crashed worker blocks its job for that long. Heartbeating separates the two — the
lease now only bounds recovery from a *dead* worker (~1 minute), while a job that
legitimately runs for an hour keeps its claim by saying so. Verified: with
`CHAOS_CRASH_AFTER_COPY=1` the job is recovered by the reconciler, the copy is
skipped as already present, and the same content hash lands as one version row.

### Retries happen in-process, not by re-queuing
**Decision.** A transient failure sleeps with exponential backoff *and full
jitter* inside the current message's lease and re-runs the job, up to
`WORKER_MAX_ATTEMPTS`.
**Alternatives.** Re-producing to a delay topic; NACK-and-redeliver.
**Why.** Re-queuing loses partition ordering and turns a dependency outage into
a retry storm. Full jitter matters specifically when a dependency recovers:
without it every replica retries in lockstep and knocks it over again. The lease
is released before each sleep, so a crash during the wait is still recovered
promptly by the reconciler.

### Permanent failures do not go to the DLQ
**Decision.** A malformed archive, a missing source or a tenant mismatch marks
the job `FAILED` with an `error_code` and commits the offset. Only exhausted
retries are dead-lettered.
**Alternatives.** Dead-lettering every failure.
**Why.** The DLQ is a queue of things an operator should look at and possibly
replay. A file that is not PMTiles will never become PMTiles; replaying it is
pointless, and burying it among real incidents makes the DLQ worthless. The
client sees the failure on the job, and `POST /publications/{id}/retry` is the
supported way to try again after fixing the source.

### Results go through a transactional outbox; requests do not
**Decision.** The worker writes `publication.results` into `catalog.outbox` in
the same transaction as the catalogue change; a relay thread drains it with
`FOR UPDATE SKIP LOCKED`.
**Alternatives.** Producing directly after the commit (as the backend does).
**Why.** The asymmetry is deliberate. If the backend loses a request message the
reconciler re-derives it from the `PENDING` job row — the state *is* the
recovery. A lost result has no such fallback: the job is already terminal and
nothing would ever re-emit it. The outbox is the cost of making that impossible.

### Idempotency comes from the content-addressed key, not from bookkeeping
**Decision.** The publish key is `{visibility}/{tenant}/{dataset}/{sha256}/data.pmtiles`,
and the copy is skipped when an object of the right size is already there.
**Alternatives.** Recording "copy done" in the database before committing.
**Why.** A retry writes identical bytes to an identical key, so repeating the
copy is harmless by construction rather than by remembering. It also gives
`immutable` caching for free and makes deduplication across versions trivial.

### One job in flight per process
**Decision.** The consume loop is synchronous and handles one message at a time;
concurrency comes from replicas.
**Alternatives.** A thread pool or asyncio inside the worker.
**Why.** The expensive steps (hash, copy) are I/O in S3, not CPU here, so a pool
would mostly add ways for a partial failure to interleave. With one job per
process the failure model is small enough to hold in your head, and
`docker compose up --scale worker=N` (or a replica count) is the scaling knob.

### tippecanoe is built from source, not pulled
**Decision.** `ops/tippecanoe/Dockerfile` builds a pinned tag and caches it
locally; `scripts/make-synthetic-pmtiles.sh` uses it.
**Alternatives.** `ghcr.io/felt/tippecanoe` (not anonymously readable),
`klokantech/tippecanoe` (predates PMTiles output).
**Why.** No maintained tippecanoe image is publicly pullable. This is a
developer tool, not part of the running platform, so a two-minute first build is
an acceptable price for not depending on an image that may vanish.
