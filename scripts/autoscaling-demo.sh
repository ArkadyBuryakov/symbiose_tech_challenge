#!/usr/bin/env bash
# Worker and node autoscaling on the AWS deployment:
#
#   BASE_URL=https://dXXXX.cloudfront.net ./scripts/autoscaling-demo.sh <email> <password> [jobs]
#   (or `make demo-autoscaling [n=60]`, which takes the first three from .env)
#
#   1. sign in and upload one archive to the staging bucket,
#   2. publish it to [jobs] datasets at once. The Kafka key is the dataset id,
#      so the backlog spreads over all partitions,
#   3. watch KEDA add workers (1 -> 6) and, as they go Pending, the Cluster
#      Autoscaler add nodes (2 -> 4). Needs kubectl on the cluster's context.
#
# The datasets are `scale-1` … `scale-<jobs>`; a re-run publishes new versions
# of the same ones.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${BASE_URL:?set BASE_URL to the CloudFront URL}"
API="${BASE_URL}/api/v1"

EMAIL="${1:?usage: autoscaling-demo.sh <email> <password> [jobs]}"
PASSWORD="${2:?usage: autoscaling-demo.sh <email> <password> [jobs]}"
JOBS="${3:-60}"
for f in input_h3_multires.pmtiles synthetic.pmtiles; do
    [[ -f "${REPO_ROOT}/sample-data/$f" ]] && FILE="${REPO_ROOT}/sample-data/$f" && break
done
[[ -f "${FILE:-}" ]] || { echo "no archive in sample-data/ (try 'make synthetic')" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
JAR="${WORK}/cookies"
ARCHIVE="${WORK}/data.pmtiles"
RUN="$(date +%s)"
cp "$FILE" "$ARCHIVE"
# New bytes on every run, as in aws-demo.sh.
printf 'autoscaling-demo %s' "$RUN" >>"$ARCHIVE"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
api() { curl -sS --fail-with-body -b "$JAR" -c "$JAR" -H "Origin: ${BASE_URL}" "$@"; }
json() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

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
say "Uploading $(basename "$FILE") (${SIZE} bytes)"
UPLOAD="$(api -X POST "${API}/demo/uploads" -H 'content-type: application/json' \
    -d "{\"sha256\":\"${SHA_HEX}\",\"content_length\":${SIZE}}")"
SOURCE_KEY="$(echo "$UPLOAD" | json "['source_key']")"
mapfile -t HEADERS < <(echo "$UPLOAD" | python3 -c \
    'import sys,json;[print(f"-H{k}: {v}") for k,v in json.load(sys.stdin)["headers"].items()]')
STATUS="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "${HEADERS[@]}" \
    --data-binary "@${ARCHIVE}" "$(echo "$UPLOAD" | json "['url']")")"
[[ "$STATUS" == "200" ]] || fail "upload failed with HTTP ${STATUS}"
ok "uploaded to staging: ${SOURCE_KEY}"

# --------------------------------------------------------------------------
say "Requesting ${JOBS} publications"
for i in $(seq 1 "$JOBS"); do
    api -o /dev/null -X POST "${API}/publications" -H 'content-type: application/json' \
        -H "Idempotency-Key: autoscaling-demo-${RUN}-${i}" \
        -d "{\"dataset_slug\":\"scale-${i}\",\"name\":\"Scale ${i}\",\"visibility\":\"private\",\"source_key\":\"${SOURCE_KEY}\"}"
done
ok "${JOBS} jobs queued on publication.requested"

# --------------------------------------------------------------------------
say "Watching workers and nodes every 10 s (Ctrl-C to stop)"
echo "  scale-up: workers within ~30 s, nodes in 2-3 min; scale-down: workers after ~5 min, nodes after ~10 more"
while true; do
    WORKERS="$(kubectl -n pmp get deploy worker -o jsonpath='{.status.readyReplicas}/{.spec.replicas}')"
    PENDING="$(kubectl -n pmp get pods --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l)"
    NODES="$(kubectl get nodes --no-headers | wc -l)"
    echo "  $(date +%T)  workers ready/wanted: ${WORKERS:-0/?}  pending pods: ${PENDING}  nodes: ${NODES}"
    sleep 10
done
