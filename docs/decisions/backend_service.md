# Backend service

The catalogue API (`services/backend/`, FastAPI). It serves every `/api/v1/*`
method except auth and tile bytes, and trusts only the gateway's internal JWT.

- **Datasets** — list/get, versions, `/current` (30 s cacheable), rollback
  (`PUT` moves the current-version pointer). Every query is tenant-scoped.
- **Publications** — `POST /publications` creates the dataset (by tenant +
  slug) and a `PENDING` job, then produces `publication.requested`.
  - `Idempotency-Key` is required; a replay returns the same job.
  - The source key must be under the caller's staging prefix.
  - The job is **committed before** the event is produced; a lost event is
    recovered by the worker's reconciler.
  - `POST /publications/{id}/retry` re-queues a failed job.
- **Tile session** — `POST /tiles/session` issues CloudFront signed cookies
  scoped to `/tiles/private/{tenant}/*` (all tenants for platform admins).
- **Live job events** — each pod tails `publication.requested` and
  `publication.results` in a throwaway consumer group and fans changes out to
  the tenant's SSE streams, re-reading the job row before sending.
- **Demo upload** — presigned PUT to the staging bucket, bound to the file's
  sha256 (`DEMO_UPLOAD_ENABLED` only).

The backend never writes versions (`SELECT` only on `dataset_versions`); the
worker owns the versioning rules.

## How it works

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant BE as Backend
    participant DB as Postgres
    participant K as Kafka
    C->>BE: POST /publications (Idempotency-Key, source_key, spec)
    BE->>BE: source_key under staging/{tenant}/ ?
    BE->>DB: find job by (tenant_id, Idempotency-Key)
    alt key already used
        DB-->>BE: existing job
        BE-->>C: 202 same job (idempotent_replay: true)
    else new
        BE->>DB: upsert dataset, insert job PENDING, COMMIT
        BE->>K: produce publication.requested (key = dataset_id)
        Note right of K: if this fails, the PENDING row<br/>is re-emitted by the reconciler
        BE-->>C: 202 {job_id, PENDING}
    end
```

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant BE as Backend pod
    participant K as Kafka
    participant DB as Postgres
    B->>BE: GET /publications/events (SSE)
    K-->>BE: publication.results {tenant_id, job_id}
    BE->>DB: SELECT job WHERE tenant_id = caller
    BE-->>B: event: job {...}
    Note over B,BE: 15 s keepalive, stream ends after 5 min,<br/>EventSource reconnects through the gateway
```

## Alternatives

| | Pros | Cons |
|---|---|---|
| **Chosen: async API + Kafka + separate worker** | API latency independent of file size; workers scale on lag; crash-safe via claim/lease/reconciler | More moving parts (broker, outbox, reconciler); eventual consistency visible to clients |
| **Synchronous publish in the API** (validate, hash, copy inside the request) | Simplest possible flow; immediate result, no broker | Multi-GB copies hold HTTP connections and pods; timeouts at the CDN/ALB (60 s); retries duplicate work; API scaling tied to processing |
| **Serverless: API Gateway + Lambda + Step Functions** | Pay per use, scales to zero; Step Functions gives retries and visibility for free | Lambda 15-min / memory limits for large archives; harder local parity; SSE needs another channel (AppSync/WebSocket API) |

## Cost

| Option | Estimate (eu-west-1) |
|---|---|
| Chosen: 2–6 pods (HPA) | Idle ≈ $7/month of node capacity (2 × 100m / 256 Mi); shares RDS db.t4g.micro Multi-AZ ≈ $31/month with auth and worker; RDS Proxy (recommended in prod) + ≈ $22/month |
| Synchronous API | No broker/worker, but API pods sized for copies (≈ 500m / 512 Mi each, ≈ $17/month per pod) and still needs the same RDS |
| Lambda + Step Functions | Lambda ≈ $0.0000167/GB-s; Step Functions $0.025/1k transitions → ≈ $1–5/month at demo volume; replaces EKS nodes + control plane (≈ $210/month) but not RDS |
