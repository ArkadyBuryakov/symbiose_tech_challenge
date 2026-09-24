"""Scenarios 8-11: private delivery, identity hygiene, revocation, cookies."""

from __future__ import annotations

import time

import httpx
import pytest

from .conftest import API, BASE_URL, USERS, Archive, Session, inside, sign_in

VERIFY_CACHE_TTL = 10  # GATEWAY_VERIFY_CACHE_TTL in .env.example


def tile_status(client: httpx.Client | None, url: str) -> int:
    requester = client or httpx.Client(base_url=BASE_URL)
    return requester.get(f"{BASE_URL}{url}", headers={"Range": "bytes=0-126"}).status_code


# --------------------------------------------------------------------------
# 8. Private datasets
# --------------------------------------------------------------------------
def test_8_private_tiles_are_isolated_by_tenant(
    alice: Session, bob: Session, anon: Session, slug: str, archive_v1: Archive
) -> None:
    job = alice.publish_and_wait(slug, archive_v1, visibility="private")
    current = alice.current(job["dataset_id"])
    url = current["url"]
    assert url.startswith("/tiles/private/org_tenant-a/")

    # Without a cookie, nobody gets in.
    assert tile_status(None, url) == 403

    # Alice's tenant-wide cookie opens it, with a private cache policy.
    session = alice.post(f"{API}/tiles/session")
    assert session.status_code == 200, session.text
    assert session.json()["resource"].endswith("/tiles/private/org_tenant-a/*")
    tile = alice.http.get(f"{BASE_URL}{url}", headers={"Range": "bytes=0-126"})
    assert tile.status_code == 206
    assert tile.headers["cache-control"].startswith("private")

    # Bob holds a perfectly valid cookie — for tenant-b. It must not work here.
    assert bob.post(f"{API}/tiles/session").status_code == 200
    assert tile_status(bob.http, url) == 403

    # Anonymous callers cannot obtain a cookie at all.
    assert anon.post(f"{API}/tiles/session").status_code == 401


def test_8b_private_datasets_are_invisible_to_other_tenants(
    alice: Session, bob: Session, anon: Session, slug: str, archive_v1: Archive
) -> None:
    job = alice.publish_and_wait(slug, archive_v1, visibility="private")
    dataset_id = job["dataset_id"]

    assert alice.get(f"{API}/datasets/{dataset_id}").status_code == 200
    # 404, not 403: another tenant should not even learn that it exists.
    assert bob.get(f"{API}/datasets/{dataset_id}").status_code == 404
    assert anon.get(f"{API}/datasets/{dataset_id}/current").status_code == 404

    listed = {d["id"] for d in bob.get(f"{API}/datasets", params={"limit": 500}).json()["items"]}
    assert dataset_id not in listed


def test_8c_platform_admin_sees_every_tenant(
    alice: Session, admin: Session, slug: str, archive_v1: Archive
) -> None:
    job = alice.publish_and_wait(slug, archive_v1, visibility="private")

    listed = admin.get(f"{API}/admin/datasets", params={"limit": 500})
    assert listed.status_code == 200
    assert job["dataset_id"] in {d["id"] for d in listed.json()["items"]}

    session = admin.post(f"{API}/tiles/session")
    assert session.json()["resource"].endswith("/tiles/private/*")
    assert tile_status(admin.http, alice.current(job["dataset_id"])["url"]) == 206


def test_8d_non_admins_cannot_reach_admin_routes(alice: Session, anon: Session) -> None:
    assert alice.get(f"{API}/admin/datasets").status_code == 403
    assert anon.get(f"{API}/admin/datasets").status_code == 401


def test_8e_a_revoked_session_cannot_refresh_its_tile_cookie() -> None:
    client = sign_in(*USERS["alice"])
    assert client.post(f"{API}/tiles/session").status_code == 200
    cookie = client.cookies.get("better-auth.session_token")

    assert client.post("/api/auth/sign-out", json={}).status_code == 200
    time.sleep(VERIFY_CACHE_TTL + 1)

    # Re-present the old session cookie, as a stolen one would be.
    replay = httpx.Client(base_url=BASE_URL, headers={"Origin": BASE_URL})
    replay.cookies.set("better-auth.session_token", cookie or "")
    assert replay.post(f"{API}/tiles/session").status_code == 401


# --------------------------------------------------------------------------
# 9. Identity comes only from the gateway
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "headers",
    [
        {"x-tenant-id": "org_tenant-b"},
        {"x-user-id": "someone-else", "x-tenant-id": "org_tenant-b", "x-tenant-role": "owner"},
        {"x-platform-role": "admin"},
        {"x-internal-token": "anything"},
    ],
)
def test_9_client_identity_headers_are_ignored(alice: Session, headers: dict[str, str]) -> None:
    jobs = alice.get(f"{API}/publications", params={"limit": 50}, headers=headers)

    assert jobs.status_code == 200
    assert {j["tenant_id"] for j in jobs.json()["items"]} <= {"org_tenant-a"}
    # And none of them unlock the admin surface.
    assert alice.get(f"{API}/admin/datasets", headers=headers).status_code == 403


def test_9b_a_forged_bearer_token_does_not_authenticate(anon: Session) -> None:
    forged = (
        "eyJhbGciOiJub25lIn0."
        "eyJzdWIiOiJhdHRhY2tlciIsInRlbmFudF9pZCI6Im9yZ190ZW5hbnQtYSIsImF1ZCI6ImJhY2tlbmQifQ."
    )

    response = anon.get(f"{API}/publications", headers={"authorization": f"Bearer {forged}"})

    assert response.status_code == 401


def test_9c_the_backend_rejects_requests_without_a_valid_internal_token() -> None:
    """Reached directly from inside the network, bypassing the gateway."""
    unsigned = inside(
        "worker",
        "curl -s -o /dev/null -w '%{http_code}' http://backend:8000/api/v1/publications",
    )
    forged = inside(
        "worker",
        "curl -s -o /dev/null -w '%{http_code}' "
        "-H 'authorization: Bearer eyJhbGciOiJub25lIn0."
        "eyJzdWIiOiJhIiwiYXVkIjoiYmFja2VuZCIsImlzcyI6ImdhdGV3YXkiLCJleHAiOjk5OTk5OTk5OTl9.' "
        "http://backend:8000/api/v1/publications",
    )
    spoofed = inside(
        "worker",
        "curl -s -o /dev/null -w '%{http_code}' "
        "-H 'x-user-id: admin' -H 'x-tenant-id: org_tenant-a' "
        "http://backend:8000/api/v1/publications",
    )

    assert unsigned == "401"
    assert forged == "401"
    assert spoofed == "401"


@pytest.mark.parametrize("path", ["/api/internal/verify", "/api/metrics", "/internal/verify"])
def test_9d_internal_surfaces_are_not_reachable_from_the_edge(anon: Session, path: str) -> None:
    assert anon.get(f"{BASE_URL}{path}").status_code == 404


# --------------------------------------------------------------------------
# 10. Revocation
# --------------------------------------------------------------------------
def test_10_a_revoked_session_stops_working_within_the_cache_ttl() -> None:
    client = sign_in(*USERS["alice"])
    assert client.get(f"{API}/publications").status_code == 200
    cookie = client.cookies.get("better-auth.session_token")

    # Revoke server-side.
    assert client.post("/api/auth/sign-out", json={}).status_code == 200

    replay = httpx.Client(base_url=BASE_URL, headers={"Origin": BASE_URL})
    replay.cookies.set("better-auth.session_token", cookie or "")

    deadline = time.monotonic() + VERIFY_CACHE_TTL + 3
    status = replay.get(f"{API}/publications").status_code
    while status == 200 and time.monotonic() < deadline:
        time.sleep(1)
        status = replay.get(f"{API}/publications").status_code

    assert status == 401, f"revoked session still accepted after {VERIFY_CACHE_TTL + 3}s"


# --------------------------------------------------------------------------
# 11. Set-Cookie survives the gateway
# --------------------------------------------------------------------------
def test_11_sign_in_cookies_are_identical_through_the_gateway() -> None:
    """Compare what the browser receives with what the auth service emitted."""
    email, password = USERS["alice"]
    through_gateway = httpx.post(
        f"{BASE_URL}/api/auth/sign-in/email",
        json={"email": email, "password": password},
        headers={"Origin": BASE_URL},
    )
    direct = inside(
        "gateway",
        "curl -s -D - -o /dev/null -X POST http://auth:3000/api/auth/sign-in/email "
        f"-H 'content-type: application/json' -H 'origin: {BASE_URL}' "
        f'-d \'{{"email":"{email}","password":"{password}"}}\'',
    )

    def cookie_names(values: list[str]) -> list[str]:
        return sorted(v.split("=", 1)[0].strip() for v in values)

    gateway_cookies = through_gateway.headers.get_list("set-cookie")
    direct_cookies = [
        line.split(":", 1)[1].strip()
        for line in direct.splitlines()
        if line.lower().startswith("set-cookie:")
    ]
    assert gateway_cookies, "sign-in set no cookies"
    assert cookie_names(gateway_cookies) == cookie_names(direct_cookies)


def test_11b_all_three_cloudfront_cookies_survive_the_gateway(alice: Session) -> None:
    """The tile session is the multi-cookie response that matters most: losing
    any one of the three makes every private tile 403."""
    response = alice.post(f"{API}/tiles/session")

    names = sorted(v.split("=", 1)[0] for v in response.headers.get_list("set-cookie"))
    assert names == ["CloudFront-Key-Pair-Id", "CloudFront-Policy", "CloudFront-Signature"]
    for value in response.headers.get_list("set-cookie"):
        assert "HttpOnly" in value
        assert "Path=/tiles/private/" in value


# --------------------------------------------------------------------------
# 12. One caller's session never leaks to another
# --------------------------------------------------------------------------
def test_12_a_sign_in_does_not_leak_to_other_callers(alice: Session) -> None:
    """Regression: the gateway's shared HTTP client stored Set-Cookie headers in
    its cookie jar and attached them to every later request, so anonymous
    callers came back signed in as whoever signed in last."""
    fresh = sign_in(*USERS["bob"])  # a sign-in passes through the gateway now
    fresh.close()

    anonymous = httpx.get(f"{BASE_URL}/api/auth/get-session", headers={"Origin": BASE_URL})
    assert anonymous.status_code == 200
    assert anonymous.json() is None, f"anonymous caller got a session: {anonymous.text[:200]}"

    # Nor may an anonymous caller reach authenticated API routes.
    assert httpx.get(f"{API}/publications").status_code == 401

    # And a signed-in caller still sees only themselves.
    me = alice.get("/api/auth/get-session").json()
    assert me["user"]["email"] == USERS["alice"][0]
