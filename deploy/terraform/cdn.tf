# CloudFront is the only public entry point; its behaviours are what the local
# nginx `edge` config becomes (docs/aws-mapping.md).

# ----------------------------------------------- internal ALB -> gateway pods
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name   = "${var.name}-alb"
  vpc_id = module.vpc.vpc_id

  ingress {
    description     = "CloudFront (VPC origin)"
    protocol        = "tcp"
    from_port       = 80
    to_port         = 80
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    description = "Gateway NodePort on the EKS nodes"
    protocol    = "tcp"
    from_port   = 30080
    to_port     = 30080
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
}

resource "aws_lb" "api" {
  name               = var.name
  internal           = true
  load_balancer_type = "application"
  subnets            = module.vpc.private_subnets
  security_groups    = [aws_security_group.alb.id]
}

# The gateway Service is a NodePort, so the nodes themselves are the targets.
resource "aws_lb_target_group" "gateway" {
  name                 = "${var.name}-gateway"
  port                 = 30080
  protocol             = "HTTP"
  vpc_id               = module.vpc.vpc_id
  target_type          = "instance"
  deregistration_delay = 30

  health_check {
    path = "/healthz"
  }
}

resource "aws_autoscaling_attachment" "gateway" {
  autoscaling_group_name = local.node_asg
  lb_target_group_arn    = aws_lb_target_group.gateway.arn
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

resource "aws_cloudfront_vpc_origin" "api" {
  vpc_origin_endpoint_config {
    name                   = var.name
    arn                    = aws_lb.api.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

# --------------------------------------------------------- CloudFront pieces
resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = var.name
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# The public half of pmp/cloudfront-cookie-private-key; the backend signs the
# tile cookies with the private half.
resource "aws_cloudfront_public_key" "cookies" {
  name        = "${var.name}-cookies"
  encoded_key = tls_private_key.cloudfront.public_key_pem
}

resource "aws_cloudfront_key_group" "cookies" {
  name  = "${var.name}-cookies"
  items = [aws_cloudfront_public_key.cookies.id]
}

# /tiles/{public,private}/... is stored as {public,private}/... in the bucket.
# The signed cookie is checked against the viewer URL, before this runs.
resource "aws_cloudfront_function" "strip_tiles_prefix" {
  name    = "${var.name}-strip-tiles-prefix"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      request.uri = request.uri.replace(/^\/tiles\//, "/");
      return request;
    }
  EOT
}

# The gateway rate-limits on X-Client-IP, which the local nginx edge set from the
# TCP peer. Overwritten here, so a client cannot choose its own bucket.
# (Not X-Real-IP: CloudFront Functions may not set that header.)
resource "aws_cloudfront_function" "real_ip" {
  name    = "${var.name}-real-ip"
  runtime = "cloudfront-js-2.0"
  publish = true
  code    = <<-EOT
    function handler(event) {
      event.request.headers["x-client-ip"] = { value: event.viewer.ip };
      return event.request;
    }
  EOT
}

# Private archives: the cookie, not the URL, authorises the response, so it
# must never land in a shared cache downstream.
resource "aws_cloudfront_response_headers_policy" "private_tiles" {
  name = "${var.name}-private-tiles"

  custom_headers_config {
    items {
      header   = "Cache-Control"
      value    = "private, max-age=600"
      override = true
    }
  }

  security_headers_config {
    content_type_options {
      override = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
  }
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# Tiles: Managed-CachingOptimized, except that an edge keeps a copy at most
# 30 days before asking S3 again (the archive's own header says a year, which
# browsers still get). Archives never change, so the re-check is a 304.
resource "aws_cloudfront_cache_policy" "tiles" {
  name        = "${var.name}-tiles"
  min_ttl     = 1
  default_ttl = 30 * 24 * 3600
  max_ttl     = 30 * 24 * 3600

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "origin_cache_control" {
  name = "UseOriginCacheControlHeaders-QueryStrings"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

# -------------------------------------------------------------- distribution
locals {
  all_methods = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]

  # A list, not a map: CloudFront matches behaviours in order.
  api_behaviours = [
    # Honours the 30 s `Cache-Control` (private datasets say `private`).
    { path = "/api/v1/datasets/*/current", cache_policy = data.aws_cloudfront_cache_policy.origin_cache_control.id },
    { path = "/api/*", cache_policy = data.aws_cloudfront_cache_policy.disabled.id },
  ]
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  comment             = var.name
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  price_class         = "PriceClass_100"
  default_root_object = "index.html"

  origin {
    origin_id                = "web"
    domain_name              = aws_s3_bucket.this["web"].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    origin_id                = "publish"
    domain_name              = aws_s3_bucket.this["publish"].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    origin_id   = "api"
    domain_name = aws_lb.api.dns_name
    vpc_origin_config {
      vpc_origin_id = aws_cloudfront_vpc_origin.api.id
    }
  }

  # Static pages (they are uploaded with `Cache-Control: no-cache`).
  default_cache_behavior {
    target_origin_id           = "web"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }

  # Public archives: immutable and content-addressed.
  ordered_cache_behavior {
    path_pattern               = "/tiles/public/*"
    target_origin_id           = "publish"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.tiles.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.strip_tiles_prefix.arn
    }
  }

  # Private archives: the same objects, only with a valid signed cookie.
  ordered_cache_behavior {
    path_pattern               = "/tiles/private/*"
    target_origin_id           = "publish"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.tiles.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.private_tiles.id
    trusted_key_groups         = [aws_cloudfront_key_group.cookies.id]

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.strip_tiles_prefix.arn
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.api_behaviours
    content {
      path_pattern             = ordered_cache_behavior.value.path
      target_origin_id         = "api"
      viewer_protocol_policy   = "redirect-to-https"
      allowed_methods          = local.all_methods
      cached_methods           = ["GET", "HEAD"]
      cache_policy_id          = ordered_cache_behavior.value.cache_policy
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id

      function_association {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.real_ip.arn
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
