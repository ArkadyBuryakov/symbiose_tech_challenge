terraform {
  required_version = ">= 1.10"

  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.66" }
    helm       = { source = "hashicorp/helm", version = "~> 3.3" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 3.2" }
    tls        = { source = "hashicorp/tls", version = "~> 4.4" }
    random     = { source = "hashicorp/random", version = "~> 3.9" }
    http       = { source = "hashicorp/http", version = "~> 3.6" }
    time       = { source = "hashicorp/time", version = "~> 0.14" }
    grafana    = { source = "grafana/grafana", version = "~> 4.46" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name, ManagedBy = "terraform" }
  }
}

# Both authenticate with a fresh token per call, so long applies don't expire.
locals {
  cluster_auth = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--region", var.region, "--cluster-name", module.eks.cluster_name]
    }
  }
}

provider "kubernetes" {
  host                   = local.cluster_auth.host
  cluster_ca_certificate = local.cluster_auth.cluster_ca_certificate
  exec {
    api_version = local.cluster_auth.exec.api_version
    command     = local.cluster_auth.exec.command
    args        = local.cluster_auth.exec.args
  }
}

provider "helm" {
  kubernetes = local.cluster_auth
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}
