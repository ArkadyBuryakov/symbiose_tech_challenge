# Builds the images locally and pushes them to ECR during `terraform apply`.
# The tag is a hash of the sources, so an unchanged tree rebuilds nothing.

locals {
  repo_root = abspath("${path.module}/../..")

  python_image = { context = ".", dockerfile = "ops/docker/python.Dockerfile" }
  images = {
    gateway = merge(local.python_image, { args = { PACKAGE = "pmp-gateway", SERVICE_DIR = "services/gateway", MODULE = "pmp_gateway", HEALTH_PORT = "8000" } })
    backend = merge(local.python_image, { args = { PACKAGE = "pmp-backend", SERVICE_DIR = "services/backend", MODULE = "pmp_backend", HEALTH_PORT = "8000" } })
    worker  = merge(local.python_image, { args = { PACKAGE = "pmp-worker", SERVICE_DIR = "services/worker", MODULE = "pmp_worker", HEALTH_PORT = "9100" } })
    migrate = { context = ".", dockerfile = "ops/migrate/Dockerfile", args = {} }
    auth    = { context = "services/auth", dockerfile = "services/auth/Dockerfile", args = {} }
  }

  image_sources = sort(setunion(
    fileset(local.repo_root, "{pyproject.toml,uv.lock,.python-version,alembic.ini}"),
    fileset(local.repo_root, "{packages,services}/*/{pyproject.toml,Dockerfile}"),
    fileset(local.repo_root, "{packages,services}/*/src/**/*.{py,ts,typed}"),
    fileset(local.repo_root, "services/auth/{package.json,package-lock.json,tsconfig.json}"),
    fileset(local.repo_root, "ops/{docker,migrate,db}/*"),
    fileset(local.repo_root, "migrations/**/*.{py,mako}"),
  ))
  image_tag = substr(sha1(join("", [for f in local.image_sources : filesha1("${local.repo_root}/${f}")])), 0, 12)
  registry  = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

resource "aws_ecr_repository" "this" {
  for_each = local.images

  name         = "${var.name}/${each.key}"
  force_delete = true
}

resource "terraform_data" "image" {
  for_each = local.images

  triggers_replace = [local.image_tag, aws_ecr_repository.this[each.key].repository_url]

  provisioner "local-exec" {
    working_dir = local.repo_root
    interpreter = ["bash", "-euo", "pipefail", "-c"]
    command     = <<-EOT
      aws ecr get-login-password --region ${var.region} \
        | docker login --username AWS --password-stdin ${local.registry}
      docker build --platform linux/amd64 -f ${each.value.dockerfile} \
        ${join(" ", [for k, v in each.value.args : "--build-arg ${k}=${v}"])} \
        --build-arg GIT_SHA=${local.image_tag} \
        -t ${aws_ecr_repository.this[each.key].repository_url}:${local.image_tag} ${each.value.context}
      docker push ${aws_ecr_repository.this[each.key].repository_url}:${local.image_tag}
    EOT
  }
}
