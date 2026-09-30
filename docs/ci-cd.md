# Setting up CI/CD

`.github/workflows/pipeline.yml` tests the code and deploys the AWS stack with
Terraform. It has no automatic triggers; it runs only from **Actions → pipeline
→ Run workflow**. This page is the one-time setup its `deploy` job needs. The
`test` job needs none.

| What | Why |
|---|---|
| S3 bucket for Terraform state | CI has no disk that outlives a run; `use_lockfile` stops two runs writing the state at once |
| GitHub as an OIDC provider in AWS | The job gets short-lived AWS credentials; no AWS keys are stored in GitHub |
| IAM role for the job | What those credentials may do; its trust policy decides which runs may use it |
| Repository variables | Tell the workflow the role, the region and the bucket |
| GitHub environment `prod` | A reviewer approves each deploy before it starts |

Run the commands from your clone of the repository, with AWS credentials for
an administrator of the target account (e.g. `export AWS_PROFILE=<profile>`)
and the GitHub CLI logged in (`gh auth login`). Set these first; the steps
below use them:

```sh
export AWS_REGION="<region>"   # where the stack and the state bucket live
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)   # owner/name
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=pmp-tfstate-$ACCOUNT
```

## 1. State bucket

```sh
aws s3api create-bucket --bucket "$BUCKET" \
  --create-bucket-configuration LocationConstraint=$AWS_REGION
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

S3 encrypts new objects by default. Versioning lets you restore an earlier
state if a run corrupts it. The state contains the platform's signing keys
(see `deploy/README.md`), so nobody but administrators and the CI role should
be able to read this bucket.

## 2. GitHub as an OIDC provider

Once per AWS account:

```sh
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
```

No thumbprint is needed: AWS validates GitHub's certificate itself. If the
command says the provider exists, skip it.

## 3. The role the job assumes

The trust policy is the security boundary. `sub` names the only runs allowed:
jobs of this repository running in the `prod` environment. A fork, another
branch outside `prod`'s rules, or another repository gets "not authorized".

```sh
cat > trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:${REPO}:environment:prod"
      }
    }
  }]
}
EOF
aws iam create-role --role-name pmp-github-deploy \
  --assume-role-policy-document file://trust.json
aws iam attach-role-policy --role-name pmp-github-deploy \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
rm trust.json
```

`AdministratorAccess` is what the Terraform needs as written: it creates IAM
roles, the EKS cluster, CloudFront, RDS and more. The reviewer gate in step 5
is what limits its use. Never widen `sub` to `repo:${REPO}:*`.

## 4. Repository variables

```sh
gh variable set AWS_ROLE_ARN    --repo "$REPO" \
  --body "arn:aws:iam::${ACCOUNT}:role/pmp-github-deploy"
gh variable set AWS_REGION      --repo "$REPO" --body "$AWS_REGION"
gh variable set TF_STATE_BUCKET --repo "$REPO" --body "$BUCKET"
```

Optional: Grafana admins, as a JSON list of IAM Identity Center user IDs
(Identity Center console → Users → user → *User ID*). Without it nobody can
sign in to Grafana until users are assigned in its console.

```sh
gh variable set GRAFANA_ADMIN_USER_IDS --repo "$REPO" --body '["<user id>"]'
```

Or in the browser: **Settings → Secrets and variables → Actions → Variables**.
None of these values is secret.

## 5. The `prod` environment

In the browser: **Settings → Environments → New environment**, named `prod`:

- **Required reviewers:** yourself (and anyone else who may approve deploys).
- **Deployment branches and tags:** *Selected branches* → `main`.

The same with `gh`:

```sh
gh api -X PUT "repos/$REPO/environments/prod" --input - <<EOF
{
  "reviewers": [{"type": "User", "id": $(gh api user --jq .id)}],
  "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}
}
EOF
gh api -X POST "repos/$REPO/environments/prod/deployment-branch-policies" -f name=main
```

## 6. Start without a local stack

CI keeps its state in the bucket; `make aws-up` keeps its own in
`deploy/terraform/terraform.tfstate`. The two must never manage the same stack,
or each will try to create what the other already has. So if a stack made by
`make aws-up` is running, remove it first:

```sh
make aws-down
```

CI then builds its own stack on the first run with **apply** ticked, and from
then on deploys go through the pipeline only. The pipeline has no destroy step;
to remove a CI-made stack, destroy it from your machine against the same state
(this needs step 7's access):

```sh
cd deploy/terraform
printf 'terraform {\n  backend "s3" {}\n}\n' > backend_override.tf
terraform init -reconfigure \
  -backend-config="bucket=$BUCKET" -backend-config="key=pmp/terraform.tfstate" \
  -backend-config="region=$AWS_REGION" -backend-config="use_lockfile=true"
TF_VAR_region=$AWS_REGION terraform destroy   # the region CI deployed to
rm backend_override.tf && terraform init -reconfigure   # back to local state
```

Handing a running stack over to CI (moving its state into the bucket) does not
work as the Terraform stands: the cluster admits only its creator (next step),
so the CI role cannot read the Kubernetes resources it would take over. It
would need explicit `access_entries` in `eks.tf` instead of
`enable_cluster_creator_admin_permissions`.

## 7. Your own access to the cluster

The cluster grants Kubernetes admin to whoever created it
(`enable_cluster_creator_admin_permissions` in `eks.tf`), which is now the CI
role. `kubectl`, k9s and `make aws-add-user` need an access entry for your own
role. Add it once, after the first CI apply:

```sh
ME=$(aws sts get-caller-identity --query Arn --output text)
# Signed in through a role (SSO or assumed): the entry is for the role itself.
if [[ $ME == *:assumed-role/* ]]; then
  ROLE=$(cut -d/ -f2 <<<"$ME")
  ME=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)
fi
aws eks create-access-entry --cluster-name pmp --principal-arn "$ME"
aws eks associate-access-policy --cluster-name pmp --principal-arn "$ME" \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster
```

Terraform does not manage this entry, so later applies leave it alone. `pmp`
is the default `name` input; use yours if you changed it.

## 8. Run it

```sh
gh workflow run pipeline --repo "$REPO" -f apply=false   # plan only
gh run watch --repo "$REPO"
```

The `deploy` job waits for a reviewer's approval, then plans. Read the plan
in the job log; run again with `-f apply=true` to apply it.

| Error in the `deploy` job | Cause |
|---|---|
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy's `sub` does not match the run: the job must use `environment: prod`, and `REPO` must be exactly the repository |
| `Could not load credentials from any providers` | The `id-token: write` permission is missing, or `AWS_ROLE_ARN` is not set |
| `Error acquiring the state lock` | Another run holds it; the `concurrency` group normally prevents this. After a cancelled run, `terraform force-unlock <id>` |
| `Unauthorized` from the kubernetes or helm provider | The cluster was created by someone else (e.g. `make aws-up`), so the CI role has no access entry; see step 6 |
