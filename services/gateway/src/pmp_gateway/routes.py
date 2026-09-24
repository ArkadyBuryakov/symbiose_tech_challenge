"""The route table.

Routing is data, not code: a YAML file says which path prefixes go to which
upstream, under which authentication policy and rate-limit class. Adding an
endpoint to the backend needs no gateway change, and the security posture of
every path is reviewable in one screen.

Matching is longest-prefix-first, so a specific rule (``/api/v1/admin/``) always
beats a general one (``/api/v1/``) regardless of the order they are written in.
Anything that matches nothing is a 404 — the gateway is an allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

__all__ = ["AuthPolicy", "Route", "RouteTable", "load_route_table"]


class AuthPolicy(StrEnum):
    """What the gateway requires before forwarding a request."""

    PUBLIC = "public"
    """No identity check; the request is forwarded untouched (no internal token)."""

    OPTIONAL = "optional"
    """Verify a credential if one is present, otherwise forward as anonymous."""

    AUTHENTICATED = "authenticated"
    """A valid credential is required; 401 otherwise."""

    PLATFORM_ADMIN = "platform_admin"
    """Authenticated *and* holding the platform admin role; 403 otherwise."""


@dataclass(frozen=True, slots=True)
class Route:
    prefix: str
    upstream: str
    policy: AuthPolicy
    methods: frozenset[str] | None = None
    rate_limit: str = "default"
    strip_prefix: bool = False

    def matches(self, path: str, method: str) -> bool:
        if not path.startswith(self.prefix):
            return False
        return self.methods is None or method.upper() in self.methods


@dataclass(frozen=True, slots=True)
class RouteTable:
    routes: tuple[Route, ...]

    def match(self, path: str, method: str) -> Route | None:
        """Longest matching prefix wins; ``None`` means 404."""
        for route in self.routes:
            if route.matches(path, method):
                return route
        return None


def _parse_route(raw: dict[str, Any], index: int) -> Route:
    where = f"routes[{index}]"
    try:
        prefix = str(raw["prefix"])
        upstream = str(raw["upstream"])
        policy = AuthPolicy(str(raw["policy"]))
    except KeyError as exc:
        raise ValueError(f"{where} is missing required key {exc}") from None
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None

    if not prefix.startswith("/"):
        raise ValueError(f"{where}: prefix must start with '/' (got {prefix!r})")

    methods = raw.get("methods")
    return Route(
        prefix=prefix,
        upstream=upstream,
        policy=policy,
        methods=frozenset(m.upper() for m in methods) if methods else None,
        rate_limit=str(raw.get("rate_limit", "default")),
        strip_prefix=bool(raw.get("strip_prefix", False)),
    )


def load_route_table(path: str | Path) -> RouteTable:
    """Load and validate the route table, failing fast on anything malformed."""
    content = Path(path).read_text(encoding="utf-8")
    document = yaml.safe_load(content) or {}
    raw_routes = document.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ValueError(f"{path}: 'routes' must be a non-empty list")

    routes = [_parse_route(raw, index) for index, raw in enumerate(raw_routes)]

    # Sorting here rather than trusting the file means a reviewer cannot
    # accidentally shadow `/api/v1/admin/` by putting `/api/v1/` above it.
    routes.sort(key=lambda r: (-len(r.prefix), r.prefix))
    return RouteTable(routes=tuple(routes))
