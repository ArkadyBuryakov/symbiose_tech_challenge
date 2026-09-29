variable "region" {
  description = "AWS region for everything except CloudFront (global)."
  type        = string
  default     = "eu-west-1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "pmp"
}

variable "demo_upload_enabled" {
  description = "Serve upload.html and let the backend presign browser uploads (docs/aws-mapping.md: false)."
  type        = bool
  default     = false
}

variable "grafana_admin_user_ids" {
  description = "IAM Identity Center user IDs made Grafana admins (Identity Center console -> Users -> user -> User ID)."
  type        = list(string)
  default     = []
}

variable "node_instance_type" {
  type    = string
  default = "t3.large"
}
