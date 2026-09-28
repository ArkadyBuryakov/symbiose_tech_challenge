output "url" {
  description = "The platform, served by CloudFront."
  value       = "https://${aws_cloudfront_distribution.this.domain_name}"
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}
