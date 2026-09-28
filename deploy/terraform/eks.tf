module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.26"

  name               = var.name
  kubernetes_version = "1.36"

  # The API is public so `terraform apply` can reach it from your machine.
  endpoint_public_access                   = true
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni = {
      before_compute = true
      # Enforces the chart's NetworkPolicies (backend/auth <- gateway only).
      configuration_values = jsonencode({ enableNetworkPolicy = "true" })
    }
    eks-pod-identity-agent = { before_compute = true }
    metrics-server         = {}
    # Secrets Store CSI driver + AWS provider: Secrets Manager -> /run/keys.
    aws-secrets-store-csi-driver-provider = {}
  }

  eks_managed_node_groups = {
    default = {
      instance_types = [var.node_instance_type]
      # The cluster autoscaler (k8s.tf) moves desired_size within min..max.
      min_size     = 2
      max_size     = 4
      desired_size = 2
    }
  }

  node_security_group_additional_rules = {
    alb_to_gateway = {
      description              = "Internal ALB to the gateway NodePort"
      type                     = "ingress"
      protocol                 = "tcp"
      from_port                = 30080
      to_port                  = 30080
      source_security_group_id = aws_security_group.alb.id
    }
  }
}
