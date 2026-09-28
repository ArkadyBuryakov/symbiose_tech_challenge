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
* State is local (`terraform/terraform.tfstate`). Keep it: `make aws-down`
  needs it, and it contains the generated signing keys.

## Inputs

In `terraform/variables.tf`; override with `-var` or a `terraform.tfvars`:

| Variable | Default | |
|---|---|---|
| `region` | `eu-west-1` | everything except CloudFront |
| `name` | `pmp` | prefix for every resource |
| `demo_upload_enabled` | `false` | serve `upload.html` and presign browser uploads |
| `node_instance_type` | `t3.large` | two nodes, up to four |

## After the first apply

Create users with the auth CLI inside the cluster. `make seed` is not meant for
this: its users have well-known passwords, and the site is public.

```sh
$(terraform -chdir=deploy/terraform output -raw kubeconfig_command)
kubectl -n pmp exec deploy/auth -- node dist/users-cli.js add-tenant acme "Acme Corp"
kubectl -n pmp exec deploy/auth -- node dist/users-cli.js add-user a@acme.com '<password>' --tenant acme --role owner
```

## Cost

About $0.41/hour ($10/day) while it is up. `make aws-down` removes all of it.
The breakdown, ways to save, and what MSK would cost instead are in
`docs/cost.md`.
