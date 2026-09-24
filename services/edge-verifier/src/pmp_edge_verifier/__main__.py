"""Run the edge verifier with uvicorn."""

from __future__ import annotations

import uvicorn

from .app import EdgeVerifierSettings


def main() -> None:
    settings = EdgeVerifierSettings()
    uvicorn.run(
        "pmp_edge_verifier.app:build_app",
        factory=True,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
