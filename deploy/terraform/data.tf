# ----------------------------------------------------------------------- RDS
resource "aws_security_group" "db" {
  name   = "${var.name}-db"
  vpc_id = module.vpc.vpc_id

  ingress {
    description     = "Postgres from the EKS nodes (pods share the node SG)"
    protocol        = "tcp"
    from_port       = 5432
    to_port         = 5432
    security_groups = [module.eks.node_security_group_id]
  }
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = module.vpc.private_subnets
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.t4g.micro"
  multi_az       = true

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "pmtiles"
  username = "postgres"
  # Only the migration Job uses it; the services log in with IAM tokens.
  manage_master_user_password         = true
  iam_database_authentication_enabled = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]

  # A demo stack: `terraform destroy` removes everything, data included.
  skip_final_snapshot = true
  deletion_protection = false
  apply_immediately   = true
}
