"""Gateway configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from pmp_common.config import ServiceSettings

__all__ = ["GatewaySettings", "get_settings"]


class GatewaySettings(ServiceSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    service_name: str = "gateway"
    http_port: int = Field(default=8000, validation_alias="HTTP_PORT")

    routes_file: Path = Field(
        default=Path("/app/routes.yaml"),
        validation_alias="GATEWAY_ROUTES_FILE",
        description="Route table: path prefixes, upstreams, auth policies, rate-limit classes.",
    )

    # --- upstreams --------------------------------------------------------
    auth_base_url: str = Field(default="http://auth:3000", validation_alias="AUTH_BASE_URL")
    backend_base_url: str = Field(
        default="http://backend:8000", validation_alias="BACKEND_BASE_URL"
    )

    # --- identity ---------------------------------------------------------
    internal_jwt_private_key_path: str = Field(
        default="/run/keys/internal-jwt.key",
        validation_alias="INTERNAL_JWT_PRIVATE_KEY_PATH",
    )
    internal_jwt_ttl_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        validation_alias="INTERNAL_JWT_TTL_SECONDS",
        description="Short by design: the token is minted per request, not stored.",
    )

    verify_cache_ttl_seconds: float = Field(
        default=10.0,
        ge=0.0,
        le=300.0,
        validation_alias="GATEWAY_VERIFY_CACHE_TTL",
        description=(
            "How long a *successful* verification is reused. This is the upper bound on "
            "how long a revoked session keeps working against the API."
        ),
    )
    verify_cache_max_entries: int = Field(
        default=10_000, ge=16, validation_alias="GATEWAY_VERIFY_CACHE_MAX"
    )
    verify_timeout_seconds: float = Field(
        default=3.0, ge=0.5, validation_alias="GATEWAY_VERIFY_TIMEOUT"
    )

    # --- rate limiting ----------------------------------------------------
    rate_limit_rps: float = Field(default=20.0, ge=0.1, validation_alias="GATEWAY_RATE_LIMIT_RPS")
    rate_limit_burst: int = Field(default=40, ge=1, validation_alias="GATEWAY_RATE_LIMIT_BURST")
    rate_limit_max_buckets: int = Field(
        default=50_000, ge=16, validation_alias="GATEWAY_RATE_LIMIT_MAX_BUCKETS"
    )

    # --- proxy ------------------------------------------------------------
    connect_timeout_seconds: float = Field(
        default=2.0, ge=0.1, validation_alias="GATEWAY_CONNECT_TIMEOUT"
    )
    read_timeout_seconds: float = Field(
        default=30.0, ge=1.0, validation_alias="GATEWAY_READ_TIMEOUT"
    )
    max_connections: int = Field(default=200, ge=1, validation_alias="GATEWAY_MAX_CONNECTIONS")
    max_keepalive_connections: int = Field(
        default=50, ge=1, validation_alias="GATEWAY_MAX_KEEPALIVE"
    )

    @model_validator(mode="after")
    def _keepalive_within_max(self) -> GatewaySettings:
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError(
                "GATEWAY_MAX_KEEPALIVE cannot exceed GATEWAY_MAX_CONNECTIONS "
                f"({self.max_keepalive_connections} > {self.max_connections})"
            )
        return self

    def upstream_url(self, name: str) -> str:
        urls = {"auth": self.auth_base_url, "backend": self.backend_base_url}
        try:
            return urls[name].rstrip("/")
        except KeyError:
            raise ValueError(
                f"route table references unknown upstream {name!r}; known: {sorted(urls)}"
            ) from None


def get_settings() -> GatewaySettings:
    return GatewaySettings()
