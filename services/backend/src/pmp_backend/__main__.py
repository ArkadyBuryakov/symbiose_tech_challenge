"""Run the backend with uvicorn.

A module entry point (``python -m pmp_backend``) rather than a console script,
so the container command is identical for every Python service.
"""

from __future__ import annotations

import uvicorn

from .settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "pmp_backend.app:build_app",
        factory=True,
        host=settings.http_host,
        port=settings.http_port,
        # Logging is configured by the app itself (structlog JSON); uvicorn's
        # own config would otherwise install a second, differently-shaped one.
        log_config=None,
        access_log=False,
        # One worker per container: scaling is done with replicas, so the
        # process stays single-purpose and its metrics stay interpretable.
        workers=1,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
