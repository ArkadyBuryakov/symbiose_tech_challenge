# Cluster add-ons that come from Helm, then the platform chart itself.

resource "kubernetes_namespace_v1" "app" {
  metadata {
    name = var.name
  }

  # Waits for the creator's cluster-admin access entry, not just the endpoint.
  depends_on = [module.eks]
}

# Created here rather than in the chart: the Helm hook Jobs run before any
# chart resource exists, and Pod Identity binds roles to these names.
resource "kubernetes_service_account_v1" "app" {
  for_each = toset([for w in keys(local.workloads) : w if !contains(keys(local.workload_namespace), w)])

  metadata {
    name      = each.key
    namespace = kubernetes_namespace_v1.app.metadata[0].name
  }
}

resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = "2.21.0"
  namespace        = "keda"
  create_namespace = true

  depends_on = [module.eks]
}

# Scales the node group (2-4) on pending pods. EKS already tags managed node
# group ASGs for auto-discovery, and the module ignores `desired_size` drift,
# so Terraform does not undo its changes.
resource "helm_release" "cluster_autoscaler" {
  name       = "cluster-autoscaler"
  repository = "https://kubernetes.github.io/autoscaler"
  chart      = "cluster-autoscaler"
  version    = "9.59.0"
  namespace  = "kube-system"

  values = [yamlencode({
    autoDiscovery = { clusterName = module.eks.cluster_name }
    awsRegion     = var.region
    # The autoscaler's minor version must match the cluster's.
    image = { tag = "v1.36.1" }
    rbac  = { serviceAccount = { name = "cluster-autoscaler" } }
    extraArgs = {
      # Otherwise a node running CoreDNS or metrics-server is never removed.
      skip-nodes-with-system-pods = false
      balance-similar-node-groups = true
    }
  })]

  depends_on = [module.eks, aws_eks_pod_identity_association.workload]
}

# The Redpanda broker (demo stand-in for MSK). Installed before the platform
# chart and complete only once its post-install Job has created the topics,
# so no service starts subscribed to a topic that does not exist yet.
resource "helm_release" "kafka" {
  name      = "${var.name}-kafka"
  chart     = "${path.module}/../helm/kafka"
  namespace = kubernetes_namespace_v1.app.metadata[0].name

  values = [yamlencode({
    chartHash = sha1(join("", [for f in sort(fileset("${path.module}/../helm/kafka", "**")) : filesha1("${path.module}/../helm/kafka/${f}")]))
    kafkaInit = file("${local.repo_root}/ops/kafka/kafka-init.sh")
  })]

  depends_on = [module.eks]
}

data "http" "rds_ca" {
  url = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
}

locals {
  chart_dir = "${path.module}/../helm/pmp"
}

resource "helm_release" "app" {
  name      = var.name
  chart     = local.chart_dir
  namespace = kubernetes_namespace_v1.app.metadata[0].name
  timeout   = 900
  # A failed install rolls back instead of leaving a release Terraform can't
  # adopt ("cannot re-use a name"). Hook Jobs survive, so their logs remain.
  atomic = true

  values = [yamlencode({
    # Terraform does not notice edits to a local chart; this makes it.
    chartHash         = sha1(join("", [for f in sort(fileset(local.chart_dir, "**")) : filesha1("${local.chart_dir}/${f}")]))
    registry          = "${local.registry}/${var.name}"
    tag               = local.image_tag
    region            = var.region
    publicBaseUrl     = "https://${aws_cloudfront_distribution.this.domain_name}"
    demoUploadEnabled = var.demo_upload_enabled
    db = {
      host            = aws_db_instance.this.address
      name            = aws_db_instance.this.db_name
      masterUser      = aws_db_instance.this.username
      masterSecretArn = aws_db_instance.this.master_user_secret[0].secret_arn
    }
    rdsCaBundle = data.http.rds_ca.response_body
    s3 = {
      stagingBucket = aws_s3_bucket.this["staging"].id
      publishBucket = aws_s3_bucket.this["publish"].id
    }
    secrets = {
      internalJwtPrivateKey = aws_secretsmanager_secret.this["internal-jwt-private-key"].name
      internalJwtPublicKey  = aws_secretsmanager_secret.this["internal-jwt-public-key"].name
      cloudfrontPrivateKey  = aws_secretsmanager_secret.this["cloudfront-cookie-private-key"].name
      betterAuthSecret      = aws_secretsmanager_secret.this["better-auth-secret"].name
    }
    cloudfrontKeyPairId = aws_cloudfront_public_key.cookies.id
    gatewayRoutes       = file("${local.repo_root}/services/gateway/routes.yaml")
    ampRemoteWriteUrl   = "${aws_prometheus_workspace.this.prometheus_endpoint}api/v1/remote_write"
  })]

  depends_on = [
    module.eks,
    helm_release.keda,
    helm_release.kafka,
    kubernetes_service_account_v1.app,
    aws_eks_pod_identity_association.workload,
    aws_secretsmanager_secret_version.this,
    terraform_data.image,
    aws_lb_listener.http,
  ]
}
