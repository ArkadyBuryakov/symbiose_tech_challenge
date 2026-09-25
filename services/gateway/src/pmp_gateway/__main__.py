"""Run the gateway with uvicorn."""

from __future__ import annotations

import uvicorn

from .settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "pmp_gateway.app:build_app",
        factory=True,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        access_log=False,
        workers=1,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
