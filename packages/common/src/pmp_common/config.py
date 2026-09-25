"""Typed, fail-fast configuration.

Every service reads its whole configuration from the environment (12-factor).
Settings are validated by pydantic-settings at import/startup time so a
misconfigured container dies immediately with a readable message instead of
failing later on the first request.

The groups below are deliberately shaped so that moving to AWS is a change of
environment variables only:

* ``S3_ENDPOINT`` unset  -> boto3 default endpoint + default credential chain
  (EKS Pod Identity / IRSA) instead of the local MinIO endpoint and static keys.
* ``KAFKA_SASL_MECHANISM=aws-msk-iam`` -> MSK IAM auth instead of PLAINTEXT.
* ``DB_AUTH=iam``       -> RDS IAM auth tokens instead of a static password.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "DbAuthMode",
    "DbSettings",
    "KafkaSettings",
    "ObservabilitySettings",
    "S3Settings",
    "ServiceSettings",
    "read_secret_file",
]

DbAuthMode = Literal["password", "iam"]
SaslMechanism = Literal["none", "aws-msk-iam"]


def _env_config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )


class DbSettings(BaseSettings):
    """Postgres connection settings (``DB_*``)."""

    model_config = _env_config("db_")

    host: str = "postgres"
    port: int = 5432
    name: str = "pmtiles"
    user: str = "postgres"
    password: str | None = None
    sslmode: str = "prefer"
    auth: DbAuthMode = "password"
    connect_timeout: int = 5
    pool_size: int = 5
    pool_max_overflow: int = 5
    pool_recycle_seconds: int = 1800
    statement_timeout_ms: int = 30_000

    @model_validator(mode="after")
    def _check_credentials(self) -> DbSettings:
        if self.auth == "password" and not self.password:
            raise ValueError("DB_PASSWORD is required when DB_AUTH=password")
        return self

    def url(self, *, driver: str, password: str | None) -> str:
        """Build a SQLAlchemy URL for ``driver`` with an explicitly supplied password.

        The password is passed in rather than read from the instance so that
        IAM auth (short-lived tokens) uses the exact same code path.
        """
        from urllib.parse import quote

        secret = f":{quote(password, safe='')}" if password else ""
        return (
            f"postgresql+{driver}://{quote(self.user, safe='')}{secret}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class KafkaSettings(BaseSettings):
    """Kafka / Redpanda settings (``KAFKA_*``)."""

    model_config = _env_config("kafka_")

    bootstrap_servers: str = "kafka:9092"
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: SaslMechanism = "none"
    aws_region: str = "eu-west-1"
    consumer_group: str = "publication-worker"
    # Delivery/consumer tuning; exposed so it can be adjusted per environment.
    request_timeout_ms: int = 30_000
    delivery_timeout_ms: int = 120_000
    max_poll_interval_ms: int = 900_000
    session_timeout_ms: int = 45_000


class S3Settings(BaseSettings):
    """Object storage settings (``S3_*``)."""

    model_config = _env_config("s3_")

    endpoint: str | None = None
    region: str = "us-east-1"
    force_path_style: bool = False
    access_key_id: str | None = None
    secret_access_key: str | None = None
    staging_bucket: str = "staging"
    publish_bucket: str = "publish"
    presign_expiry_seconds: int = 900
    multipart_part_bytes: int = 512 * 1024**2

    @model_validator(mode="after")
    def _check_static_credentials(self) -> S3Settings:
        one_sided = bool(self.access_key_id) != bool(self.secret_access_key)
        if one_sided:
            raise ValueError(
                "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together "
                "(leave both unset to use the default AWS credential chain)"
            )
        return self


class ObservabilitySettings(BaseSettings):
    """Logging / metrics / tracing settings."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, frozen=True)

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_format: Literal["json", "console"] = Field(default="json", validation_alias="LOG_FORMAT")
    # Sampling is configured by the OTel SDK itself from OTEL_TRACES_SAMPLER(_ARG).
    otlp_endpoint: str | None = Field(default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")


class ServiceSettings(BaseSettings):
    """Fields shared by every service."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    service_name: str = "pmp"
    environment: Literal["local", "dev", "staging", "prod"] = Field(
        default="local", validation_alias="ENVIRONMENT"
    )
    git_sha: str = Field(default="unknown", validation_alias="GIT_SHA")
    http_host: str = Field(default="0.0.0.0", validation_alias="HTTP_HOST")  # noqa: S104
    http_port: int = Field(default=8000, validation_alias="HTTP_PORT")
    public_base_url: str = Field(
        default="http://localhost:8080", validation_alias="PUBLIC_BASE_URL"
    )

    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


def read_secret_file(path: str | Path, *, what: str) -> str:
    """Read a secret from a file, failing loudly with a useful message.

    Locally these files come from ``dev-keys/`` (generated by
    ``scripts/gen-dev-keys.sh``); on AWS the same paths are populated from
    Secrets Manager by the Secrets Store CSI driver, so the code is unchanged.
    """
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - exercised only on misconfiguration
        raise ValueError(f"cannot read {what} from {p}: {exc}") from exc
    if not content:
        raise ValueError(f"{what} at {p} is empty")
    return content
