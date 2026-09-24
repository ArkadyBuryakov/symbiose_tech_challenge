#!/bin/sh
# Create the staging and publish buckets and the per-service access keys.
# Idempotent: re-running only reconciles policies.
#
# On AWS this is Terraform: two S3 buckets, a CloudFront Origin Access Control
# on the publish bucket, and IAM roles for service accounts instead of the
# MinIO users below. See docs/aws-mapping.md.
set -eu

ALIAS=local
ENDPOINT="${S3_INTERNAL_ENDPOINT:-http://s3:9000}"
STAGING="${STAGING_BUCKET:-staging}"
PUBLISH="${PUBLISH_BUCKET:-publish}"

echo "waiting for ${ENDPOINT}"
until mc alias set "$ALIAS" "$ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
    sleep 1
done

mc mb --ignore-existing "$ALIAS/$STAGING"
mc mb --ignore-existing "$ALIAS/$PUBLISH"

# ---------------------------------------------------------------------------
# Publish bucket: readable without credentials *from inside the compose
# network only*. The S3 port is never published to the host, so nginx (`edge`)
# is the sole route in, and it is what enforces the signed-cookie check for
# private tiles.
#
# This mirrors the AWS setup, where the bucket has no public access at all and
# an Origin Access Control lets exactly one CloudFront distribution read it.
# ---------------------------------------------------------------------------
mc anonymous set download "$ALIAS/$PUBLISH"

# Staging is never readable without credentials: clients reach it only through
# short-lived presigned PUTs.
mc anonymous set none "$ALIAS/$STAGING" || true

# Staged uploads are scratch space; the published copy is the durable one.
cat >/tmp/staging-lifecycle.json <<JSON
{
  "Rules": [
    {
      "ID": "expire-staged-uploads",
      "Status": "Enabled",
      "Expiration": { "Days": 7 },
      "Filter": { "Prefix": "" }
    }
  ]
}
JSON
mc ilm rule import "$ALIAS/$STAGING" </tmp/staging-lifecycle.json || \
    echo "note: lifecycle rule not applied (non-fatal)"

# ---------------------------------------------------------------------------
# Least-privilege access keys.
# ---------------------------------------------------------------------------
cat >/tmp/policy-backend.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PresignStagingUploads",
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": ["arn:aws:s3:::${STAGING}/*"]
    },
    {
      "Sid": "InspectStagedObject",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::${STAGING}/*"]
    }
  ]
}
JSON

cat >/tmp/policy-worker.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadStagedObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::${STAGING}", "arn:aws:s3:::${STAGING}/*"]
    },
    {
      "Sid": "WritePublishedObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket",
                 "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": ["arn:aws:s3:::${PUBLISH}", "arn:aws:s3:::${PUBLISH}/*"]
    }
  ]
}
JSON

ensure_user() {
    access="$1"; secret="$2"; policy_name="$3"; policy_file="$4"
    mc admin policy create "$ALIAS" "$policy_name" "$policy_file" >/dev/null 2>&1 || \
        mc admin policy create "$ALIAS" "$policy_name" "$policy_file" >/dev/null 2>&1 || true
    mc admin user add "$ALIAS" "$access" "$secret" >/dev/null 2>&1 || \
        echo "user $access already exists"
    mc admin policy attach "$ALIAS" "$policy_name" --user "$access" >/dev/null 2>&1 || true
    echo "access key '$access' -> policy '$policy_name'"
}

ensure_user "$BACKEND_S3_ACCESS_KEY" "$BACKEND_S3_SECRET_KEY" pmp-backend /tmp/policy-backend.json
ensure_user "$WORKER_S3_ACCESS_KEY"  "$WORKER_S3_SECRET_KEY"  pmp-worker  /tmp/policy-worker.json

echo "--- buckets ---"
mc ls "$ALIAS"
echo "s3-init: done"
