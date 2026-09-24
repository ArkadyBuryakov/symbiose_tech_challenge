#!/usr/bin/env bash
# Chaos scenario: the same publication message is delivered twice.
#
# This is the everyday case in an at-least-once system — a consumer crash
# between doing the work and committing the offset, a producer retry, a
# rebalance. The platform must end up with exactly one version either way.
#
# The second delivery is a verbatim replay of the first message, produced
# straight onto the topic, so it bypasses the API's idempotency key entirely and
# tests the *worker's* claim instead.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8080}"
API="${BASE_URL}/api/v1"
# A fresh dataset each run, so the first publish is always a CREATED version
# and the assertion is about the duplicate rather than about deduplication.
SLUG="${SLUG:-chaos-duplicate-$(date +%s)}"
TOPIC="${KAFKA_TOPIC_PUBLICATION_REQUESTED:-publication.requested}"

cd "$REPO_ROOT"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
json() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

say "1. Publish once, normally"
OUT="$(SLUG="$SLUG" ./scripts/demo.sh "${1:-}" "$SLUG" 2>&1)" || { echo "$OUT"; exit 1; }
DATASET_ID="$(grep -oE 'dataset=[0-9a-f-]+' <<<"$OUT" | tail -1 | cut -d= -f2)"
echo "$OUT" | grep -E 'result=|Range:'

BEFORE="$(curl -sS "${API}/datasets/${DATASET_ID}/versions")"
COUNT_BEFORE="$(echo "$BEFORE" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
echo "  versions after the first publish: ${COUNT_BEFORE}"

say "2. Replay the last publication.requested message verbatim"
# `-o :end` reads the whole topic and exits, rather than blocking for more.
MESSAGE="$(docker compose exec -T kafka \
    rpk topic consume "$TOPIC" -o :end -f '%v\n' 2>/dev/null \
    | grep "$DATASET_ID" | tail -1)"
[[ -n "$MESSAGE" ]] || { echo "could not find the original message on ${TOPIC}" >&2; exit 1; }
echo "  replaying job $(echo "$MESSAGE" | json "['job_id']")"

# rpk reads newline-delimited records; the trailing newline is required.
printf '%s\n' "$MESSAGE" | docker compose exec -T kafka \
    rpk topic produce "$TOPIC" -k "$DATASET_ID"

say "3. Wait for the worker to process the duplicate"
sleep 6

AFTER="$(curl -sS "${API}/datasets/${DATASET_ID}/versions")"
COUNT_AFTER="$(echo "$AFTER" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')"
echo "  versions after the replay:        ${COUNT_AFTER}"

docker compose logs worker --since 30s 2>/dev/null \
    | grep -o '"event": "worker.job_not_claimable".*"reason": "[^"]*"' | tail -1 \
    | sed 's/^/  worker said: /' || true

say "Result"
if [[ "$COUNT_BEFORE" == "$COUNT_AFTER" ]]; then
    echo "  PASS — the duplicate delivery created no new version (${COUNT_AFTER} total)."
    echo "  The claim is a conditional UPDATE, so the second delivery had nothing to claim."
else
    echo "  FAIL — version count went from ${COUNT_BEFORE} to ${COUNT_AFTER}." >&2
    exit 1
fi
