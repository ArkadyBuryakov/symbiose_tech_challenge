"""End-to-end fixtures: real HTTP against the running stack.

Everything goes through the public edge (``http://localhost:8080``) exactly as
a browser or a producer would. The few checks that need to reach *inside* the
network — producing a raw Kafka message, calling the backend directly to prove
it rejects unsigned requests, rewriting a staged object — use
``docker compose exec``, never a published debug port.

Run with ``make e2e`` after ``make up && make seed``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
API = f"{BASE_URL}/api/v1"

USERS = {
    "alice": ("alice@tenant-a.test", "demo-password-alice"),
    "bob": ("bob@tenant-b.test", "demo-password-bob"),
    "admin": ("admin@platform.test", "demo-password-admin"),
}

SPEC_PATH = REPO_ROOT / "sample-data" / "input_forest_crowns_pmtiles.spec.json"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session", autouse=True)
def _stack_is_up() -> None:
    try:
        httpx.get(f"{BASE_URL}/healthz", timeout=3).raise_for_status()
        httpx.get(f"{BASE_URL}/api/auth/ok", timeout=3).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.exit(f"stack not reachable at {BASE_URL} ({exc}); run `make up && make seed`")


# --------------------------------------------------------------------------
# docker compose helpers (for the few checks that must go inside the network)
# --------------------------------------------------------------------------
def compose(*args: str, input_bytes: bytes | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker compose {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode(errors='replace')[-800:]}"
        )
    return result.stdout.decode(errors="replace")


def inside(service: str, command: str) -> str:
    """Run a shell command inside a compose service."""
    return compose("exec", "-T", service, "sh", "-c", command)


# --------------------------------------------------------------------------
# Archives
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Archive:
    path: Path
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def sha256_b64(self) -> str:
        return base64.b64encode(hashlib.sha256(self.data).digest()).decode()


def _synthetic(name: str, variant: int) -> Path:
    path = REPO_ROOT / "sample-data" / name
    if not path.exists():
        subprocess.run(
            [
                str(REPO_ROOT / "scripts" / "make-synthetic-pmtiles.sh"),
                str(path),
                "--variant",
                str(variant),
            ],
            check=True,
            capture_output=True,
            timeout=900,
        )
    return path


@pytest.fixture(scope="session")
def archive_v1() -> Archive:
    path = _synthetic("synthetic.pmtiles", 0)
    return Archive(path, path.read_bytes())


@pytest.fixture(scope="session")
def archive_v2() -> Archive:
    path = _synthetic("synthetic-v2.pmtiles", 2)
    return Archive(path, path.read_bytes())


@pytest.fixture(scope="session")
def spec() -> dict[str, Any] | None:
    import json

    return json.loads(SPEC_PATH.read_text()) if SPEC_PATH.exists() else None


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
class Session:
    """An API client for one caller, with the helpers every test needs."""

    def __init__(self, client: httpx.Client, *, email: str | None = None) -> None:
        self.http = client
        self.email = email

    # ---- raw ------------------------------------------------------------
    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.http.get(path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.http.post(path, **kw)

    def put(self, path: str, **kw: Any) -> httpx.Response:
        return self.http.put(path, **kw)

    # ---- workflow ---------------------------------------------------------
    def stage(self, archive: Archive | bytes) -> str:
        """Upload through the presigned-PUT path and return the staging key."""
        data = archive.data if isinstance(archive, Archive) else archive
        digest = hashlib.sha256(data).digest()
        upload = self.post(
            f"{API}/demo/uploads",
            json={"sha256": digest.hex(), "content_length": len(data)},
        )
        assert upload.status_code == 201, upload.text
        body = upload.json()
        put = httpx.put(
            body["url"],
            content=data,
            headers={**body["headers"], "x-amz-checksum-sha256": base64.b64encode(digest).decode()},
            timeout=60,
        )
        assert put.status_code == 200, put.text
        return str(body["source_key"])

    def publish(
        self,
        slug: str,
        source_key: str,
        *,
        key: str | None = None,
        visibility: str = "public",
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.post(
            f"{API}/publications",
            json={
                "dataset_slug": slug,
                "source_key": source_key,
                "visibility": visibility,
                **({"spec": spec} if spec else {}),
            },
            headers={"Idempotency-Key": key or f"e2e-{uuid.uuid4()}"},
        )
        assert response.status_code == 202, response.text
        result: dict[str, Any] = response.json()
        return result

    def wait(self, job_id: str, *, timeout: float = 90.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job: dict[str, Any] = self.get(f"{API}/publications/{job_id}").json()
            if job["status"] in ("SUCCEEDED", "FAILED"):
                return job
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} still {job['status']} after {timeout}s")
            time.sleep(0.5)

    def publish_and_wait(self, slug: str, archive: Archive, **kw: Any) -> dict[str, Any]:
        accepted = self.publish(slug, self.stage(archive), **kw)
        job = self.wait(accepted["job_id"])
        job["dataset_id"] = accepted["dataset_id"]
        return job

    def versions(self, dataset_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self.get(f"{API}/datasets/{dataset_id}/versions").json()
        return result

    def current(self, dataset_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.get(f"{API}/datasets/{dataset_id}/current").json()
        return result


def sign_in(email: str, password: str) -> httpx.Client:
    """Sign in as a browser would, honouring BetterAuth's sign-in rate limit.

    BetterAuth allows three sign-ins per ten seconds per client IP — a genuine
    credential-stuffing control that is kept, not loosened for tests. Every e2e
    request comes from the same IP, so the suite signs in once per user per
    session, and the few tests that *need* a fresh session (revocation) wait
    out a 429 using the server's own ``x-retry-after``.
    """
    client = httpx.Client(base_url=BASE_URL, headers={"Origin": BASE_URL}, timeout=30)
    for _ in range(8):
        response = client.post(
            "/api/auth/sign-in/email", json={"email": email, "password": password}
        )
        if response.status_code != 429:
            break
        time.sleep(float(response.headers.get("x-retry-after", "10")) + 1)
    assert response.status_code == 200, (
        f"sign-in as {email} failed ({response.status_code}): {response.text} — run `make seed`"
    )
    return client


@pytest.fixture(scope="session")
def alice() -> Iterator[Session]:
    client = sign_in(*USERS["alice"])
    yield Session(client, email=USERS["alice"][0])
    client.close()


@pytest.fixture(scope="session")
def bob() -> Iterator[Session]:
    client = sign_in(*USERS["bob"])
    yield Session(client, email=USERS["bob"][0])
    client.close()


@pytest.fixture(scope="session")
def admin() -> Iterator[Session]:
    client = sign_in(*USERS["admin"])
    yield Session(client, email=USERS["admin"][0])
    client.close()


@pytest.fixture
def anon() -> Iterator[Session]:
    client = httpx.Client(base_url=BASE_URL, headers={"Origin": BASE_URL}, timeout=30)
    yield Session(client)
    client.close()


@pytest.fixture
def slug() -> str:
    """A fresh dataset per test, so tests never depend on each other's state."""
    return f"e2e-{uuid.uuid4().hex[:12]}"
