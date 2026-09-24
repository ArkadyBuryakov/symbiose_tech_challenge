# Runbook

Commands assume the local stack (`make psql`, `docker compose exec ...`). On
AWS the same SQL runs against RDS through a bastion or `kubectl exec` into a
migration pod, and the Kafka commands run through `kafka-*.sh` or the MSK
console. Every section starts from a symptom.

Useful at all times:

```bash
make status                 # container health
make logs s=worker          # structured JSON; pipe to `jq` to filter
make psql                   # catalogue shell (catalog schema)
make dlq                    # print the dead-letter topic
open http://localhost:8081  # Redpanda Console: topics, lag, messages
```

Every log line carries `request_id`, `trace_id` and, where known, `job_id`,
`dataset_id` and `tenant_id`. Start from whichever of those the user gave you:

```bash
docker compose logs --no-log-prefix worker backend gateway | jq -c 'select(.job_id=="<id>")'
```

---

## A job is stuck in `PENDING` or `RUNNING`

**What normally happens.** The reconciler re-emits `PENDING` jobs older than
`WORKER_STUCK_PENDING_SECONDS` (60 s) and `RUNNING` jobs whose lease has
expired (`WORKER_LEASE_SECONDS`, 60 s, renewed every 20 s while the job runs).
A stuck job usually recovers by itself within about two minutes.

**Look.**

```sql
SELECT id, status, attempts, lease_owner, lease_expires_at, updated_at, error_code
FROM catalog.publication_jobs
WHERE status IN ('PENDING', 'RUNNING')
ORDER BY updated_at;
```

* `PENDING`, old `updated_at` → the request message was never produced (the
  backend crashed or Kafka was down between commit and produce). Check that the
  reconciler is running: `make logs s=worker | grep reconciler`.
* `RUNNING`, `lease_expires_at` in the **future**, and it keeps moving → a live
  worker is processing it (a large file). Check `worker_inflight_jobs` and the
  worker's logs for that `job_id`.
* `RUNNING`, `lease_expires_at` in the **past** → the owner died. The next
  reconciler pass (≤ 30 s) re-emits it.

**Act.** Usually nothing. To force immediate recovery of an orphaned job,
expire its lease; the reconciler then re-emits it on its next pass:

```sql
UPDATE catalog.publication_jobs
SET lease_expires_at = now() - interval '1 second'
WHERE id = '<job_id>' AND status = 'RUNNING';
```

Never set a `RUNNING` job straight back to `PENDING` while its owner might
still be alive — the lease is the only thing stopping two workers publishing it
at once. (It would still be safe, because the copy is content-addressed and the
version insert is guarded by a unique index, but it is wasted work.)

---

## Messages in the dead-letter queue

**What it means.** A job hit a *transient* error (storage, database, network)
`WORKER_MAX_ATTEMPTS` times in a row. It is `FAILED` with
`error_code = MAX_ATTEMPTS_EXCEEDED`. Permanent errors (bad file, missing
source) never go to the DLQ — they fail the job directly.

**Inspect.**

```bash
make dlq
# or, with headers:
docker compose exec kafka rpk topic consume publication.requested.dlq -o :end \
  -f 'key=%k error=%h{x-error-code} detail=%h{x-error-detail}\n%v\n\n'
```

The `x-error-code` header is the underlying cause (usually `STORAGE_ERROR` or
`DATABASE_ERROR`); `x-error-detail` is the last error message. Grafana's
*DLQ (1h)* stat and `dlq_messages_total` show when it started.

**Replay** once the cause is fixed. The supported path is the API, which
resets the job and re-emits it:

```bash
curl -X POST http://localhost:8080/api/v1/publications/<job_id>/retry \
  -H 'Origin: http://localhost:8080' -b cookies.txt       # or -H 'x-api-key: ...'
```

To replay many at once, re-produce the DLQ payloads onto the main topic. This is
safe: the worker's claim only succeeds for a job that is claimable, so a
message for a job that has since succeeded is a no-op. First reset the jobs:

```sql
UPDATE catalog.publication_jobs
SET status = 'PENDING', error_code = NULL, error_message = NULL
WHERE status = 'FAILED' AND error_code = 'MAX_ATTEMPTS_EXCEEDED';
```

then let the reconciler pick them up (≤ 90 s), or re-produce explicitly:

```bash
docker compose exec kafka sh -c \
  "rpk topic consume publication.requested.dlq -o :end -f '%k %v\n' \
   | while read -r key value; do printf '%s\n' \"\$value\" | rpk topic produce publication.requested -k \"\$key\"; done"
```

---

## A job `FAILED` and the user wants it retried

Check `error_code` first — it says whether a retry can possibly help:

| `error_code` | Meaning | Fix before retrying |
|---|---|---|
| `INVALID_PMTILES`, `UNSUPPORTED_PMTILES_VERSION`, `EMPTY_SOURCE` | The staged bytes are not a PMTiles v3 archive | Re-export and re-stage. A new upload plus a new `POST /publications` is usually simpler than overwriting. |
| `SOURCE_NOT_FOUND` | Nothing at `source_key` (typo, or expired by the 7-day staging lifecycle) | Stage the file again |
| `SOURCE_FORBIDDEN` | The worker's credentials cannot read staging | IAM / bucket policy |
| `MAX_ATTEMPTS_EXCEEDED` | Transient errors, retries exhausted | Fix the dependency (see *DLQ* above) |

Then `POST /api/v1/publications/{job_id}/retry` (only `FAILED` jobs are
retryable; anything else is `409`). The datasets page has a *Retry* button.

---

## Roll a dataset back

Rollback is a pointer move — instant, no data copied, fully reversible.

**UI:** datasets page → *Versions* → *Make current* on the version to restore.

**API** (tenant `owner`/`admin`, or platform admin):

```bash
curl -X PUT http://localhost:8080/api/v1/datasets/<dataset_id>/current \
  -H 'content-type: application/json' -H 'Origin: http://localhost:8080' \
  -b cookies.txt -d '{"seq": 3}'
```

Clients see the change within 30 s (the `Cache-Control` on
`/datasets/{id}/current`). The tile archives are immutable and content
addressed, so no cache purge is needed — the old version's URL was never
changed, it simply becomes current again.

To stop a version from ever being served again, mark it `RETIRED` (a later
upload of the same bytes then gets a *new* version number rather than
resurrecting it):

```sql
UPDATE catalog.dataset_versions SET status = 'RETIRED'
WHERE dataset_id = '<dataset_id>' AND seq = <n>;
```

Retire only a version that is not current — move the pointer first.

---

## A worker crashed (or keeps crashing)

**One crash** needs no action. The job it held is `RUNNING` with a lease that
stops being renewed; within `WORKER_LEASE_SECONDS` (60 s) plus one reconciler
pass (30 s) it is re-emitted and picked up by another worker. The re-run skips
the S3 copy if the object is already in place and produces the same version. The
Kafka offset was never committed, so the original message is also redelivered
to whichever consumer takes over the partition; it finds nothing to claim.

**Crash loop.**

```bash
make logs s=worker | jq -c 'select(.level=="error" or .level=="critical")' | tail
docker compose ps worker            # restarts count
```

* `worker.chaos_enabled` in the logs → a `CHAOS_*` variable is set. Unset it.
* Configuration error at startup → the process exits immediately with a
  pydantic validation message naming the variable.
* A specific message crashes it every time (a poison message) → it will be
  re-delivered after every restart. Find its `job_id` in the last log lines
  before each exit, mark the job `FAILED`, and the next delivery becomes a no-op:

  ```sql
  UPDATE catalog.publication_jobs
  SET status='FAILED', error_code='INTERNAL_ERROR', error_message='poison message, see incident'
  WHERE id = '<job_id>';
  ```

Workers are safe to restart or scale at any time: `SIGTERM` stops polling, lets
the in-flight job finish within `WORKER_SHUTDOWN_GRACE_SECONDS`, flushes the
producer, drains the outbox once more and leaves the consumer group cleanly.

---

## The outbox backlog is growing

**What it means.** Result events are committed to `catalog.outbox` but not
reaching Kafka. Nothing is lost — they are durable in Postgres — but consumers of
`publication.results` are behind.

```sql
SELECT count(*), min(created_at), max(attempts)
FROM catalog.outbox WHERE sent_at IS NULL;
```

* `attempts` climbing → the relay is trying and Kafka is refusing. Check broker
  health (`docker compose exec kafka rpk cluster health`) and the worker logs for
  `outbox.produce_failed`.
* `attempts` all zero → no relay is running. The relay is a thread inside every
  worker; if all workers are down, so is the relay. Start a worker.

The relay drains at full speed once Kafka is back (it only sleeps when a batch
comes back short). Sent rows are kept for audit; trim periodically:

```sql
DELETE FROM catalog.outbox WHERE sent_at < now() - interval '7 days';
```

---

## The auth service is down

**What users see.**

* Anonymous reads of public datasets and public tiles keep working — the
  gateway's `optional` routes only call auth when a credential is present, and
  tiles never touch the gateway.
* Signed-in users keep working for up to `GATEWAY_VERIFY_CACHE_TTL` (10 s) on
  cached verifications. After that, authenticated API calls get **`503`
  `auth-unavailable`** — *not* `401`. The gateway distinguishes "cannot tell who
  you are" from "you are nobody", so clients do not discard valid sessions.
* Private tiles keep working until each user's signed cookie expires (≤ 10 min);
  refreshing a cookie needs auth, so it fails after that.
* The gateway's `/readyz` fails, which takes it out of the load balancer on AWS.

**Act.** `make logs s=auth`. The usual causes are Postgres (the auth service's
readiness checks it) or a bad `BETTER_AUTH_SECRET_PATH`. Failures are never
cached, so recovery is immediate once auth is back.

---

## Revoke a user's access

1. **End their sessions.** Deleting the session rows takes effect on the API
   within `GATEWAY_VERIFY_CACHE_TTL` (10 s):

   ```sql
   DELETE FROM auth.session
   WHERE "userId" = (SELECT id FROM auth."user" WHERE email = '<email>');
   ```

   For a full lock-out also ban the user (BetterAuth admin plugin), which stops
   new sign-ins:

   ```sql
   UPDATE auth."user" SET banned = true, "banReason" = '<ticket>'
   WHERE email = '<email>';
   ```

2. **Revoke their API keys** (producers):

   ```sql
   UPDATE auth.apikey SET enabled = false
   WHERE "referenceId" = (SELECT id FROM auth."user" WHERE email = '<email>');
   ```

   For the demo producer, `make seed-rotate` deletes and re-mints its key.

3. **Remove tenant membership** if they should keep an account but lose a tenant:

   ```sql
   DELETE FROM auth.member
   WHERE "userId" = (SELECT id FROM auth."user" WHERE email = '<email>')
     AND "organizationId" = '<org_id>';
   ```

**The limit to be aware of.** A signed tile cookie already issued stays valid
until it expires (`TILE_COOKIE_TTL_SECONDS`, 10 minutes). There is no
revocation list for signed cookies — this is inherent to CloudFront's model.
The user cannot obtain a *new* one, so the exposure is bounded by the TTL. If
that is not acceptable for an incident, rotate the cookie-signing key: every
outstanding cookie becomes invalid at once (and every user's map re-requests a
cookie on its next refresh).
