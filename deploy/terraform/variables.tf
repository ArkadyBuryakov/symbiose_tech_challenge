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

variable "node_instance_type" {
  type    = string
  default = "t3.large"
}
