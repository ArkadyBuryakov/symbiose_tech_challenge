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

---

## Phase 3 — edge and map

### nginx proxies to the object store; it does not sign requests
**Decision.** `/tiles/*` proxies to the publish bucket, which is anonymously
readable *inside the compose network only*. `/staging-upload/*` forwards a
presigned request with the internal `Host` intact.
**Alternatives.** SigV4 signing in nginx (njs), or a small signing proxy.
**Why.** It is the local shape of CloudFront + Origin Access Control: the CDN
reads the bucket, nobody else can reach it, and authorisation for private
objects happens at the edge (signed cookies). Teaching nginx to sign would add a
moving part that does not exist on AWS at all.

### The presigned URL's origin is rewritten, nothing else
**Decision.** The backend presigns against `http://s3:9000` and swaps only the
*origin* for the public edge; nginx forwards the request upstream with
`Host: s3:9000`.
**Alternatives.** Publishing the object store on the host; presigning against
`localhost:8080` directly.
**Why.** SigV4 signs the `Host` header, so the URL must be signed for the host
the request will actually carry. Rewriting only the origin keeps the path, the
query string and therefore the signature intact, keeps the store off the host
network, and avoids CORS entirely because the browser stays on one origin.
Verified end to end, including that a mismatched `x-amz-checksum-sha256` is
rejected with `XAmzContentChecksumMismatch`.

### `Cache-Control` is set at the edge, not trusted from the origin
**Decision.** `proxy_hide_header Cache-Control` then `add_header`: `immutable`
for public tiles, `private, max-age=600` for private ones.
**Alternatives.** Relying on the metadata the worker writes onto the object.
**Why.** The same object can be served under two policies (a dataset can be
private), so the policy belongs to the route, not to the bytes. The edge also
strips the store's `Strict-Transport-Security`, `Vary` and rate-limit headers,
which are wrong or meaningless here and would otherwise leak the origin.

### The map is built from the stored spec, and falls back to archive metadata
**Decision.** `buildLayers()` reads `layers`, `style.color_field` and
`field_ranges_by_resolution` from the version's spec; with no `layers` array it
falls back to the archive's own `vector_layers`.
**Alternatives.** Hard-coding the H3 layer names; requiring a full spec.
**Why.** Adding a resolution or changing the colour field is then a change to
the published spec, not to the page. The fallback is what lets a minimal spec
(or none) still render, which matters because the synthetic fixture has no
hand-written spec.

### Per-resolution colour ranges
**Decision.** Each H3 layer is coloured against its own `count` range.
**Alternatives.** One global range from `style.min`/`style.max`.
**Why.** `count` means different things at different resolutions — r10 tops out
at 728, r12 at 32 — so a shared scale renders the fine layers almost uniformly
pale. Each layer gets its own legend for the same reason.

---

## Phase 4 — auth and gateway

### The gateway is a small FastAPI service, not Envoy or Kong
**Decision.** ~300 lines of FastAPI: route table, verify + cache, internal JWT,
rate limit, streaming proxy.
**Alternatives.** Envoy Gateway with `ext_authz`; Kong; an ALB with Lambda
authorisers.
**Why.** For this platform the gateway's job is small and unusual enough
(mint a signed internal identity, per-tenant rate limiting, a pass-through auth
surface) that the configuration to express it in Envoy would be about as long as
the code — and much harder to unit-test. The decision is deliberately
reversible: `/internal/verify` is an ordinary HTTP endpoint, so Envoy's
`ext_authz` filter could call it unchanged, and the route table is already
declarative YAML.

### Identity is a signed token, not trusted headers
**Decision.** The gateway mints a 60-second EdDSA JWT (`aud` = upstream name)
and the services verify it with the gateway's public key.
**Alternatives.** `X-User-Id`/`X-Tenant-Id` headers with a NetworkPolicy
guaranteeing only the gateway can reach the backend.
**Why.** Trusted headers make every future networking mistake a full
authentication bypass — a port-forward, a misconfigured policy, a debug sidecar.
With a signed token the backend is safe even when reached directly, which is
exactly what the tests assert. The `aud` claim additionally stops a token minted
for the backend being replayed against another service.

### Revocation bounds, stated explicitly
**Decision.** Two different bounds, both deliberate:
* **API** — a revoked session keeps working for at most
  `GATEWAY_VERIFY_CACHE_TTL` (default 10 s), because successful verifications are
  cached. BetterAuth's own cookie cache is disabled so nothing else adds to it.
* **Private tiles** — a signed cookie stays valid until it expires (default
  10 minutes); there is no revocation list, exactly as with CloudFront.
**Alternatives.** No verify cache (every request hits the auth service); a
revocation list checked at the edge.
**Why.** 10 seconds of staleness in exchange for roughly an order of magnitude
fewer calls to the auth service is a good trade, and it is a *bounded*, stated
one. The tile bound is inherent to signed cookies; shortening the TTL is the
only dial, and 10 minutes keeps refreshes rare while bounding exposure.

### Only successful verifications are cached
**Decision.** A 401 from `/internal/verify` is never cached.
**Why.** Caching failures turns a momentary auth-service hiccup into a
lockout for the whole TTL. The asymmetry costs nothing: failures are rare.

### Rate limiting is per replica and says so
**Decision.** In-memory token bucket keyed by user id, or client IP when
anonymous.
**Alternatives.** Redis counters; no limiting.
**Why.** It is an abuse control, not a quota: with N replicas the effective
limit is N times the configured rate. That is fine for what it is for, and the
step to a shared counter (Redis `INCR`/`EXPIRE`) does not change the interface.
Keying by user rather than by IP means one noisy tenant cannot starve the rest.

### `/api/auth/*` is a verbatim pass-through
**Decision.** Policy `public`: no identity check, no internal token, every
header forwarded unchanged in both directions.
**Why.** The session cookie is the browser's credential; the gateway has no
business interpreting, merging or re-signing it. This is also why the proxy
handles headers as a multi-valued list — collapsing them into a dict silently
drops all but one `Set-Cookie`, which has its own test.

### Organizations and memberships are seeded with SQL, users through the API
**Decision.** `signUpEmail` for users (password hashing is BetterAuth's job);
direct inserts for organizations and members.
**Alternatives.** `auth.api.createOrganization` / `addMember` throughout.
**Why.** Those endpoints act on behalf of a session and make the caller an
owner. The seed needs neither: it needs deterministic ids and a Bob who is only
a `member`, so that tenant-role authorisation is actually exercised rather than
assumed.

### A single-organization user does not have to choose one
**Decision.** `/internal/verify` falls back to the caller's sole membership when
the session has no active organization; more than one membership with no
explicit choice yields `tenant_id: null`.
**Alternatives.** A BetterAuth `session.create.before` hook; requiring an
explicit choice always.
**Why.** Refusing a single-tenant user until they pick their only tenant is
pointless friction, and guessing for a multi-tenant user would silently decide
which tenant's data a request touches. The fallback lives in `verify` rather
than in a plugin hook so its behaviour is explicit and testable.

---

## Phase 5 — private delivery

### Signed cookies, not signed URLs
**Decision.** Private archives are authorised by CloudFront-format signed
cookies issued by `POST /api/v1/tiles/session`.
**Alternatives.** Signed URLs per archive; proxying private tiles through the
backend.
**Why.** A PMTiles archive is read with many range requests to *one* URL. A
signed URL would put a credential in every request line (and in every log and
cache key) and would change whenever it was re-signed, defeating caching.
Proxying through the backend would put the API in the tile hot path. Cookies
leave the object URL stable and cacheable, and CloudFront validates them
natively on AWS.

### Tenant-wide cookie scope
**Decision.** The policy resource is `/tiles/private/{tenant_id}/*`, or
`/tiles/private/*` for platform administrators.
**Alternatives.** One cookie per dataset.
**Why.** Per-dataset scope would be marginally tighter, but a tenant member is
already entitled to every private dataset of their tenant, so it would add
re-issuance on every dataset switch without reducing what a leaked cookie
exposes in practice. Tenant isolation — the property that matters — is exact,
including the `org_tenant-a` vs `org_tenant-ab` prefix case, which has a test.

### The cookie format is checked against botocore, not against ourselves
**Decision.** Unit tests assert that our policy JSON is byte-identical to
`botocore.signers.CloudFrontSigner.build_policy`, and that a cookie signed with
botocore verifies in the edge verifier.
**Why.** CloudFront signs the literal policy bytes, so a key-order or
whitespace difference would pass every self-consistency test and then fail on
AWS. botocore is AWS's reference implementation, which makes it the right
oracle.

### The edge verifier holds only the public key
**Decision.** `edge-verifier` can validate cookies but cannot mint them; only
the backend has the private key.
**Why.** It mirrors CloudFront's trusted key group exactly, and it means the
component that is deleted on AWS never held anything worth stealing.

### `Accept-Ranges` and `Cache-Control` are replaced at the edge, not appended
**Decision.** `proxy_hide_header` before `add_header` for both.
**Why.** Found by the e2e suite: MinIO sends `Accept-Ranges` itself, so adding
ours produced `Accept-Ranges: bytes, bytes` — a malformed header some clients
reject.

---

## Phase 6 — operability

### Client IP comes from `X-Real-IP`, never from the front of `X-Forwarded-For`
**Decision.** The gateway's anonymous rate-limit buckets and BetterAuth's
sign-in rate limit both key on `X-Real-IP`, which the edge overwrites with the
TCP peer it saw.
**Alternatives.** The first `X-Forwarded-For` entry (what the gateway did
originally); BetterAuth's `trustedProxies` chain walking.
**Why.** nginx *appends* to a client-supplied `X-Forwarded-For`, so its first
entry is whatever the client wants it to be — keying a rate limit on it lets an
attacker mint a fresh bucket per request. Found while making the e2e suite pass
BetterAuth's sign-in limit. `trustedProxies` would also work but needs the
compose/VPC CIDRs baked into config, and the edge already produces a trustworthy
single value.

### The e2e suite respects the sign-in rate limit instead of loosening it
**Decision.** Users sign in once per test session; the revocation tests that
need fresh sessions wait out a `429` using BetterAuth's `x-retry-after`.
**Alternatives.** A laxer limit in `.env.example`.
**Why.** Three sign-ins per ten seconds per client is a real credential-stuffing
control, and a test setup that silently disables it tends to leak into the
configuration people actually deploy.

### Tracing starts at the edge
**Decision.** The edge uses the official `nginx:*-otel` image; under
`PROFILE=observability` it starts a span per request and propagates
`traceparent`. Services export over OTLP only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set; Kafka carries `traceparent` in message
headers, including through the outbox.
**Alternatives.** Starting traces at the gateway.
**Why.** The brief asks for one trace from the edge request to the worker's
database and S3 calls. Off by default so the normal stack has no exporter
retrying against a collector that is not running.

### The Grafana dashboard is generated
**Decision.** `scripts/build-grafana-dashboard.py` writes the provisioned JSON.
**Why.** Panel ids and grid positions are mechanical; the generator keeps each
PromQL query readable in one line of review instead of buried in 1,000 lines of
JSON.

### `/tiles/session` accepts a platform admin without a tenant
**Decision.** A dedicated dependency, `require_tenant_or_platform_admin`, instead
of the tenant-only one.
**Why.** Found by the e2e suite: the handler's platform-admin branch (scope
`/tiles/private/*`) was unreachable because the tenant check ran first and
refused a caller with no organization. A scope that depends on *who* is asking
needs a dependency that admits both kinds of caller and lets the handler decide.

### The worker enforces its own shutdown bound
**Decision.** On `SIGTERM` the worker stops polling, lets the in-flight job run
for up to `WORKER_SHUTDOWN_GRACE_SECONDS` (25 s), then exits itself; Docker's
`stop_grace_period` (40 s) is longer, so the worker always leaves first.
**Why.** Leaving the bound to the orchestrator's `SIGKILL` skips the final outbox
drain and the clean consumer-group leave. Abandoning the job is safe for the
same reasons a crash is: no offset commit, lease stops being renewed,
content-addressed redo.

---

## Deliberately out of scope (future work)

* **WebSocket / SSE job status.** The UI polls `GET /publications/{id}` every
  second while a job is in flight. A push channel would need the gateway to
  proxy WebSocket upgrades (with the same identity check on the handshake) and a
  fan-out from `publication.results` — a consumer that is not built yet. The
  results topic and the outbox are already in place for it.
* **A `publication.results` consumer.** Results are produced (through the
  outbox) and schema'd, but nothing consumes them yet; they exist for
  notifications, webhooks and the push channel above.
* **Shared rate-limit state.** Per-replica token buckets; Redis/ElastiCache is
  the next step once limits must be exact across replicas.
* **Version retirement API.** `RETIRED` is supported by the schema and the
  versioning rules, but there is no endpoint to set it; the runbook shows the
  SQL.
* **Visibility changes.** A dataset's visibility is fixed at creation. Changing
  it requires copying objects between the `public/` and `private/` prefixes and
  is a separate, deliberate operation.
* **Row-level security in Postgres** as a second tenant-isolation control
  (see *Tenant scoping is a parameter* above).

### Chaos tooling reuses the running image, it does not rebuild
**Decision.** `make chaos-crash-after-copy` and the e2e crash test recreate the
worker from the image tag and OTLP endpoint of the container that is running.
**Why.** Found when a commit landed between `make up` and a test run: the image
tag was being recomputed from `git rev-parse HEAD`, which named an image that
had never been built. A chaos run should exercise the code under test, not
silently rebuild it — and recreating the worker without its OTLP endpoint would
have switched tracing off under the observability profile.
