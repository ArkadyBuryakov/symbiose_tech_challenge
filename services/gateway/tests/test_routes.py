"""Route matching — the gateway's allowlist.

A mistake here either exposes an internal endpoint or hands a route the wrong
authentication policy, so the table's behaviour is pinned down explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pmp_gateway.routes import AuthPolicy, load_route_table

ROUTES_FILE = Path(__file__).resolve().parents[1] / "routes.yaml"


@pytest.fixture(scope="module")
def table():  # type: ignore[no-untyped-def]
    return load_route_table(ROUTES_FILE)


@pytest.mark.parametrize(
    ("path", "method", "upstream", "policy"),
    [
        ("/api/auth/sign-in/email", "POST", "auth", AuthPolicy.PUBLIC),
        ("/api/auth/get-session", "GET", "auth", AuthPolicy.PUBLIC),
        ("/api/v1/admin/datasets", "GET", "backend", AuthPolicy.PLATFORM_ADMIN),
        ("/api/v1/datasets", "GET", "backend", AuthPolicy.OPTIONAL),
        ("/api/v1/datasets/abc/current", "GET", "backend", AuthPolicy.OPTIONAL),
        ("/api/v1/publications", "POST", "backend", AuthPolicy.AUTHENTICATED),
        ("/api/v1/demo/uploads", "POST", "backend", AuthPolicy.AUTHENTICATED),
        ("/api/v1/tiles/session", "POST", "backend", AuthPolicy.AUTHENTICATED),
    ],
)
def test_routes_resolve_as_documented(table, path, method, upstream, policy) -> None:  # type: ignore[no-untyped-def]
    route = table.match(path, method)

    assert route is not None, f"{method} {path} matched no route"
    assert route.upstream == upstream
    assert route.policy is policy


def test_admin_prefix_is_not_shadowed_by_the_general_api_rule(table) -> None:  # type: ignore[no-untyped-def]
    """Longest prefix wins, whatever order the file lists the rules in."""
    route = table.match("/api/v1/admin/publications", "GET")

    assert route is not None
    assert route.policy is AuthPolicy.PLATFORM_ADMIN


def test_writing_to_a_dataset_is_not_optional_auth(table) -> None:  # type: ignore[no-untyped-def]
    """The read rule is restricted to GET/HEAD; a rollback must be authenticated."""
    route = table.match("/api/v1/datasets/abc/current", "PUT")

    assert route is not None
    assert route.policy is AuthPolicy.AUTHENTICATED


@pytest.mark.parametrize(
    "path",
    [
        "/internal/verify",
        "/metrics",
        "/healthz",
        "/readyz",
        "/",
        "/api",
        "/api/v2/datasets",
        "/tiles/public/x/y/z/data.pmtiles",
        "/../api/v1/datasets",
    ],
)
def test_everything_not_explicitly_routed_is_a_404(table, path) -> None:  # type: ignore[no-untyped-def]
    """The gateway is an allowlist: `/internal/*` and `/metrics` are unreachable."""
    assert table.match(path, "GET") is None


def test_route_file_is_rejected_when_malformed(tmp_path: Path) -> None:
    bad = tmp_path / "routes.yaml"

    bad.write_text("routes: []")
    with pytest.raises(ValueError, match="non-empty list"):
        load_route_table(bad)

    bad.write_text("routes:\n  - upstream: backend\n    policy: public\n")
    with pytest.raises(ValueError, match="missing required key"):
        load_route_table(bad)

    bad.write_text("routes:\n  - prefix: /x\n    upstream: backend\n    policy: superuser\n")
    with pytest.raises(ValueError, match="superuser"):
        load_route_table(bad)

    bad.write_text("routes:\n  - prefix: api/v1\n    upstream: backend\n    policy: public\n")
    with pytest.raises(ValueError, match="must start with"):
        load_route_table(bad)


def test_every_upstream_in_the_table_is_known(table) -> None:  # type: ignore[no-untyped-def]
    from pmp_gateway.settings import GatewaySettings

    settings = GatewaySettings()
    for route in table.routes:
        assert settings.upstream_url(route.upstream)


def test_unknown_upstream_is_rejected() -> None:
    from pmp_gateway.settings import GatewaySettings

    with pytest.raises(ValueError, match="unknown upstream"):
        GatewaySettings().upstream_url("nowhere")
