# The three buckets: staging (presigned uploads, expires), publish (tiles, read
# only by CloudFront through OAC) and web (the static pages).

locals {
  buckets = { for b in ["staging", "publish", "web"] : b => "${var.name}-${local.account_id}-${b}" }
}

resource "aws_s3_bucket" "this" {
  for_each = local.buckets

  bucket        = each.value
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = aws_s3_bucket.this

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "staging" {
  bucket = aws_s3_bucket.this["staging"].id

  rule {
    id     = "expire-staged-uploads"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Browsers PUT presigned uploads straight to the regional endpoint.
resource "aws_s3_bucket_cors_configuration" "staging" {
  bucket = aws_s3_bucket.this["staging"].id

  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = ["https://${aws_cloudfront_distribution.this.domain_name}"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# publish and web: s3:GetObject for this distribution only (OAC).
data "aws_iam_policy_document" "cloudfront_read" {
  for_each = toset(["publish", "web"])

  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.this[each.key].arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "cloudfront_read" {
  for_each = data.aws_iam_policy_document.cloudfront_read

  bucket     = aws_s3_bucket.this[each.key].id
  policy     = each.value.json
  depends_on = [aws_s3_bucket_public_access_block.this]
}

# ------------------------------------------------------------------ web pages
locals {
  web_dir = "${local.repo_root}/web"
  web_files = [
    for f in fileset(local.web_dir, "**") : f
    if !startswith(f, "tests/") && (var.demo_upload_enabled || f != "upload.html")
  ]
  content_types = {
    html = "text/html; charset=utf-8"
    js   = "text/javascript; charset=utf-8"
    css  = "text/css; charset=utf-8"
    json = "application/json"
    svg  = "image/svg+xml"
    png  = "image/png"
  }
}

resource "aws_s3_object" "web" {
  for_each = toset(local.web_files)

  bucket       = aws_s3_bucket.this["web"].id
  key          = each.value
  source       = "${local.web_dir}/${each.value}"
  etag         = filemd5("${local.web_dir}/${each.value}")
  content_type = lookup(local.content_types, reverse(split(".", each.value))[0], "application/octet-stream")
  # The pages are tiny and change with every deploy; the tiles they load are
  # what carries long cache lifetimes.
  cache_control = "no-cache"
}
