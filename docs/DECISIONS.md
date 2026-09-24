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
