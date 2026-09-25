#!/usr/bin/env bash
# End-to-end demonstration, driven entirely through the public edge, as the
# tenant-a producer (the API key `make seed` writes to dev-keys/producer-api-key).
#
#   ./scripts/demo.sh [path/to/file.pmtiles] [dataset-slug]
#   VISIBILITY=private ./scripts/demo.sh ...     publish a private dataset
#
# It does exactly what a producing pipeline does:
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
VISIBILITY="${VISIBILITY:-public}"
SPEC_FILE="${SPEC_FILE:-${REPO_ROOT}/sample-data/input_forest_crowns_pmtiles.spec.json}"
KEY_FILE="${REPO_ROOT}/dev-keys/producer-api-key"

[[ -s "$KEY_FILE" ]] || { echo "no producer API key at $KEY_FILE — run 'make seed' first" >&2; exit 1; }
API_KEY="$(<"$KEY_FILE")"

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

# The cookie jar only holds the signed tile cookies of a private dataset.
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
api() { curl -sS -H "x-api-key: ${API_KEY}" -b "$COOKIE_JAR" -c "$COOKIE_JAR" "$@"; }
json() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

# --------------------------------------------------------------------------
# 1. Presigned upload
# --------------------------------------------------------------------------
read -r SHA_HEX SHA_B64 SIZE < <(python3 - "$FILE" <<'PY'
import base64, hashlib, os, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest(), base64.b64encode(h.digest()).decode(), os.path.getsize(sys.argv[1]))
PY
)

say "Publishing $(basename "$FILE") (${SIZE} bytes, sha256 ${SHA_HEX:0:16}…) as ${VISIBILITY}"

UPLOAD="$(api -X POST "${API}/demo/uploads" -H 'content-type: application/json' \
    -d "{\"sha256\":\"${SHA_HEX}\",\"content_length\":${SIZE}}")"
SOURCE_KEY="$(echo "$UPLOAD" | json "['source_key']")"
UPLOAD_URL="$(echo "$UPLOAD" | json "['url']")"
echo "  staging key: ${SOURCE_KEY}"

# The URL was signed against the internal object store and rewritten onto the
# edge origin; nginx forwards it with the original Host so SigV4 still verifies.
# The PUT carries the presigned signature, not the caller's credentials.
STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$UPLOAD_URL" \
    -H 'content-type: application/vnd.pmtiles' \
    -H "x-amz-checksum-sha256: ${SHA_B64}" \
    --data-binary "@${FILE}")"
[[ "$STATUS" == "200" ]] || { echo "upload failed with HTTP ${STATUS}" >&2; exit 1; }
echo "  uploaded through the edge"

# --------------------------------------------------------------------------
# 2. Request publication
# --------------------------------------------------------------------------
BODY="$(python3 - "$SLUG" "$SOURCE_KEY" "$SPEC_FILE" "$VISIBILITY" <<'PY'
import json, os, sys
slug, source_key, spec_file, visibility = sys.argv[1:5]
body = {"dataset_slug": slug, "source_key": source_key, "name": slug, "visibility": visibility}
if os.path.isfile(spec_file):
    with open(spec_file) as f:
        body["spec"] = json.load(f)
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
echo "  ${STATUS} — result=$(echo "$JOB" | json "['result']"), version $(echo "$JOB" | json "['result_version_seq']")"

# --------------------------------------------------------------------------
# 4. Verify the published archive over HTTP range requests
# --------------------------------------------------------------------------
say "Verifying tile delivery"
CURRENT="$(api "${API}/datasets/${DATASET_ID}/current")"
TILE_URL="$(echo "$CURRENT" | json "['url']")"

# A private archive needs the signed tile cookies first — exactly what the map
# page does before its first range request. The dataset's own visibility, not
# the requested one: an existing dataset keeps the visibility it was created with.
if [[ "$(echo "$CURRENT" | json "['visibility']")" == "private" ]]; then
    api -o /dev/null -X POST "${API}/tiles/session"
    echo "  obtained signed tile cookies for the private archive"
fi
RANGE_STATUS="$(api -o /dev/null -w '%{http_code}' -r 0-126 "${BASE_URL}${TILE_URL}")"
echo "  GET ${TILE_URL}"
echo "  Range: bytes=0-126 -> HTTP ${RANGE_STATUS}"
[[ "$RANGE_STATUS" == "206" ]] || { echo "expected 206 Partial Content" >&2; exit 1; }

say "Done"
echo "  Map:      ${BASE_URL}/map.html?dataset=${DATASET_ID}"
echo "  Datasets: ${BASE_URL}/"
