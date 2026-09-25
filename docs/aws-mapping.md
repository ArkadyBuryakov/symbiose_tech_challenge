# From `docker compose` to AWS

The local stack was built so that moving to AWS changes **configuration and
infrastructure**, not application code. This document lists, per component,
what it becomes, which settings change, what IAM it needs, and what is deleted.

Nothing here is implemented yet (no Terraform, Helm or manifests by design);
this is the map for that work.

## Component mapping

| Local | AWS | Notes |
|---|---|---|
| `edge` (nginx) | **CloudFront** distribution | Deleted. Its routes become cache behaviours (below). |
| `edge-verifier` | CloudFront signed-cookie validation | **Deleted.** CloudFront checks the same cookies natively. |
| `gateway` | EKS Deployment (+ HPA) behind an internal ALB | CloudFront → ALB is the `/api/*` origin. |
| `auth` | EKS Deployment | Not routed by CloudFront except `/api/auth/*` via the gateway. |
| `backend` | EKS Deployment (+ HPA) | Reachable only from the gateway (NetworkPolicy). |
| `worker` | EKS Deployment, scaled on consumer lag (KEDA) | One job per pod; replicas ≤ partitions (6). |
| `postgres` | **RDS for PostgreSQL** 16, Multi-AZ | IAM database authentication for the service roles. |
| `kafka` (Redpanda) | **Amazon MSK** (provisioned or Serverless) | IAM auth, TLS. RF 3, `min.insync.replicas=2`. |
| `s3` (MinIO) | **Amazon S3** — two buckets | `staging` (lifecycle expiry), `publish` (OAC-only). |
| `migrate`, `migrate-auth` | Helm `pre-install,pre-upgrade` hook Jobs | Same images, same commands. |
| `kafka-init` | Helm pre-install hook Job (or Terraform `aws_msk_*` topics) | Same script, IAM flags. |
| `s3-init` | Terraform | Buckets, policies, lifecycle, OAC. |
| `dev-keys/` | **Secrets Manager** + Secrets Store CSI driver | Mounted at the *same paths* (`/run/keys/...`). |
| Prometheus / Grafana | Amazon Managed Prometheus / Managed Grafana | Or CloudWatch Container Insights. |
| Jaeger | AWS X-Ray via the ADOT collector | Services already speak OTLP. |

### CloudFront behaviours (what the nginx config becomes)

| Path | Origin | Viewer policy | Cache |
|---|---|---|---|
| `/tiles/public/*` | S3 `publish` via **OAC** | public | `CachingOptimized`; objects are immutable |
| `/tiles/private/*` | S3 `publish` via OAC | **Restrict viewer access** — trusted key group holding the public half of the cookie-signing key | `CachingOptimized`, cookies *not* in the cache key |
| `/api/*` | internal ALB → gateway | public | `CachingDisabled`, except `/api/v1/datasets/*/current` (honours the 30 s `Cache-Control`) |
| `/staging-upload/*` | **not needed** — presign directly against the S3 regional endpoint | — | — |
| `/*` | S3 `web` bucket (or the same distribution's default origin) | public | short TTL |

The `/staging-upload` rewrite exists only because MinIO is not reachable from
the host. On AWS the backend presigns against S3 directly (`S3_ENDPOINT`
unset), the browser `PUT`s to `*.s3.<region>.amazonaws.com`, and a CORS rule on
the staging bucket allows `PUT` from the site origin. The
`x-amz-checksum-sha256` binding is unchanged.

## Configuration that changes

Every value below is an environment variable already read by the code. No code
changes are needed for any of them.

| Variable | Local | AWS |
|---|---|---|
| `S3_ENDPOINT` | `http://s3:9000` | **unset** → regional endpoint |
| `S3_FORCE_PATH_STYLE` | `true` | **unset** (virtual-hosted style) |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | per-service MinIO users | **unset** → default credential chain (EKS Pod Identity) |
| `S3_REGION` | `us-east-1` | the real region |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | MSK IAM bootstrap brokers (port 9098) |
| `KAFKA_SASL_MECHANISM` | `none` | `aws-msk-iam` |
| `KAFKA_AWS_REGION` | — | the MSK region |
| `DB_HOST` | `postgres` | RDS endpoint (or RDS Proxy) |
| `DB_AUTH` | `password` | `iam` |
| `DB_PASSWORD` | local password | **unset** |
| `DB_SSLMODE` | `disable` | `verify-full` (+ RDS CA bundle in the image) |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | `https://maps.example.com` |
| `INTERNAL_JWT_*_KEY_PATH`, `CLOUDFRONT_*_KEY_PATH`, `BETTER_AUTH_SECRET_PATH` | `./dev-keys` bind mount | Secrets Store CSI mount, same paths |
| `CLOUDFRONT_KEY_PAIR_ID` | `LOCALKEYPAIRID` | the CloudFront **public key ID** in the trusted key group |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://jaeger:4317` (profile) | the ADOT collector service |
| `DEMO_UPLOAD_ENABLED` | `true` | **`false`** |
| `ENVIRONMENT` | `local` | `prod` |

The two AWS-only code paths are deliberately small, isolated and marked as
stubs, because they cannot be exercised locally:

* `pmp_common.kafka._security_config` — `aws-msk-iam` uses
  `aws-msk-iam-sasl-signer-python` in an `oauth_cb`.
* `pmp_common.db._make_iam_token_provider` — RDS auth tokens from
  `boto3.client("rds").generate_db_auth_token`, cached for 10 minutes and
  resolved on every new connection by the `do_connect` hook.

## IAM per service (EKS Pod Identity)

Least privilege, mirroring the local MinIO policies (`ops/s3/s3-init.sh`) and
Postgres grants (`ops/db/grants.sql`) one for one.

### gateway
* No AWS permissions. Reads its signing key from a Secrets Store mount:
  `secretsmanager:GetSecretValue` on `pmp/internal-jwt-private-key` (granted to
  the CSI driver's role for this service account).

### auth
* `rds-db:connect` on `arn:aws:rds-db:<region>:<acct>:dbuser:<db-resource-id>/auth_svc`
* `secretsmanager:GetSecretValue` on `pmp/better-auth-secret`

### backend
* `s3:PutObject` on `arn:aws:s3:::<staging>/*` — this is what makes its
  presigned PUTs valid; it never writes the publish bucket.
* `kafka-cluster:Connect`, `kafka-cluster:DescribeTopic`,
  `kafka-cluster:WriteData` on `publication.requested`
* `rds-db:connect` as `backend_svc`
* `secretsmanager:GetSecretValue` on `pmp/internal-jwt-public-key`,
  `pmp/cloudfront-cookie-private-key`

### worker
* `s3:GetObject`, `s3:ListBucket` on `<staging>`
* `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`, `s3:AbortMultipartUpload`,
  `s3:ListMultipartUploadParts` on `<publish>` — the **only** writer of the
  publish bucket
* `kafka-cluster:Connect`, `DescribeTopic`, `ReadData` on
  `publication.requested`; `WriteData` on `publication.results` and
  `publication.requested.dlq`; `AlterGroup`, `DescribeGroup` on the
  `publication-worker` group
* `rds-db:connect` as `worker_svc`

### migration Jobs
* `rds-db:connect` as the privileged migration role (never used by a service)
* MSK `CreateTopic`/`DescribeTopic` for the topic hook

### CloudFront
* Origin Access Control on `publish`: bucket policy allows `s3:GetObject` only
  for `cloudfront.amazonaws.com` with `aws:SourceArn` = the distribution.
* Trusted key group containing the public key whose private half is
  `pmp/cloudfront-cookie-private-key`.

## What gets deleted

| Deleted | Replaced by |
|---|---|
| `services/edge/` (nginx) | CloudFront behaviours above |
| `services/edge-verifier/` | CloudFront trusted key group |
| `/staging-upload` rewrite in `Storage.presign_staging_put` | Presigning straight against S3 (the rewrite is a no-op when `PUBLIC_BASE_URL` and the S3 endpoint are both public) |
| `ops/s3/s3-init.sh` | Terraform |
| `docker-compose*.yml` | Helm charts |
| `scripts/gen-dev-keys.sh` | Secrets Manager (keys generated once, rotated there) |

The CloudFront cookie **issuer** (`POST /api/v1/tiles/session` in the backend)
does not change at all: it already emits CloudFront's exact format, which the
unit tests check against botocore's reference implementation.

## Operational differences worth planning for

* **Signed-cookie scope.** The policy `Resource` is built from
  `PUBLIC_BASE_URL`; it must be the CloudFront domain (or the custom domain
  aliased to it), or CloudFront will reject every private tile.
* **Consumer scaling.** Worker replicas above the partition count (6) sit idle.
  Raise partitions before raising `maxReplicas`.
* **RDS Proxy** is recommended in front of RDS for the backend (async pool per
  pod × HPA replicas); it supports IAM auth, so `DB_AUTH=iam` is unchanged.
* **Rate limiting** in the gateway is per pod. If limits need to be exact across
  replicas, move the token bucket to ElastiCache (see `DECISIONS.md`).
