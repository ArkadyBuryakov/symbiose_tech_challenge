#!/usr/bin/env bash
# Chaos scenario: the worker dies between copying the object and committing the
# catalogue transaction.
#
# This is the worst moment to crash — the bytes are in the publish bucket but
# nothing in the database knows about them. Recovery must produce exactly the
# same result as an uninterrupted run: one version row, the same content hash,
# the job SUCCEEDED.
#
# What makes that work:
#   * the publish key is the content hash, so the re-run copies to the same
#     place (and skips the copy when it is already there);
#   * nothing was committed, so there is no partial state to reconcile;
#   * the lease expires and the reconciler re-emits the request.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8080}"
API="${BASE_URL}/api/v1"
SLUG="${SLUG:-chaos-crash-$(date +%s)}"
FILE="${1:-}"

cd "$REPO_ROOT"
# Recreate the worker from the image it runs now, and keep its tracing config:
# recomputing the tag from HEAD breaks once a commit lands after `make up`.
GIT_SHA="$(docker compose ps worker --format '{{.Image}}' | head -1 | cut -d: -f2)"
export GIT_SHA
OTEL_EXPORTER_OTLP_ENDPOINT="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' \
    "$(docker compose ps -q worker | head -1)" | sed -n 's/^OTEL_EXPORTER_OTLP_ENDPOINT=//p')"
export OTEL_EXPORTER_OTLP_ENDPOINT

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
json() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

# Job status is tenant-scoped, so this script signs in as the same user the
# publication is made as (demo.sh's default), exactly as a client would.
COOKIE_JAR="$(mktemp)"
EMAIL="${DEMO_EMAIL:-alice@tenant-a.test}"
PASSWORD="${DEMO_PASSWORD:-demo-password-alice}"
api() { curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -H "Origin: ${BASE_URL}" "$@"; }
sign_in() {
    local status
    for _ in 1 2 3 4 5 6; do
        status="$(api -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/api/auth/sign-in/email" \
            -H 'content-type: application/json' \
            -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")"
        [[ "$status" == "200" ]] && return 0
        # BetterAuth rate-limits sign-in (3 per 10s); wait it out rather than fail.
        [[ "$status" == "429" ]] && { sleep 11; continue; }
        echo "sign-in failed with HTTP ${status} — run 'make seed' first" >&2
        exit 1
    done
    echo "sign-in still rate-limited after retries" >&2
    exit 1
}
restore_worker() {
    docker compose up -d --no-build --force-recreate worker >/dev/null 2>&1 || true
    rm -f "$COOKIE_JAR"
}
trap restore_worker EXIT
sign_in

say "1. Restart the worker with CHAOS_CRASH_AFTER_COPY=1"
# A short lease keeps the demonstration quick; the mechanism is identical at
# the default 60s.
CHAOS_CRASH_AFTER_COPY=1 WORKER_LEASE_SECONDS=15 WORKER_RECONCILER_INTERVAL_SECONDS=10 \
    WORKER_STUCK_PENDING_SECONDS=15 \
    docker compose up -d --no-build --force-recreate worker >/dev/null
sleep 3
echo "  worker will exit(92) right after the copy, before the DB commit"

say "2. Publish"
# DEMO_WAIT=0: upload and request publication, but do not wait — the job cannot
# finish while the worker is crash-looping, and waiting is this script's job.
OUT="$(DEMO_WAIT=0 ./scripts/demo.sh "$FILE" "$SLUG" 2>&1)" || { echo "$OUT" >&2; exit 1; }
JOB_ID="$(grep -oE 'job [0-9a-f-]+ accepted' <<<"$OUT" | cut -d' ' -f2 || true)"
DATASET_ID="$(grep -oE '  dataset [0-9a-f-]+' <<<"$OUT" | awk '{print $2}' || true)"
if [[ -z "$JOB_ID" || -z "$DATASET_ID" ]]; then echo "$OUT" >&2; exit 1; fi
echo "  job ${JOB_ID} (it cannot complete while the worker is crashing)"

# Give the crashing worker a moment to pick the job up, copy, and die.
sleep 8

docker compose logs worker --since 60s 2>/dev/null \
    | grep -o '"event": "chaos.crash_after_copy"' | tail -1 \
    | sed 's/^/  worker log: /' || echo "  (crash marker not seen yet)"

say "3. Restart the worker normally"
docker compose up -d --no-build --force-recreate worker >/dev/null
echo "  waiting for the lease to expire and the reconciler to re-emit..."

for i in $(seq 1 60); do
    JOB="$(api "${API}/publications/${JOB_ID}")"
    STATUS="$(echo "$JOB" | json "['status']")"
    printf '\r  t=%02ds status: %-10s' "$((i * 2))" "$STATUS"
    [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" ]] && break
    sleep 2
done
echo

say "4. Check the outcome"
VERSIONS="$(api "${API}/datasets/${DATASET_ID}/versions")"
COUNT="$(echo "$VERSIONS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
CURRENT="$(api "${API}/datasets/${DATASET_ID}/current")"

echo "  job status:    $(echo "$JOB" | json "['status']")"
echo "  job result:    $(echo "$JOB" | json "['result']")"
echo "  attempts:      $(echo "$JOB" | json "['attempts']")"
echo "  version rows:  ${COUNT}"
echo "  current seq:   $(echo "$CURRENT" | json "['seq']")"
echo "  content hash:  $(echo "$CURRENT" | json "['sha256']")"

docker compose logs worker --since 180s 2>/dev/null \
    | grep -o '"event": "worker.copy_skipped".*"reason": "[^"]*"' | tail -1 \
    | sed 's/^/  worker log: /' || true

say "Result"
FAILURES=0
[[ "$(echo "$JOB" | json "['status']")" == "SUCCEEDED" ]] || { echo "  FAIL: job is not SUCCEEDED" >&2; FAILURES=1; }
[[ "$COUNT" == "1" ]] || { echo "  FAIL: expected exactly 1 version row, got ${COUNT}" >&2; FAILURES=1; }
[[ "$(echo "$CURRENT" | json "['seq']")" == "1" ]] || { echo "  FAIL: current version is not seq 1" >&2; FAILURES=1; }

if [[ "$FAILURES" == "0" ]]; then
    echo "  PASS — recovery produced exactly one version with the same content hash,"
    echo "  and the re-run skipped the copy because the object was already in place."
else
    exit 1
fi
