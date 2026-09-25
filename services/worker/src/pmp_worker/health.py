"""Probes and metrics for a process that is not an HTTP service.

The worker still needs ``/healthz``, ``/readyz`` and ``/metrics``: Kubernetes
uses the first two, Prometheus the third. A FastAPI/uvicorn stack would be a
disproportionate dependency for three endpoints, so this is a thread running
``http.server``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pmp_common.logging import get_logger
from pmp_common.metrics import CONTENT_TYPE_LATEST, render_metrics

__all__ = ["HealthServer"]

log = get_logger(__name__)

ReadinessCheck = Callable[[], tuple[bool, str]]


class _Handler(BaseHTTPRequestHandler):
    server_version = "pmp-worker"
    readiness: ReadinessCheck
    version: str

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._json(200, {"status": "ok", "service": "worker", "version": self.version})
        elif path == "/readyz":
            ok, detail = type(self).readiness()
            self._json(
                200 if ok else 503, {"status": "ok" if ok else "unavailable", "detail": detail}
            )
        elif path == "/metrics":
            body = render_metrics()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"status": "not found"})

    def _json(self, status: int, body: dict[str, str]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: Any) -> None:
        """Silence the default stderr access log; probes would flood it."""


class HealthServer:
    def __init__(self, *, host: str, port: int, version: str, readiness: ReadinessCheck) -> None:
        handler = type(
            "Handler", (_Handler,), {"readiness": staticmethod(readiness), "version": version}
        )
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health-server", daemon=True
        )
        self._thread.start()
        log.info("worker.health_server_started", port=self._server.server_address[1])

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
