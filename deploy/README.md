# Deploying to AWS

`docs/aws-mapping.md` describes what each local component becomes. This
directory implements it:

* `terraform/` has every AWS resource. It also builds and pushes the images,
  uploads `web/` and installs the Helm charts.
* `helm/pmp/` is the platform's workloads, with values supplied by Terraform.

```sh
make aws-up     # terraform init + apply   (~30-40 min the first time)
make aws-down   # terraform destroy        (~20-30 min)
```

`make aws-up` prints `url`, the CloudFront address of the platform.

## Prerequisites

* Terraform ≥ 1.10, AWS CLI v2, Docker (with the daemon running) and `bash`.
* AWS credentials for the target account in your shell, e.g.
  `export AWS_PROFILE=symbiose-demo`. The identity that runs `apply` becomes
  cluster admin.
* IAM Identity Center enabled for the account (or its organization): Amazon
  Managed Grafana signs users in with it.
* State is local (`terraform/terraform.tfstate`). Keep it: `make aws-down`
  needs it, and it contains the generated signing keys.

## Inputs

In `terraform/variables.tf`; override with `-var` or a `terraform.tfvars`:

| Variable | Default | |
|---|---|---|
| `region` | `eu-west-1` | everything except CloudFront |
| `name` | `pmp` | prefix for every resource |
| `demo_upload_enabled` | `false` | serve `upload.html` and presign browser uploads |
| `grafana_admin_user_ids` | `[]` | Identity Center user IDs made Grafana admins |
| `node_instance_type` | `t3.large` | two nodes, up to four |

## After the first apply

Create users with `make aws-add-user`. `make seed` is not meant for this: its
users have well-known passwords, and the site is public.

```sh
make aws-add-user email=a@acme.com password='<password>'
```

As with `make add-user`, the user joins the tenant of their email domain
(`acme-com`), created if missing; its first user is the owner. For anything
else, run the CLI in the pod:
`kubectl -n pmp exec deploy/auth -- node dist/users-cli.js`.

## Metrics dashboard

The ADOT collector scrapes every app pod's `/metrics` into Amazon Managed
Prometheus. Amazon Managed Grafana has it as its default data source, with the
local *PMTiles platform* dashboard already installed. Open
`terraform output grafana_url` and sign in with Identity Center.

To be allowed in, put your Identity Center user ID (Identity Center console →
Users → your user → *User ID*) in `terraform.tfvars` and apply:

```hcl
grafana_admin_user_ids = ["<user id>"]
```

The *Private tile authorisations* panel stays empty on AWS: CloudFront checks
the cookies there, and its numbers are in CloudFront's own console reports.

## Checking private tiles through CloudFront

```sh
make aws-demo email=a@acme.com password='<password>'
```

Signs in as that user, publishes a private archive, then reads it through
CloudFront: a cache miss with the signed cookies, a cache hit on the repeat,
and a 403 without them. Needs `demo_upload_enabled = true` (as in
`terraform.tfvars`).

## Cost

About $0.41/hour ($10/day) while it is up. `make aws-down` removes all of it.
The breakdown, ways to save, and what MSK would cost instead are in
`docs/cost.md`.
