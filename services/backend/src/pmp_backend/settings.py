"""Backend configuration."""

from __future__ import annotations

from pydantic import Field

from pmp_common.config import (
    DbSettings,
    KafkaSettings,
    S3Settings,
    ServiceSettings,
)

__all__ = ["BackendSettings", "get_settings"]


class BackendSettings(ServiceSettings):
    """Everything the catalogue API needs, validated at startup."""

    service_name: str = "backend"

    db: DbSettings = Field(default_factory=DbSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    s3: S3Settings = Field(default_factory=S3Settings)

    # --- identity: trust only tokens signed by the gateway -----------------
    internal_jwt_public_key_path: str = Field(
        default="/run/keys/internal-jwt.pub",
        validation_alias="INTERNAL_JWT_PUBLIC_KEY_PATH",
    )
    internal_jwt_audience: str = Field(default="backend", validation_alias="INTERNAL_JWT_AUDIENCE")

    # --- features ---------------------------------------------------------
    demo_upload_enabled: bool = Field(default=False, validation_alias="DEMO_UPLOAD_ENABLED")

    # --- private tile cookies ---------------------------------------------
    cloudfront_private_key_path: str = Field(
        default="/run/keys/cloudfront.key", validation_alias="CLOUDFRONT_PRIVATE_KEY_PATH"
    )
    cloudfront_key_pair_id: str = Field(
        default="LOCALKEYPAIRID", validation_alias="CLOUDFRONT_KEY_PAIR_ID"
    )
    tile_cookie_ttl_seconds: int = Field(
        default=600, ge=60, le=86400, validation_alias="TILE_COOKIE_TTL_SECONDS"
    )


def get_settings() -> BackendSettings:
    """Build settings. Called once at startup so failures are fatal and early."""
    return BackendSettings()
