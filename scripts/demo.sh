#!/usr/bin/env bash
# End-to-end demonstration, driven entirely through the public edge.
#
#   ./scripts/demo.sh [path/to/file.pmtiles] [dataset-slug]
#   VISIBILITY=private ./scripts/demo.sh ...     publish a private dataset
#   DEMO_API_KEY=pmp_... ./scripts/demo.sh ...   authenticate as the producer
#
# It does exactly what a real client does:
#   1. ask the API for a presigned PUT into staging,
#   2. upload the archive through the edge (the object store is not reachable
#      from the host, and the signature is bound to the SHA-256),
#   3. POST /publications with an Idempotency-Key,
#   4. poll the job until it is terminal,
#   5. verify the published archive answers a Range request with 206,
#   6. print the map URL.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8080}"
API="${BASE_URL}/api/v1"

FILE="${1:-}"
SLUG="${2:-forest-crowns}"
SPEC_FILE="${SPEC_FILE:-${REPO_ROOT}/sample-data/input_forest_crowns_pmtiles.spec.json}"
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

# Prefer the real sample; fall back to a synthetic archive so the demo works on
# a fresh clone (the .pmtiles files are deliberately not committed).
if [[ -z "$FILE" ]]; then
    if [[ -f "${REPO_ROOT}/sample-data/input_h3_multires.pmtiles" ]]; then
        FILE="${REPO_ROOT}/sample-data/input_h3_multires.pmtiles"
    else
        FILE="${REPO_ROOT}/sample-data/synthetic.pmtiles"
        if [[ ! -f "$FILE" ]]; then
            echo "no sample archive found; generating one..."
            "${REPO_ROOT}/scripts/make-synthetic-pmtiles.sh" "$FILE" >/dev/null
        fi
    fi
fi
[[ -f "$FILE" ]] || { echo "no such file: $FILE" >&2; exit 1; }

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# `Origin` is not decoration: BetterAuth rejects a state-changing request
# without it (CSRF), and a browser always sends one. `x-api-key` is used
# instead of the cookie jar when DEMO_API_KEY is set, which is how a producing
# pipeline would authenticate.
api() {
    if [[ -n "${DEMO_API_KEY:-}" ]]; then
        curl -sS -H "x-api-key: ${DEMO_API_KEY}" -H "Origin: ${BASE_URL}" "$@"
    else
        curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -H "Origin: ${BASE_URL}" "$@"
    fi
}
json() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

# --------------------------------------------------------------------------
# 0. Authenticate, if the auth service is wired up.
#    Before phase 4 the backend runs with a fixed development identity and this
#    step is skipped.
# --------------------------------------------------------------------------
EMAIL="${DEMO_EMAIL:-alice@tenant-a.test}"
PASSWORD="${DEMO_PASSWORD:-demo-password-alice}"
if [[ -n "${DEMO_API_KEY:-}" ]]; then
    say "Authenticating with the producer API key"
elif curl -sfo /dev/null "${BASE_URL}/api/auth/ok" 2>/dev/null; then
    say "Signing in as ${EMAIL}"
    STATUS="$(api -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/api/auth/sign-in/email" \
        -H 'content-type: application/json' \
        -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")"
    [[ "$STATUS" == "200" ]] || {
        echo "sign-in failed with HTTP ${STATUS} — run 'make seed' first" >&2
        exit 1
    }
    echo "  signed in"
else
    echo "auth service not routed; using the backend's development identity"
fi

# --------------------------------------------------------------------------
# 1. Presigned upload
# --------------------------------------------------------------------------
SHA_HEX="$(sha256sum "$FILE" | cut -d' ' -f1)"
SHA_B64="$(python3 -c "import base64;print(base64.b64encode(bytes.fromhex('${SHA_HEX}')).decode())")"
SIZE="$(stat -c%s "$FILE")"

say "Publishing $(basename "$FILE") ($(numfmt --to=iec "$SIZE")B, sha256 ${SHA_HEX:0:16}…)"

UPLOAD="$(api -X POST "${API}/demo/uploads" -H 'content-type: application/json' \
    -d "{\"sha256\":\"${SHA_HEX}\",\"content_length\":${SIZE}}")"
SOURCE_KEY="$(echo "$UPLOAD" | json "['source_key']")"
UPLOAD_URL="$(echo "$UPLOAD" | json "['url']")"
echo "  staging key: ${SOURCE_KEY}"

# The URL was signed against the internal object store and rewritten onto the
# edge origin; nginx forwards it with the original Host so SigV4 still verifies.
echo "  uploading through the edge..."
# The PUT goes straight to the object store through the edge; it carries the
# presigned signature, not the caller's session.
STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$UPLOAD_URL" \
    -H 'content-type: application/vnd.pmtiles' \
    -H "x-amz-checksum-sha256: ${SHA_B64}" \
    --data-binary "@${FILE}")"
[[ "$STATUS" == "200" ]] || { echo "upload failed with HTTP ${STATUS}" >&2; exit 1; }
echo "  uploaded"

# --------------------------------------------------------------------------
# 2. Request publication
# --------------------------------------------------------------------------
SPEC_ARG='null'
[[ -f "$SPEC_FILE" ]] && SPEC_ARG="$(cat "$SPEC_FILE")"

BODY="$(python3 - "$SLUG" "$SOURCE_KEY" "$SPEC_ARG" "${VISIBILITY:-public}" <<'PY'
import json, sys
slug, source_key, spec_raw, visibility = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = {
    "dataset_slug": slug,
    "source_key": source_key,
    "name": "Forest crowns density",
    "visibility": visibility,
}
if spec_raw != "null":
    body["spec"] = json.loads(spec_raw)
print(json.dumps(body))
PY
)"

IDEMPOTENCY_KEY="${IDEMPOTENCY_KEY:-demo-$(date +%s)-$RANDOM}"
say "POST /publications  (Idempotency-Key: ${IDEMPOTENCY_KEY})"
ACCEPTED="$(api -X POST "${API}/publications" \
    -H 'content-type: application/json' \
    -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
    -d "$BODY")"
JOB_ID="$(echo "$ACCEPTED" | json "['job_id']")"
DATASET_ID="$(echo "$ACCEPTED" | json "['dataset_id']")"
echo "  job ${JOB_ID} accepted (202)"

# Callers that want to drive the job themselves (the chaos scripts) stop here.
if [[ "${DEMO_WAIT:-1}" == "0" ]]; then
    echo "  dataset ${DATASET_ID}"
    exit 0
fi

# --------------------------------------------------------------------------
# 3. Poll to a terminal state
# --------------------------------------------------------------------------
say "Waiting for the worker"
for _ in $(seq 1 60); do
    JOB="$(api "${API}/publications/${JOB_ID}")"
    STATUS="$(echo "$JOB" | json "['status']")"
    printf '\r  status: %-10s' "$STATUS"
    [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" ]] && break
    sleep 1
done
echo

if [[ "$STATUS" != "SUCCEEDED" ]]; then
    echo "$JOB" | python3 -m json.tool >&2
    exit 1
fi
RESULT="$(echo "$JOB" | json "['result']")"
SEQ="$(echo "$JOB" | json "['result_version_seq']")"
echo "  ${STATUS} — result=${RESULT}, version ${SEQ}"

# --------------------------------------------------------------------------
# 4. Verify the published archive over HTTP range requests
# --------------------------------------------------------------------------
say "Verifying tile delivery"
CURRENT="$(api "${API}/datasets/${DATASET_ID}/current")"
TILE_URL="$(echo "$CURRENT" | json "['url']")"

# A private archive needs the signed tile cookies first — exactly what the map
# page does before its first range request.
if [[ "$(echo "$CURRENT" | json "['visibility']")" == "private" ]]; then
    api -o /dev/null -X POST "${API}/tiles/session"
    echo "  obtained signed tile cookies for the private archive"
fi
RANGE_STATUS="$(api -o /dev/null -w '%{http_code}' -r 0-126 "${BASE_URL}${TILE_URL}")"
echo "  GET ${TILE_URL}"
echo "  Range: bytes=0-126 -> HTTP ${RANGE_STATUS}"
[[ "$RANGE_STATUS" == "206" ]] || { echo "expected 206 Partial Content" >&2; exit 1; }

api -r 0-126 "${BASE_URL}${TILE_URL}" | head -c 7 | grep -q PMTiles \
    && echo "  first 127 bytes are a PMTiles v3 header"

say "Done"
echo "  Map:      ${BASE_URL}/map.html?dataset=${DATASET_ID}"
echo "  Datasets: ${BASE_URL}/"
