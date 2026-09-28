# Decision log

One short entry per non-obvious choice: what was decided, what else was
considered, and why.

## Build and infrastructure

**uv workspace with a shared `pmp-common` package.** One lock file for all
Python services. The alternatives were separate repos or copying the shared
code. Event schemas and the internal token are contracts between services, so
one lock means the services cannot disagree about them. Extras keep boto3 out of
the gateway image and FastAPI out of the worker image.

**One parameterised Dockerfile for the four Python services.** Build args choose
the package and module. The alternative was four near-identical files. A base
or hardening change is one edit, and the images cannot drift apart.

**Redpanda locally instead of Apache Kafka.** It is a single binary with no
ZooKeeper, and it speaks the Kafka protocol, so the clients are unchanged
against MSK. Nothing here depends on broker internals.

**MinIO from quay.io.** The Docker Hub image is no longer published. MinIO
supports presigned PUT, ranged GET with 206, CopyObject, multipart copy and
`x-amz-checksum-sha256`; the lighter mocks do not support all of these.

**Two one-shot migrators (Python `migrate`, Node `migrate-auth`).** The
alternative was one image with both runtimes. BetterAuth owns the shape of its
tables and its own CLI is the only thing that knows it.

**One folded catalogue migration.** Nothing had been deployed, so the
spec-identity change was folded into `0001` instead of kept as `0002`. The cost
is that existing local volumes need `make clean`.

**`text` + `CHECK` instead of Postgres `ENUM`.** Adding a value is a cheap,
reversible migration instead of awkward, non-transactional DDL.

**UUIDv7 identifiers.** Time-ordered keys give better index locality and a
natural sort order, and they are still generated client-side.

**Hand-rolled asyncio wrapper around `confluent_kafka.Producer`.** It uses a
poll thread and a future resolved with `call_soon_threadsafe`.
`AIOProducer` batches behind a 1 s buffer timeout by default. The backend
produces one message per request and wants the acknowledgement promptly.

**Ed25519 for the internal token, RSA-SHA1 for tile cookies.** Ed25519 has no
parameters to get wrong. CloudFront dictates RSA-SHA1 for its cookies.

**tippecanoe built from source.** No maintained image can be pulled
anonymously. It is only a developer tool, so a two-minute first build is
acceptable.

**Grafana dashboard checked in as JSON.** It is provisioned as-is and edited in
Grafana when needed. A generator was tried and removed as extra machinery.

## Publication API and worker

**`Idempotency-Key` is required.** `(tenant_id, key)` is unique, and a replay
returns the original job. A client that times out will retry, so the safe
behaviour must be the only behaviour.

**Commit the job, then produce.** A lost message leaves a `PENDING` row that
the reconciler re-emits. Producing first could reference a rolled-back job,
which nothing can repair.

**New datasets default to `private`.** Publishing tenant data publicly by
accident is the worse mistake. Public is an explicit `"visibility": "public"`.

**Only the worker writes versions.** `backend_svc` has only `SELECT` on
`dataset_versions`, so the versioning rules exist in exactly one place.

**Tenant scoping is a query parameter.** Every repository function takes
`tenant_id` (or an explicit admin flag), so no query exists without the
predicate. RLS would be a second control, at the cost of per-request
`SET LOCAL`.

**`/datasets/{id}/current` is a relative-path indirection, cached for 30 s.**
It is correct behind localhost and behind CloudFront, and the archive itself is
`immutable`.

**A version is the archive bytes *and* the spec (changes the brief's rule).**
Identity is `(sha256, spec_sha256)`, where `spec_sha256` is the SHA-256 of the
canonical JSON. The same bytes with a new spec give `CREATED`, while the same
bytes and spec give `DEDUPLICATED`. The alternatives were the brief's rule
(which silently discarded a new spec) or a mutable spec endpoint (which breaks
rollback of styling). Both versions share one stored object. This was agreed
with the product owner before implementation.

**The claim is one conditional `UPDATE`.** It is the claim, the mutual
exclusion and the crash recovery in one statement. A duplicate delivery finds
no row and just commits its offset. Partition ownership alone is not enough
during a rebalance.

**Leases are heartbeaten.** A 60 s lease is renewed every 20 s. The lease then
bounds only recovery from a dead worker, not the length of a job.

**In-process retries with full jitter.** Re-queuing loses ordering and turns an
outage into a retry storm. Jitter stops replicas retrying in lockstep.

**Permanent failures skip the DLQ.** A file that is not PMTiles never will be.
The DLQ is kept for things an operator should replay. The client retries with
`POST /publications/{id}/retry`.

**Results use a transactional outbox; requests do not.** A lost request is
re-derived from the `PENDING` row. A lost result has no such fallback.

**Content-addressed publish keys.** A retry writes the same bytes to the same
key and skips the copy when the object is already there, so nothing needs to be
recorded.

**One job in flight per process.** The failure model stays small. Replicas are
the scaling knob.

**Worker enforces its own shutdown bound.** It exits after 25 s, well inside
Docker's 40 s, so the outbox drain and the consumer-group leave always run.

## Edge and map

**nginx proxies to the object store; it does not sign.** The publish bucket is
readable only inside the compose network, which is the local shape of
CloudFront plus Origin Access Control.

**Only the presigned URL's origin is rewritten.** SigV4 signs the `Host`, so
nginx forwards with `Host: s3:9000`. The store stays off the host and there is
no CORS.

**`Cache-Control` and `Accept-Ranges` are set at the edge.** The policy belongs
to the route, not to the bytes. Headers are replaced, not appended, because
MinIO already sends `Accept-Ranges`.

**The map is built only from the stored spec.** Per-resolution colour ranges
are used because `count` at r10 and at r12 differ by 20×. A version without a
spec is reported, not guessed at.

**OpenStreetMap basemap with a YlOrRd ramp.** A green ramp vanished into OSM's
forest colours. The OSM tile policy allows light interactive use only, so
production should switch the tile URL.

## Auth and gateway

**Custom FastAPI gateway, not Envoy or Kong.** The job is small (route table,
verify + cache, internal JWT, rate limit, streaming proxy). The same logic in
Envoy config would be as long and harder to test. The choice is reversible:
Envoy's `ext_authz` could call `/internal/verify` unchanged.

**Internal signed JWT, not trusted headers.** Trusted headers turn any
networking mistake into an authentication bypass. The backend verifies a 60 s
EdDSA token with `aud` set to itself, so it is safe even when reached directly.

**Revocation bounds.** On the API, a revoked session works for at most
`GATEWAY_VERIFY_CACHE_TTL` (10 s); only successes are cached. On private tiles,
the signed cookie is valid until its 10-minute TTL, the same as CloudFront.

**Rate limits are per route class, per replica.** `routes.yaml` gives each
class (`auth`, `read`, `default`) its own `{rps, burst}` token bucket, keyed by
user id or `X-Client-IP`. It is an abuse control. Redis is the multi-replica next
step.

**`/api/auth/*` is a verbatim pass-through.** Headers stay a multi-valued list,
so every `Set-Cookie` survives. The upstream client keeps no cookie jar; one
did, and it leaked sessions between callers.

**Self sign-up is disabled.** Tenants and users are provisioned by an operator
(`make seed`, `make add-tenant`, `make add-user`). A public sign-up endpoint
would let anyone create accounts on a multi-tenant platform.

**Single-membership users need not pick an organization.** `/internal/verify`
falls back to the sole membership. With several memberships and no choice, the
token has no tenant, which is better than guessing.

**Client IP comes from `X-Client-IP`.** nginx appends to a client-supplied
`X-Forwarded-For`, so its first entry is attacker-controlled.

## Private delivery

**Signed cookies, not signed URLs.** A PMTiles archive is many range requests
to one URL. Cookies keep that URL stable and cacheable, and CloudFront validates
them natively.

**Tenant-wide cookie scope.** `/tiles/private/{tenant}/*` (admins get
`/tiles/private/*`). Per-dataset scope would add re-issuance without reducing
what a member may see anyway.

**Cookie format checked against botocore.** CloudFront signs literal policy
bytes, so AWS's reference signer is the test oracle.

**Edge verifier holds only the public key.** It mirrors CloudFront's trusted
key group. The component that is deleted on AWS never holds anything worth
stealing.

## Operability and tests

**Tracing starts at the edge.** The nginx otel image starts traces under
`PROFILE=observability`, and Kafka headers carry `traceparent`. Export is
enabled only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

**The e2e suite respects BetterAuth's sign-in limit.** It signs in once per
user and waits out a `429`. Loosening a real control for tests tends to leak
into deployed configuration.

**Chaos scenarios are e2e tests.** `make chaos-*` runs them. They recreate the
worker from the image it is running, not by rebuilding.

**The map starts from the archive's own metadata; the version's spec
overrides it.** Tippecanoe writes `vector_layers` (layers, zooms, fields) and
`tilestats` (geometry, numeric min/max) into every archive, so the map page
reads them with `PMTiles.getMetadata()` (the same instance serves the tiles,
so no extra header fetch) and builds a base spec. The published spec is merged
over it by layer name: zoom windows, geometry, labels, colour field and ranges
from the spec win, while layers only the archive lists are still drawn. A
version published without a spec therefore still renders, in a solid colour.

**Live job status is SSE from the backend, fed by Kafka.** Each backend
process tails `publication.requested` and `publication.results` in its own
throwaway consumer group, starting at `latest` and never committing, so every
replica sees every event. It fans `(tenant_id, job_id)` out to that tenant's
open `GET /publications/events` streams, and each stream re-reads the job row
before sending it, so the browser only gets tenant-scoped database rows, never
raw event payload. SSE is plain HTTP, so it goes through the existing streaming
gateway and edge unchanged. WebSocket would have needed upgrade handling in
both. A 15 s keepalive stays under the gateway's 30 s read timeout. Streams
end after 5 minutes and the browser's `EventSource` reconnects through the
gateway, which re-checks the session; that bounds how long a revoked session
keeps receiving events. The alternative, polling from the page, keeps working
with no Kafka at all but is not live.

## Out of scope (future work)

- A `RUNNING` event. The worker's claim emits nothing, so the live stream shows
  `PENDING` and then the terminal state.
- Shared (Redis) rate-limit state.
- A version-retirement API; the runbook shows the SQL.
- Changing a dataset's visibility, which means copying objects between prefixes.
- Postgres row-level security as a second tenant control.
