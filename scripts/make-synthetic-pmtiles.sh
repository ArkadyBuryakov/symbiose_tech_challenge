#!/usr/bin/env bash
# Generate a small synthetic PMTiles archive so the platform can be exercised
# end to end without the real sample file.
#
#   ./scripts/make-synthetic-pmtiles.sh [output.pmtiles] [--variant N]
#
# The output mimics the real dataset's shape: hexagon-ish polygons across three
# "H3 resolution" layers plus a `centroids` point layer, each carrying a `count`
# field, so the map page's spec-driven styling has something to render.
#
# `--variant N` changes the generated geometry deterministically, which is how
# the tests get two archives with *different* content hashes.
#
# tippecanoe is not published to any readable public registry, so the first run
# builds it from a pinned source tag (~2 minutes) and caches the image.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIPPECANOE_VERSION="${TIPPECANOE_VERSION:-2.79.0}"
IMAGE="pmp/tippecanoe:${TIPPECANOE_VERSION}"

OUTPUT="${1:-${REPO_ROOT}/sample-data/synthetic.pmtiles}"
VARIANT=0
if [[ "${2:-}" == "--variant" ]]; then VARIANT="${3:-1}"; fi

mkdir -p "$(dirname "$OUTPUT")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE (first run only, a couple of minutes)..."
    docker build -q \
        --build-arg "TIPPECANOE_VERSION=${TIPPECANOE_VERSION}" \
        -f "${REPO_ROOT}/ops/tippecanoe/Dockerfile" \
        -t "$IMAGE" "${REPO_ROOT}/ops/tippecanoe" >/dev/null
fi

echo "generating synthetic GeoJSON (variant ${VARIANT})..."
python3 - "$WORK" "$VARIANT" <<'PY'
import json, math, sys, pathlib

work = pathlib.Path(sys.argv[1])
variant = int(sys.argv[2])

# Centred on the real sample's bounding box so both datasets frame the same place.
LON0, LAT0 = 1.5758939, 47.603009

def hexagon(lon, lat, radius):
    """A hexagon in degrees — close enough to an H3 cell for rendering."""
    ring = []
    for i in range(6):
        angle = math.radians(60 * i + 30)
        ring.append([
            round(lon + radius * math.cos(angle) / math.cos(math.radians(lat)), 7),
            round(lat + radius * math.sin(angle), 7),
        ])
    ring.append(ring[0])
    return ring

def grid(resolution, cells, radius, max_count):
    features = []
    for i in range(cells):
        for j in range(cells):
            lon = LON0 + (i - cells / 2) * radius * 1.8
            lat = LAT0 + (j - cells / 2) * radius * 1.6
            # `variant` perturbs the values so a second run yields different bytes.
            count = 1 + (i * 7 + j * 13 + variant * 29) % max_count
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [hexagon(lon, lat, radius)]},
                "properties": {
                    "h3_index": f"{resolution}-{i}-{j}",
                    "h3_resolution": resolution,
                    "count": count,
                },
            })
    return features

def centroids(cells, radius, max_count):
    features = []
    for i in range(cells):
        for j in range(cells):
            lon = LON0 + (i - cells / 2) * radius * 1.8
            lat = LAT0 + (j - cells / 2) * radius * 1.6
            count = 1 + (i * 5 + j * 11 + variant * 17) % max_count
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]},
                "properties": {
                    "id": i * cells + j,
                    "h": round(6 + (count % 25) * 0.9, 2),
                    "count": count,
                },
            })
    return features

layers = {
    "h3_r10": grid(10, 14, 0.0012, 72),
    "h3_r11": grid(11, 20, 0.0008, 148),
    "h3_r12": grid(12, 28, 0.0005, 32),
    "centroids": centroids(34, 0.0004, 40),
}
for name, features in layers.items():
    path = work / f"{name}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    print(f"  {name}: {len(features)} features")
PY

echo "running tippecanoe..."
# Run as root inside the container (tippecanoe needs to create its scratch
# files), then hand the result back to the calling user so the cleanup trap and
# the copy below work on every Docker configuration.
docker run --rm -v "$WORK:/data" \
    -e "OWNER=$(id -u):$(id -g)" \
    --entrypoint /bin/sh "$IMAGE" -c '
        tippecanoe \
            --output=/data/out.pmtiles \
            --force \
            --no-feature-limit --no-tile-size-limit \
            --named-layer=h3_r10:/data/h3_r10.geojson \
            --named-layer=h3_r11:/data/h3_r11.geojson \
            --named-layer=h3_r12:/data/h3_r12.geojson \
            --named-layer=centroids:/data/centroids.geojson \
            --minimum-zoom=0 --maximum-zoom=16 \
            --quiet
        chown "$OWNER" /data/out.pmtiles
    '

cp "$WORK/out.pmtiles" "$OUTPUT"
echo "wrote $OUTPUT ($(du -h "$OUTPUT" | cut -f1), sha256 $(sha256sum "$OUTPUT" | cut -c1-16)...)"
