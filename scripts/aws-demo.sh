#!/usr/bin/env bash
# Private tiles through CloudFront, end to end, on the AWS deployment:
#
#   BASE_URL=https://dXXXX.cloudfront.net ./scripts/aws-demo.sh <email> <password> [file.pmtiles]
#   (or `make aws-demo email=... password=...`, which fills in BASE_URL)
#
#   1. sign in as a tenant user (create one with `make aws-add-user`),
#   2. upload an archive straight to the staging bucket (presigned PUT),
#   3. publish it as a private dataset and wait for the worker,
#   4. with the signed tile cookies: the first range read is a CloudFront miss,
#      and repeating it is soon served from the edge cache (the 3rd read),
#   5. without them: 403, although the bytes are now in the cache.
#
# Archives are content-addressed, so re-publishing the same file would reuse a
# URL that is already cached. A unique nonce is appended to a copy of the file:
# PMTiles readers only follow the header's offsets, so trailing bytes are inert.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${BASE_URL:?set BASE_URL to the CloudFront URL}"
API="${BASE_URL}/api/v1"

EMAIL="${1:?usage: aws-demo.sh <email> <password> [file.pmtiles]}"
PASSWORD="${2:?usage: aws-demo.sh <email> <password> [file.pmtiles]}"
FILE="${3:-}"
if [[ -z "$FILE" ]]; then
    for f in input_h3_multires.pmtiles synthetic.pmtiles; do
        [[ -f "${REPO_ROOT}/sample-data/$f" ]] && FILE="${REPO_ROOT}/sample-data/$f" && break
    done
fi
[[ -f "$FILE" ]] || { echo "no archive given and none in sample-data/ (try 'make synthetic')" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
JAR="${WORK}/cookies"
ARCHIVE="${WORK}/data.pmtiles"
cp "$FILE" "$ARCHIVE"
printf 'aws-demo %s %s' "$(date +%s)" "$RANDOM" >>"$ARCHIVE"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
# Every call reports its timing on stderr; `-w ''` silences it (job polling).
TIMING='%{stderr}  · %{method} %{url_effective} -> %{http_code} in %{time_total}s\n'
api() { curl -sS --fail-with-body -b "$JAR" -c "$JAR" -H "Origin: ${BASE_URL}" -w "$TIMING" "$@"; }
json() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

# Prints "<status> <x-cache> <pop> <first byte s> <total s>" for one range
# read of the archive.
fetch() {
    curl -sS -o /dev/null -D - -r 0-16383 -w 'T %{time_starttransfer} %{time_total}\n' \
        "$@" "$TILE_URL" | tr -d '\r' | awk '
        /^HTTP/ {s=$2} tolower($1)=="x-cache:" {c=$2} tolower($1)=="x-amz-cf-pop:" {p=$2}
        /^T / {f=$2; t=$3}
        END {print s, (c ? c : "-"), (p ? p : "-"), f, t}'
}

# --------------------------------------------------------------------------
say "Signing in to ${BASE_URL} as ${EMAIL}"
api -o /dev/null -X POST "${BASE_URL}/api/auth/sign-in/email" \
    -H 'content-type: application/json' \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"email":sys.argv[1],"password":sys.argv[2]}))' "$EMAIL" "$PASSWORD")"
ok "signed in"

# --------------------------------------------------------------------------
read -r SHA_HEX SIZE < <(python3 - "$ARCHIVE" <<'PY'
import hashlib, os, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest(), os.path.getsize(sys.argv[1]))
PY
)
say "Uploading $(basename "$FILE") + nonce (${SIZE} bytes, sha256 ${SHA_HEX:0:16}…)"
UPLOAD="$(api -X POST "${API}/demo/uploads" -H 'content-type: application/json' \
    -d "{\"sha256\":\"${SHA_HEX}\",\"content_length\":${SIZE}}")"
SOURCE_KEY="$(echo "$UPLOAD" | json "['source_key']")"
mapfile -t HEADERS < <(echo "$UPLOAD" | python3 -c \
    'import sys,json;[print(f"-H{k}: {v}") for k,v in json.load(sys.stdin)["headers"].items()]')
read -r STATUS SECONDS_ SPEED < <(curl -sS -o /dev/null -w '%{http_code} %{time_total} %{speed_upload}\n' \
    -X PUT "${HEADERS[@]}" --data-binary "@${ARCHIVE}" "$(echo "$UPLOAD" | json "['url']")")
[[ "$STATUS" == "200" ]] || fail "upload failed with HTTP ${STATUS}"
ok "uploaded to staging in ${SECONDS_}s ($(awk "BEGIN {printf \"%.1f\", ${SPEED} / 1048576}") MB/s): ${SOURCE_KEY}"

# --------------------------------------------------------------------------
say "Publishing as a private dataset"
ACCEPTED="$(api -X POST "${API}/publications" -H 'content-type: application/json' \
    -H "Idempotency-Key: aws-demo-${SHA_HEX}" \
    -d "{\"dataset_slug\":\"aws-demo\",\"name\":\"AWS demo\",\"visibility\":\"private\",\"source_key\":\"${SOURCE_KEY}\"}")"
JOB_ID="$(echo "$ACCEPTED" | json "['job_id']")"
DATASET_ID="$(echo "$ACCEPTED" | json "['dataset_id']")"
STARTED=$SECONDS
for _ in $(seq 1 120); do
    JOB="$(api -w '' "${API}/publications/${JOB_ID}")"
    STATUS="$(echo "$JOB" | json "['status']")"
    printf '\r  job %s: %-10s' "$JOB_ID" "$STATUS"
    [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" ]] && break
    sleep 1
done
echo
[[ "$STATUS" == "SUCCEEDED" ]] || { echo "$JOB" >&2; fail "job did not succeed"; }
ok "worker finished in ~$((SECONDS - STARTED))s"
CURRENT="$(api "${API}/datasets/${DATASET_ID}/current")"
[[ "$(echo "$CURRENT" | json "['visibility']")" == "private" ]] \
    || fail "dataset 'aws-demo' already exists and is not private"
TILE_URL="${BASE_URL}$(echo "$CURRENT" | json "['url']")"
ok "published: ${TILE_URL}"

# --------------------------------------------------------------------------
say "Fetching through CloudFront with signed tile cookies"
api -o /dev/null -X POST "${API}/tiles/session"
ok "got signed cookies for /tiles/private/"

read -r CODE CACHE POP TTFB TOTAL < <(fetch -b "$JAR")
echo "  1st read: HTTP ${CODE}, x-cache: ${CACHE}, pop: ${POP}, first byte ${TTFB}s, total ${TOTAL}s"
[[ "$CODE" == "206" && "$CACHE" == "Miss" ]] || fail "expected 206 and a cache miss"
ok "cache miss — CloudFront fetched it from S3"

# Measured on this stack: the 2nd read of a new archive is still a Miss however
# long we wait, and the 3rd is a Hit. x-cache reports only the edge location;
# the regional edge cache behind it keeps the bytes from the 1st read, and the
# edge stores them on the 2nd, so S3 is read once.
for n in 2 3 4; do
    read -r CODE CACHE POP2 TTFB TOTAL < <(fetch -b "$JAR")
    echo "  read ${n}: HTTP ${CODE}, x-cache: ${CACHE}, pop: ${POP2}, first byte ${TTFB}s, total ${TOTAL}s"
    [[ "$CACHE" == "Hit" ]] && break
done
[[ "$CODE" == "206" && "$CACHE" == "Hit" ]] \
    || fail "expected 206 and a cache hit$([[ "$POP" != "$POP2" ]] && echo " (answered by another edge: ${POP} -> ${POP2})")"
ok "cache hit on read ${n} — served from the edge"

# --------------------------------------------------------------------------
say "Fetching the same bytes without cookies"
read -r CODE CACHE _ TTFB TOTAL < <(fetch)
echo "  anonymous read: HTTP ${CODE}, x-cache: ${CACHE}, first byte ${TTFB}s, total ${TOTAL}s"
[[ "$CODE" == "403" ]] || fail "expected 403"
ok "denied, although the bytes are in the edge cache"

say "Done"
echo "  Map: ${BASE_URL}/map.html?dataset=${DATASET_ID}"
