# Metrics: the ADOT collector scrapes every app pod's /metrics and remote-writes
# to Amazon Managed Prometheus; Amazon Managed Grafana reads it and shows the
# same dashboard as the local `observability` profile.

resource "aws_prometheus_workspace" "this" {
  alias = var.name
}

# ------------------------------------------------------------------ Grafana
# Sign-in is IAM Identity Center, which must be enabled in the account.
data "aws_iam_policy_document" "grafana_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["grafana.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "grafana" {
  name               = "${var.name}-grafana"
  assume_role_policy = data.aws_iam_policy_document.grafana_trust.json
}

# Read-only, and only this workspace.
data "aws_iam_policy_document" "grafana" {
  statement {
    actions   = ["aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"]
    resources = [aws_prometheus_workspace.this.arn]
  }
}

resource "aws_iam_role_policy" "grafana" {
  role   = aws_iam_role.grafana.id
  policy = data.aws_iam_policy_document.grafana.json
}

resource "aws_grafana_workspace" "this" {
  name                     = var.name
  account_access_type      = "CURRENT_ACCOUNT"
  authentication_providers = ["AWS_SSO"]
  permission_type          = "CUSTOMER_MANAGED"
  role_arn                 = aws_iam_role.grafana.arn
  grafana_version          = "12.4"
  # Lets Terraform install the Amazon Managed Prometheus data source plugin.
  configuration = jsonencode({
    plugins         = { pluginAdminEnabled = true }
    unifiedAlerting = { enabled = true } # required before upgrading to 12
  })
}

resource "aws_grafana_role_association" "admins" {
  count = length(var.grafana_admin_user_ids) > 0 ? 1 : 0

  workspace_id = aws_grafana_workspace.this.id
  role         = "ADMIN"
  user_ids     = var.grafana_admin_user_ids
}

# Terraform's own credentials for the Grafana API. Tokens live at most 30 days,
# so a new one is minted on the first apply after 25.
resource "aws_grafana_workspace_service_account" "terraform" {
  workspace_id = aws_grafana_workspace.this.id
  name         = "terraform"
  grafana_role = "ADMIN"
}

resource "time_rotating" "grafana_token" {
  rotation_days = 25
}

resource "aws_grafana_workspace_service_account_token" "terraform" {
  workspace_id       = aws_grafana_workspace.this.id
  service_account_id = aws_grafana_workspace_service_account.terraform.service_account_id
  name               = "terraform-${time_rotating.grafana_token.unix}"
  seconds_to_live    = 30 * 24 * 3600
}

provider "grafana" {
  url  = "https://${aws_grafana_workspace.this.endpoint}"
  auth = aws_grafana_workspace_service_account_token.terraform.key
}

# SigV4 in the core Prometheus data source is deprecated in favour of this
# plugin. Neither the AWS API nor the grafana provider installs plugins on a
# managed workspace, so Grafana's own API does. 409 = already installed.
locals {
  amp_plugin         = "grafana-amazonprometheus-datasource"
  amp_plugin_version = "3.2.0" # needs Grafana >= 12.2.5
}

resource "terraform_data" "amp_plugin" {
  triggers_replace = [aws_grafana_workspace.this.id, aws_grafana_workspace.this.grafana_version, local.amp_plugin_version]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      code=$(curl -sS -o /dev/stderr -w '%%{http_code}' -X POST \
        -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
        -d '{"version":"${local.amp_plugin_version}"}' \
        "$GRAFANA_URL/api/plugins/${local.amp_plugin}/install")
      [[ $code == 200 || $code == 409 ]] || { echo "plugin install: HTTP $code" >&2; exit 1; }
    EOT
    environment = {
      GRAFANA_URL = "https://${aws_grafana_workspace.this.endpoint}"
      TOKEN       = aws_grafana_workspace_service_account_token.terraform.key
    }
  }
}

# uid "prometheus" is what the dashboard's panels reference, as locally.
resource "grafana_data_source" "prometheus" {
  type       = local.amp_plugin
  name       = "Amazon Managed Prometheus"
  uid        = "prometheus"
  url        = trimsuffix(aws_prometheus_workspace.this.prometheus_endpoint, "/")
  is_default = true

  json_data_encoded = jsonencode({
    httpMethod    = "POST"
    sigV4Auth     = true
    sigV4AuthType = "ec2_iam_role" # the workspace's IAM role
    sigV4Region   = var.region
  })

  depends_on = [terraform_data.amp_plugin]
}

resource "grafana_dashboard" "platform" {
  # The local dashboard, pointed at the plugin's data source type.
  config_json = replace(
    file("${local.repo_root}/ops/observability/grafana/dashboards/pmtiles-platform.json"),
    "\"type\": \"prometheus\"", "\"type\": \"${local.amp_plugin}\"",
  )
  overwrite = true

  depends_on = [grafana_data_source.prometheus]
}
