# One role per workload, bound to its service account by EKS Pod Identity.
# Least privilege, as listed in docs/aws-mapping.md.

locals {
  db_user = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}"
  secret  = { for k, s in aws_secretsmanager_secret.this : k => s.arn }
  s3      = { for k, b in aws_s3_bucket.this : k => b.arn }

  # service account => policy statements
  workloads = {
    gateway = [
      { actions = ["secretsmanager:GetSecretValue"], resources = [local.secret["internal-jwt-private-key"]] },
    ]
    auth = [
      { actions = ["rds-db:connect"], resources = ["${local.db_user}/auth_svc"] },
      { actions = ["secretsmanager:GetSecretValue"], resources = [local.secret["better-auth-secret"]] },
    ]
    backend = [
      # What makes its presigned PUTs valid; it never writes the publish bucket.
      { actions = ["s3:PutObject"], resources = ["${local.s3.staging}/*"] },
      { actions = ["rds-db:connect"], resources = ["${local.db_user}/backend_svc"] },
      { actions = ["secretsmanager:GetSecretValue"], resources = [local.secret["internal-jwt-public-key"], local.secret["cloudfront-cookie-private-key"]] },
    ]
    worker = [
      { actions = ["s3:GetObject", "s3:ListBucket"], resources = [local.s3.staging, "${local.s3.staging}/*"] },
      # The only writer of the publish bucket.
      {
        actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        resources = [local.s3.publish, "${local.s3.publish}/*"]
      },
      { actions = ["rds-db:connect"], resources = ["${local.db_user}/worker_svc"] },
    ]
    # Logs in as the RDS master user with the password RDS keeps.
    migrate = [
      { actions = ["secretsmanager:GetSecretValue"], resources = [aws_db_instance.this.master_user_secret[0].secret_arn] },
    ]
    # Adds nodes for pending pods, removes idle ones. Describe calls cannot be
    # scoped; changing capacity is limited to this cluster's node group.
    cluster-autoscaler = [
      {
        actions = [
          "autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations", "autoscaling:DescribeScalingActivities",
          "autoscaling:DescribeTags", "ec2:DescribeImages", "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplateVersions", "ec2:GetInstanceTypesFromInstanceRequirements",
          "eks:DescribeNodegroup",
        ]
        resources = ["*"]
      },
      {
        actions   = ["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"]
        resources = ["arn:aws:autoscaling:${var.region}:${local.account_id}:autoScalingGroup:*:autoScalingGroupName/${local.node_asg}"]
      },
    ]
    otel-collector = [
      { actions = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], resources = ["*"] },
    ]
  }
  # Everything runs in the app namespace except the cluster add-ons.
  workload_namespace = { cluster-autoscaler = "kube-system" }
  node_asg           = module.eks.eks_managed_node_groups["default"].node_group_autoscaling_group_names[0]
}

data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "workload" {
  for_each = local.workloads

  dynamic "statement" {
    for_each = each.value
    content {
      actions   = statement.value.actions
      resources = statement.value.resources
    }
  }
}

resource "aws_iam_role" "workload" {
  for_each = local.workloads

  name               = "${var.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy" "workload" {
  for_each = local.workloads

  role   = aws_iam_role.workload[each.key].id
  policy = data.aws_iam_policy_document.workload[each.key].json
}

resource "aws_eks_pod_identity_association" "workload" {
  for_each = local.workloads

  cluster_name    = module.eks.cluster_name
  namespace       = lookup(local.workload_namespace, each.key, var.name)
  service_account = each.key
  role_arn        = aws_iam_role.workload[each.key].arn
}
