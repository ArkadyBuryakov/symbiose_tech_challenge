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

__all__ = ["AuthPolicy", "RateClass", "Route", "RouteTable", "load_route_table"]


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
class RateClass:
    rps: float
    burst: int


@dataclass(frozen=True, slots=True)
class Route:
    prefix: str
    upstream: str
    policy: AuthPolicy
    rate_limit: str
    methods: frozenset[str] | None = None

    def matches(self, path: str, method: str) -> bool:
        if not path.startswith(self.prefix):
            return False
        return self.methods is None or method.upper() in self.methods


@dataclass(frozen=True, slots=True)
class RouteTable:
    routes: tuple[Route, ...]
    rate_limits: dict[str, RateClass]

    def match(self, path: str, method: str) -> Route | None:
        """Longest matching prefix wins; ``None`` means 404."""
        return next((r for r in self.routes if r.matches(path, method)), None)


def _parse_route(raw: dict[str, Any], index: int, rate_limits: dict[str, RateClass]) -> Route:
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
    rate_limit = str(raw.get("rate_limit", "default"))
    if rate_limit not in rate_limits:
        raise ValueError(f"{where}: unknown rate_limit class {rate_limit!r}")

    methods = raw.get("methods")
    return Route(
        prefix=prefix,
        upstream=upstream,
        policy=policy,
        rate_limit=rate_limit,
        methods=frozenset(m.upper() for m in methods) if methods else None,
    )


def load_route_table(path: str | Path) -> RouteTable:
    """Load and validate the route table, failing fast on anything malformed."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    raw_classes = document.get("rate_limits")
    if not isinstance(raw_classes, dict) or not raw_classes:
        raise ValueError(f"{path}: 'rate_limits' must be a non-empty mapping")
    try:
        rate_limits = {
            str(name): RateClass(rps=float(c["rps"]), burst=int(c["burst"]))
            for name, c in raw_classes.items()
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path}: each rate_limits class needs numeric rps and burst") from exc

    raw_routes = document.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ValueError(f"{path}: 'routes' must be a non-empty list")
    routes = [_parse_route(raw, i, rate_limits) for i, raw in enumerate(raw_routes)]

    # Sorting here rather than trusting the file means a reviewer cannot
    # accidentally shadow `/api/v1/admin/` by putting `/api/v1/` above it.
    routes.sort(key=lambda r: (-len(r.prefix), r.prefix))
    return RouteTable(routes=tuple(routes), rate_limits=rate_limits)
