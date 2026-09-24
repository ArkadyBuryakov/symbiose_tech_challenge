"""Export JSON Schema for every Kafka event into ``docs/events/``.

The event models are the contract between the backend and the worker; this
makes that contract readable by someone who does not read Python (and
diffable in review when a schema changes).

Run with: ``uv run python scripts/export-event-schemas.py``
"""

from __future__ import annotations

import json
from pathlib import Path

from pmp_common.events import (
    SCHEMA_VERSION,
    EventEnvelope,
    PublicationFailed,
    PublicationRequested,
    PublicationSucceeded,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "events"

MODELS: list[tuple[str, type[EventEnvelope]]] = [
    ("publication.requested", PublicationRequested),
    ("publication.succeeded", PublicationSucceeded),
    ("publication.failed", PublicationFailed),
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in MODELS:
        schema = model.model_json_schema(mode="serialization")
        schema["$id"] = f"https://pmtiles.platform/events/{name}/v{SCHEMA_VERSION}.json"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        path = OUT_DIR / f"{name}.v{SCHEMA_VERSION}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path.name)
    print(f"wrote {len(written)} schemas to {OUT_DIR}:")
    for name in written:
        print(f"  {name}")


if __name__ == "__main__":
    main()
