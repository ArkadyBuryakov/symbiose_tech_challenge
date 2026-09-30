# What the AWS deployment costs

This covers the stack that `make aws-up` creates (`deploy/terraform`), with
default inputs, in **eu-central-1 (Frankfurt)**. Prices are on-demand list
prices in USD, excluding tax, taken from the AWS Price List API on 2026-09-30.
There are no savings plans or reserved capacity.

## Summary

| | Cost |
|---|---|
| **Per hour, idle** | **≈ $0.43** |
| Per day | ≈ $10 |
| Per month (730 h) | ≈ $315 |
| Plus EKS control-plane logs | ≈ $10–40 / month (see below) |
| A typical demo session: apply (~40 min) + 2 h use + destroy (~30 min) | ≈ $1.40 |

Traffic adds little on top at demo volumes, because CloudFront's free tier
covers the first 1 TB a month. The two EC2 nodes are the largest single item,
followed by the EKS control plane. Kafka is a single Redpanda pod on those
nodes, so it adds nothing. MSK Serverless, the production target in
`docs/aws-mapping.md`, would add ≈ $0.93/h and more than triple the bill (see
the end of this page).

## Fixed costs (billed while the stack exists)

| Component | What is deployed | Unit price | $/hour | $/month |
|---|---|---|---:|---:|
| EC2 nodes | 2 × t3.large (Linux) | $0.096 / hour | 0.192 | 140 |
| EBS (node disks) | 2 × 20 GB gp3 | $0.0952 / GB-month | 0.005 | 3.8 |
| EKS control plane | 1 cluster (standard support) | $0.10 / hour | 0.100 | 73 |
| NAT gateway | 1 (single NAT) | $0.052 / hour | 0.052 | 38 |
| Public IPv4 | 1 (the NAT gateway's) | $0.005 / hour | 0.005 | 3.7 |
| RDS PostgreSQL | db.t4g.micro, Multi-AZ | $0.037 / hour | 0.037 | 27 |
| | 20 GB gp3, Multi-AZ | $0.274 / GB-month | 0.008 | 5.5 |
| Application Load Balancer | 1 internal | $0.027 / hour (+ LCUs, ~0 idle) | 0.027 | 20 |
| Secrets Manager | 4 platform keys + the RDS master secret | $0.40 / secret-month | 0.003 | 2.0 |
| KMS | 1 customer key (EKS secrets encryption) | $1 / key-month | 0.001 | 1.0 |
| **Total** | | | **≈ 0.43** | **≈ 315** |

These cost nothing extra:
- **CloudFront:** the distribution, its VPC origin and its OAC.
- **Kafka (one Redpanda pod), Pod Identity, the add-ons, KEDA, the Cluster Autoscaler and the CSI driver:** all run on the nodes above. The broker keeps its data in an `emptyDir` on the node disk, so there is no EBS volume.
- **S3 and ECR:** they cost cents at demo volumes (see below).

## Usage-dependent costs

| Item | Price | Notes |
|---|---|---|
| **EKS control-plane logs** | $0.63 / GB ingested, $0.0324 / GB-month stored | The EKS module ships `audit`, `api` and `authenticator` logs to CloudWatch with 90-day retention. The audit log is chatty even when idle, because KEDA, the HPA and the CSI driver poll the API. **Estimate:** 0.5–2 GB/day, which is ≈ $10–40/month. This is the largest cost not visible in the table above. |
| CloudFront → viewers | $0.085 / GB after 1 TB free per month; $0.012 / 10k HTTPS requests after 10M free | Range reads of PMTiles are small. A demo stays inside the free tier. |
| CloudFront Functions | $0.10 / 1M invocations after 2M free per month | Run on `/tiles/*` and `/api/*` requests. |
| NAT gateway data | $0.052 / GB processed | Mostly image pulls from ECR and public ECR: ≈ 1–2 GB per deploy, so ≈ $0.10. |
| Cross-AZ traffic | $0.01 / GB each way | Pod ↔ RDS/Kafka/pod across the two AZs. Negligible at demo volume. RDS Multi-AZ replication is free. |
| X-Ray | $5 / 1M traces stored after 100k free per month | Probe endpoints are not traced. |
| Amazon Managed Grafana | $9 per active editor/admin user per month, $5 per viewer | Billed only for users who sign in that month. |
| Amazon Managed Prometheus | First 40M samples a month free, then $0.90 / 10M ingested; storage free up to 10 GB, then $0.03 / GB-month; $0.10 / 1B samples queried | ≈ 700 series (7 pods) scraped every 30 s is ≈ 2M samples/day (≈ 60M/month), so ≈ $2/month. More replicas add series. |
| S3 | $0.0245 / GB-month | Staged uploads expire after 7 days. The published archives are what grows. |
| ECR | $0.10 / GB-month | ≈ 0.5–1 GB per build. Old images are kept, so every rebuild adds a little until `make aws-down`. |
| EC2 CPU credits | $0.05 / vCPU-hour of surplus | t3 nodes run in *unlimited* mode. This only applies if a node averages above its 30% baseline, which only a sustained publishing load would cause. |

## Scaling under load

The HPAs (gateway, backend: 2–6 pods) and KEDA (worker: 1–6 pods) scale pods.
Each worker requests 500m CPU (one job's real use). Beyond about three busy
workers the pods no longer fit on two nodes, so the cluster autoscaler grows
the node group, up to 4 nodes. Once the backlog is drained it removes idle
nodes again, after about 10 minutes.

| Load | Nodes | $/hour |
|---|---:|---:|
| Idle, or a few jobs at a time | 2 | 0.43 |
| Full backlog: 6 workers plus scaled-out API pods | 3–4 | 0.53–0.62 |

Each extra t3.large costs $0.096/h, and is billed only while it runs.

## Where to save, largest first

| Change | Saves | Trade-off |
|---|---:|---|
| Turn off EKS control-plane logs (`enabled_log_types = []`), or keep only `api` | ≈ $10–40/month | Less audit trail for a demo cluster. |
| RDS Single-AZ | $0.018/h + $2.7/month storage (≈ $16/month) | Departs from `docs/aws-mapping.md` (Multi-AZ). |
| t3.medium nodes (`node_instance_type`) | $0.096/h idle (≈ $70/month) | Half the CPU per node, so a backlog reaches the 4-node ceiling sooner: raise `max_size` to keep 6 workers. |
| Destroy between demos | everything | `make aws-down`. The next `make aws-up` takes ~40 min and issues a new CloudFront domain. |

Not worth it at this scale: VPC endpoints for ECR, S3 and STS to avoid NAT data
charges. They cost ≈ $0.012/h per endpoint per AZ, which is more than the NAT
data they would save.

## Kafka: the demo broker compared with MSK

The demo runs Kafka in-cluster to keep the bill at a third of what MSK would
make it. The durability and per-service IAM that MSK provides are not what a
demo is assessed on. For reference, on-demand:

| Kafka | $/hour | Stack total $/hour | Notes |
|---|---:|---:|---|
| One Redpanda pod (deployed) | 0 | ≈ 0.43 | 1 replica, no auth or TLS. The data is lost if the pod is rescheduled; the worker's reconciler re-publishes pending jobs. |
| MSK provisioned, 3 × kafka.t3.small | ≈ 0.16 + $0.119 / GB-month storage | ≈ 0.59 | Needs a third AZ; creation takes 25–40 min; t3.small brokers are for development only. |
| MSK Serverless, 18 partitions | ≈ 0.93 ($0.90 / cluster-hour + $0.0018 / partition-hour) | ≈ 1.36 | IAM per topic and group, RF 3, `min.insync.replicas=2`. Data $0.12 / GB in, $0.06 / GB out. |
