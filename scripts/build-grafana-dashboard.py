"""Generate the provisioned Grafana dashboard.

The dashboard JSON is generated rather than hand-edited: panel ids and grid
positions are mechanical, and a generator keeps the queries readable in review.

    uv run python scripts/build-grafana-dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = (
    Path(__file__).resolve().parents[1]
    / "ops/observability/grafana/dashboards/pmtiles-platform.json"
)
DS = {"type": "prometheus", "uid": "prometheus"}

_next_id = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def row(title: str, y: int) -> dict[str, Any]:
    return {
        "type": "row",
        "id": _id(),
        "title": title,
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "panels": [],
    }


def timeseries(
    title: str,
    targets: list[tuple[str, str]],
    *,
    x: int,
    y: int,
    w: int = 8,
    h: int = 8,
    unit: str = "short",
    description: str = "",
    stack: bool = False,
) -> dict[str, Any]:
    return {
        "type": "timeseries",
        "id": _id(),
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "fillOpacity": 12,
                    "showPoints": "never",
                    "stacking": {"mode": "normal" if stack else "none"},
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi"},
        },
        "targets": [
            {"refId": chr(65 + i), "expr": expr, "legendFormat": legend, "datasource": DS}
            for i, (expr, legend) in enumerate(targets)
        ],
    }


def stat(
    title: str,
    expr: str,
    *,
    x: int,
    y: int,
    w: int = 4,
    h: int = 4,
    unit: str = "short",
    description: str = "",
    thresholds: list[tuple[float | None, str]] | None = None,
) -> dict[str, Any]:
    steps = [{"value": value, "color": color} for value, color in (thresholds or [(None, "green")])]
    return {
        "type": "stat",
        "id": _id(),
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": steps},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "value",
            "graphMode": "area",
            "textMode": "value",
        },
        "targets": [{"refId": "A", "expr": expr, "datasource": DS}],
    }


def build() -> dict[str, Any]:
    panels: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- health
    panels.append(row("At a glance", 0))
    panels += [
        stat(
            "Jobs in flight",
            "sum(worker_inflight_jobs) or vector(0)",
            x=0,
            y=1,
            description="Publication jobs currently being processed across all workers.",
        ),
        stat(
            "Outbox backlog",
            "max(outbox_pending) or vector(0)",
            x=4,
            y=1,
            description="Result events committed but not yet on Kafka. Should hover near 0.",
            thresholds=[(None, "green"), (50, "orange"), (500, "red")],
        ),
        stat(
            "DLQ (1h)",
            "sum(increase(dlq_messages_total[1h])) or vector(0)",
            x=8,
            y=1,
            description="Jobs dead-lettered after exhausting retries. Anything >0 needs a look.",
            thresholds=[(None, "green"), (1, "red")],
        ),
        stat(
            "Failed jobs (1h)",
            'sum(increase(publication_jobs_total{status="FAILED"}[1h])) or vector(0)',
            x=12,
            y=1,
            thresholds=[(None, "green"), (1, "orange")],
        ),
        stat(
            "Gateway 5xx ratio",
            'sum(rate(gateway_proxy_requests_total{status=~"5.."}[5m])) '
            "/ clamp_min(sum(rate(gateway_proxy_requests_total[5m])), 1e-9)",
            x=16,
            y=1,
            unit="percentunit",
            thresholds=[(None, "green"), (0.01, "orange"), (0.05, "red")],
        ),
        stat(
            "Verify cache hit ratio",
            'sum(rate(gateway_verify_cache_total{result="hit"}[5m])) '
            "/ clamp_min(sum(rate(gateway_verify_cache_total[5m])), 1e-9)",
            x=20,
            y=1,
            unit="percentunit",
            description="Share of authenticated requests that did not need a call to auth.",
        ),
    ]

    # -------------------------------------------------------------- pipeline
    panels.append(row("Publication pipeline", 5))
    panels += [
        timeseries(
            "Jobs completed by outcome",
            [
                (
                    "sum by (status, result) (rate(publication_jobs_total[5m]))",
                    "{{status}} {{result}}",
                )
            ],
            x=0,
            y=6,
            unit="ops",
            stack=True,
            description="CREATED / DEDUPLICATED / POINTER_MOVED successes and FAILED jobs.",
        ),
        timeseries(
            "Job duration",
            [
                (
                    "histogram_quantile(0.5, sum by (le) "
                    "(rate(publication_job_duration_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) "
                    "(rate(publication_job_duration_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) "
                    "(rate(publication_job_duration_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            x=8,
            y=6,
            unit="s",
            description="Claim to terminal state inside the worker, including hash and copy.",
        ),
        timeseries(
            "Failures by error code",
            [
                (
                    'sum by (error_code) (increase(publication_jobs_total{status="FAILED"}[15m]))',
                    "{{error_code}}",
                )
            ],
            x=16,
            y=6,
            stack=True,
        ),
        timeseries(
            "Publication requests",
            [("sum by (result) (rate(publication_requests_total[5m]))", "{{result}}")],
            x=0,
            y=14,
            unit="reqps",
            description="accepted / idempotent_replay / rejected at the backend API.",
        ),
        timeseries(
            "Outbox backlog and DLQ",
            [
                ("max(outbox_pending)", "outbox pending"),
                ("sum(increase(dlq_messages_total[5m]))", "dead-lettered (5m)"),
            ],
            x=8,
            y=14,
        ),
        timeseries(
            "Reconciler re-emissions",
            [("sum by (reason) (increase(reconciler_requeued_total[5m]))", "{{reason}}")],
            x=16,
            y=14,
            description="stuck_pending: a request message was lost. expired_lease: a worker died.",
        ),
    ]

    # ---------------------------------------------------------------- gateway
    panels.append(row("Gateway", 22))
    panels += [
        timeseries(
            "Requests by status",
            [("sum by (status) (rate(gateway_proxy_requests_total[5m]))", "{{status}}")],
            x=0,
            y=23,
            unit="reqps",
            stack=True,
        ),
        timeseries(
            "Latency p95 by route",
            [
                (
                    "histogram_quantile(0.95, sum by (le, route) "
                    "(rate(gateway_proxy_duration_seconds_bucket[5m])))",
                    "{{route}}",
                )
            ],
            x=8,
            y=23,
            unit="s",
        ),
        timeseries(
            "Auth verification",
            [
                ("sum by (outcome) (rate(gateway_verify_calls_total[5m]))", "verify {{outcome}}"),
                ("sum by (result) (rate(gateway_verify_cache_total[5m]))", "cache {{result}}"),
            ],
            x=16,
            y=23,
            unit="reqps",
        ),
        timeseries(
            "Rate-limit rejections",
            [
                (
                    "sum by (rate_limit_class) (rate(gateway_rate_limited_total[5m]))",
                    "{{rate_limit_class}}",
                )
            ],
            x=0,
            y=31,
            unit="reqps",
        ),
        timeseries(
            "Private tile authorisations",
            [
                (
                    "sum by (decision, reason) (rate(edge_tile_authorisations_total[5m]))",
                    "{{decision}} {{reason}}",
                )
            ],
            x=8,
            y=31,
            unit="reqps",
            description="Signed-cookie checks at the edge. out_of_scope = cross-tenant attempt.",
        ),
        timeseries(
            "Backend request latency p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le, route) "
                    '(rate(http_request_duration_seconds_bucket{job="backend"}[5m])))',
                    "{{route}}",
                )
            ],
            x=16,
            y=31,
            unit="s",
        ),
    ]

    return {
        "uid": "pmtiles-platform",
        "title": "PMTiles platform",
        "tags": ["pmtiles"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": panels,
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT}")
