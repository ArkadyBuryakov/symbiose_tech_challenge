"""Edge verifier: validates CloudFront signed cookies for nginx.

On AWS, CloudFront itself checks the signed cookies on ``/tiles/private/*`` and
this service does not exist. Locally, nginx's ``auth_request`` asks it the same
question for every private tile read:

    GET /verify
    X-Original-URI: /tiles/private/<tenant>/<dataset>/<sha>/data.pmtiles
    Cookie: CloudFront-Policy=...; CloudFront-Signature=...; CloudFront-Key-Pair-Id=...

and gets ``204`` (serve it) or ``403`` (do not). Because the cookie format is
CloudFront's own, the backend that issues the cookies needs no change on AWS.

It holds only the **public** key. It cannot issue cookies, only check them.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from prometheus_client import Counter
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from pmp_common.cloudfront import (
    COOKIE_KEY_PAIR_ID,
    COOKIE_POLICY,
    COOKIE_SIGNATURE,
    CloudFrontVerifier,
    PolicyError,
)
from pmp_common.config import ServiceSettings
from pmp_common.logging import configure_logging, get_logger
from pmp_common.web import create_app

__all__ = ["EdgeVerifierSettings", "build_app"]

log = get_logger(__name__)

TILE_DECISIONS = Counter(
    "edge_tile_authorisations_total",
    "Private tile authorisation decisions.",
    ["decision", "reason"],
)


class EdgeVerifierSettings(ServiceSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    service_name: str = "edge-verifier"
    cloudfront_public_key_path: str = Field(
        default="/run/keys/cloudfront.pub", validation_alias="CLOUDFRONT_PUBLIC_KEY_PATH"
    )
    cloudfront_key_pair_id: str = Field(
        default="LOCALKEYPAIRID", validation_alias="CLOUDFRONT_KEY_PAIR_ID"
    )


def _reason(exc: PolicyError) -> str:
    """A low-cardinality label for metrics, derived from the refusal."""
    text = str(exc)
    for needle, label in (
        ("missing", "missing"),
        ("malformed", "malformed"),
        ("key pair", "unknown_key"),
        ("signature", "bad_signature"),
        ("expired", "expired"),
        ("cover", "out_of_scope"),
    ):
        if needle in text:
            return label
    return "invalid"


def build_app(settings: EdgeVerifierSettings | None = None) -> FastAPI:
    settings = settings or EdgeVerifierSettings()
    configure_logging(
        service=settings.service_name,
        level=settings.observability.log_level,
        fmt=settings.observability.log_format,
        git_sha=settings.git_sha,
    )
    verifier = CloudFrontVerifier.from_file(
        settings.cloudfront_public_key_path, key_pair_id=settings.cloudfront_key_pair_id
    )

    app = create_app(
        title="Edge verifier",
        service=settings.service_name,
        version=settings.git_sha,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/verify", include_in_schema=False)
    async def verify(request: Request) -> Response:
        original_uri = request.headers.get("x-original-uri", "")
        url = f"{settings.public_base_url}{original_uri}"
        try:
            verifier.verify(
                policy_b64=request.cookies.get(COOKIE_POLICY),
                signature_b64=request.cookies.get(COOKIE_SIGNATURE),
                key_pair_id=request.cookies.get(COOKIE_KEY_PAIR_ID),
                url=url,
            )
        except PolicyError as exc:
            reason = _reason(exc)
            TILE_DECISIONS.labels("deny", reason).inc()
            # Logged without the cookie values: they are credentials.
            log.info("tiles.denied", path=original_uri.split("?", 1)[0], reason=reason)
            return Response(status_code=403)

        TILE_DECISIONS.labels("allow", "ok").inc()
        return Response(status_code=204)

    return app
