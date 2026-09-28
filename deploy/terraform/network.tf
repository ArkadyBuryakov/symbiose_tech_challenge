data "aws_availability_zones" "available" {
  state = "available"
}

# Two AZs: enough for RDS Multi-AZ and the EKS nodes. One NAT gateway;
# the internet gateway is also what CloudFront VPC origins require.
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name            = var.name
  cidr            = "10.0.0.0/16"
  azs             = slice(data.aws_availability_zones.available.names, 0, 2)
  private_subnets = ["10.0.0.0/19", "10.0.32.0/19"]
  public_subnets  = ["10.0.64.0/24", "10.0.65.0/24"]

  enable_nat_gateway = true
  single_nat_gateway = true
}
