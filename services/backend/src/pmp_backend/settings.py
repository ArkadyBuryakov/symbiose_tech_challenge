"""Backend configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from pmp_common.config import (
    DbSettings,
    KafkaSettings,
    S3Settings,
    ServiceSettings,
)

__all__ = ["BackendSettings", "get_settings"]

AuthMode = Literal["internal_jwt", "dev_stub"]


class BackendSettings(ServiceSettings):
    """Everything the catalogue API needs, validated at startup."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    service_name: str = "backend"
    http_port: int = Field(default=8000, validation_alias="HTTP_PORT")

    db: DbSettings = Field(default_factory=DbSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    s3: S3Settings = Field(default_factory=S3Settings)

    # --- identity ---------------------------------------------------------
    # `internal_jwt` is the real mode: trust only tokens signed by the gateway.
    # `dev_stub` exists so the API can be developed and exercised before the
    # gateway and auth service exist; it refuses to start outside ENVIRONMENT=local.
    auth_mode: AuthMode = Field(default="internal_jwt", validation_alias="BACKEND_AUTH_MODE")
    internal_jwt_public_key_path: str = Field(
        default="/run/keys/internal-jwt.pub",
        validation_alias="INTERNAL_JWT_PUBLIC_KEY_PATH",
    )
    internal_jwt_audience: str = Field(default="backend", validation_alias="INTERNAL_JWT_AUDIENCE")

    dev_stub_user_id: str = Field(default="dev-user", validation_alias="BACKEND_DEV_USER_ID")
    dev_stub_tenant_id: str = Field(default="dev-tenant", validation_alias="BACKEND_DEV_TENANT_ID")

    # --- features ---------------------------------------------------------
    demo_upload_enabled: bool = Field(default=False, validation_alias="DEMO_UPLOAD_ENABLED")

    # --- private tile cookies (phase 5) -----------------------------------
    cloudfront_private_key_path: str = Field(
        default="/run/keys/cloudfront.key", validation_alias="CLOUDFRONT_PRIVATE_KEY_PATH"
    )
    cloudfront_key_pair_id: str = Field(
        default="LOCALKEYPAIRID", validation_alias="CLOUDFRONT_KEY_PAIR_ID"
    )
    tile_cookie_ttl_seconds: int = Field(
        default=600, ge=60, le=86400, validation_alias="TILE_COOKIE_TTL_SECONDS"
    )

    # --- listing limits ---------------------------------------------------
    max_page_size: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def _guard_dev_stub(self) -> BackendSettings:
        if self.auth_mode == "dev_stub" and self.environment != "local":
            raise ValueError(
                "BACKEND_AUTH_MODE=dev_stub is only allowed when ENVIRONMENT=local; "
                "it disables authentication entirely"
            )
        return self


def get_settings() -> BackendSettings:
    """Build settings. Called once at startup so failures are fatal and early."""
    return BackendSettings()
