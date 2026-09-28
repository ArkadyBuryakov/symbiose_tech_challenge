# Underlying structure

## Web app

Simple static web app to demo all sides of application. From S3 upload to serving pmtiles.
- / - Datasets
  - Auth form
  - Datasets list
    - public - for both authorized and unauthorized
    - private - tenant-scoped datasets available for authorized users only
- /upload.html - Upload (only when `DEMO_UPLOAD_ENABLED=true`, 404 otherwise)
  - Upload form: pmtiles go to intermediate (staging) S3 via presigned PUT, spec is sent with the publication request
  - Publication jobs - list of jobs with real-time update of their status, retry for failed ones
- /map.html - Map viewer of a dataset's current version, styled from archive metadata and spec

## Edge

Internet-facing entry point (nginx locally, CloudFront on AWS - see `docs/aws-mapping.md`):
- Serves the static web app
- Proxies `/api/*` to API Gateway
- Serves tiles from serving S3 (see Tile serving)
- Proxies presigned staging uploads to S3 (local only)

## API Gateway service

Single entry point for `/api/*`:
- Allowlist routing between backend and auth service (`routes.yaml`), anything else is 404
- Strips client-supplied identity headers
- Checks authentication with auth service (successes cached for 10s)
- Rate limiting per user / client IP
- Mints short-lived internal JWT for backend

## Auth service

Better Auth service to easily support:
- AuthN (sessions) - authorization itself is enforced by gateway and backend
- Tenants (Organizations) with roles
- Platform admins
- API Keys

## Backend service

- Serves methods for web-client, except auth and tile serving
- Creates dataset (by tenant+slug) and publication job, posts processing events to kafka
  - `Idempotency-Key` required, retry returns the same job
  - source key must be under caller's tenant staging prefix
  - job is committed before event is posted, lost events are recovered by worker's reconciler
- Retries failed jobs
- Issues signed cookies for private tiles, scoped to tenant (or all tenants for platform admin)
- Consumes requested/status events and routes them to SSE connections
- Imitates upload to the source S3:
  - issues presigned PUT to intermediate S3 storage, bound to file's sha256
  - browser uploads pmtiles and calls POST-method with spec to run processing job

## Worker

- Processing tasks
  - takes unprocessed events from kafka
  - claims job with conditional update - duplicate events or racing workers are no-op
  - validates pmtiles header and hashes the file
  - copies it to serving S3 under content-addressed key (idempotent)
  - resolves version by archive sha256 + spec sha256
    - if currently active - mark as deduplicated
    - if present (available), but not active - switch active version to resolved one
    - if not present - create new version
  - updates version, dataset pointer, job status and status event (outbox) in one transaction
  - kafka offset committed only after job reached terminal state
  - transient failures retried with backoff, after max attempts job is failed and event goes to DLQ
- Lease heartbeat - keeps running job's lease alive while it is processed
- Reconciler - resilience against interrupted tasks (OOMKilled, Downscaled instance, lost events)
  - finds pending jobs stuck for too long and running jobs with expired lease
  - re-publishes processing task request to kafka, next worker re-claims the job
- Outbox relay - publishes status events from outbox table to kafka

## Kafka

Transport for event-based communications:
- processing job request
  - produced by backend (or worker's reconciler on re-emit), consumed by workers and backend (SSE)
- processing job status
  - produced by worker (via outbox), consumed by backend (SSE)
- dead letter queue
  - produced by worker for failed and undecodable requests

## Tile serving

- Serving S3 bucket without access to internet, `public/` and `private/<tenant>/` prefixes
- Edge serves S3 files (CloudFront as CDN cache on AWS, nginx locally)
  - public datasets served as is
  - private datasets require signed cookie to allow access to tenants subpath (checked by CloudFront on AWS, by edge-verifier locally)
