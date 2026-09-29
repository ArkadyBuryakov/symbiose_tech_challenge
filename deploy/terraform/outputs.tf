output "url" {
  description = "The platform, served by CloudFront."
  value       = "https://${aws_cloudfront_distribution.this.domain_name}"
}

output "grafana_url" {
  description = "Amazon Managed Grafana; sign in with IAM Identity Center."
  value       = "https://${aws_grafana_workspace.this.endpoint}"
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}
