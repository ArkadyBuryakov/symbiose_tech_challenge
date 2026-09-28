# The keys scripts/gen-dev-keys.sh makes locally, generated once here and kept
# in Secrets Manager. Pods get them at the same /run/keys paths through the
# Secrets Store CSI driver. Note they are also in the Terraform state.

resource "tls_private_key" "internal_jwt" {
  algorithm = "ED25519"
}

# CloudFront signed cookies are RSA-SHA1, so this one cannot be Ed25519.
resource "tls_private_key" "cloudfront" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "random_password" "better_auth" {
  length  = 43
  special = false
}

locals {
  secret_values = {
    internal-jwt-private-key      = tls_private_key.internal_jwt.private_key_pem
    internal-jwt-public-key       = tls_private_key.internal_jwt.public_key_pem
    cloudfront-cookie-private-key = tls_private_key.cloudfront.private_key_pem_pkcs8
    better-auth-secret            = random_password.better_auth.result
  }
}

resource "aws_secretsmanager_secret" "this" {
  for_each = toset(keys(local.secret_values))

  name = "${var.name}/${each.key}"
  # Deleted immediately on destroy, so the next apply can reuse the name.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "this" {
  for_each = aws_secretsmanager_secret.this

  secret_id     = each.value.id
  secret_string = local.secret_values[each.key]
}
