/**
 * Build MapLibre layers from a dataset's stored spec.
 *
 * The spec is what the producing pipeline published alongside the tiles, so the
 * map is data-driven: adding an H3 resolution or changing the colour field is a
 * change to the spec, not to this page.
 *
 * Spec shape (see sample-data/input_forest_crowns_pmtiles.spec.json):
 *
 *   style.color_field                  property to colour by
 *   style.min / style.max              global fallback range
 *   style.columns                      popup labels
 *   layers[]                           { layer, minzoom, maxzoom, geometry, columns? }
 *   field_ranges_by_resolution["10"]   per-resolution { field: {min, max} }
 *
 * Per-resolution ranges matter: the same `count` means something different at
 * r10 and r12 (bigger cells hold more), so colouring every layer against one
 * global range would wash the fine layers out entirely.
 */

/**
 * Sequential yellow-orange-red ramp (ColorBrewer YlOrRd), dark = dense.
 * Colour-blind safe, and chosen to contrast with the OSM basemap: the data sits
 * on forest, which OSM draws green, so a green ramp would vanish into it.
 */
const RAMP = ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c", "#f03b20", "#bd0026"];

/** Solid colour for layers that do not carry the colour field. */
const POINT_COLOR = "#1d4e89";

const SOURCE_ID = "pmtiles-source";

/** Pull the H3 resolution out of a layer name like `h3_r11`. */
function resolutionOf(layerName) {
    const match = /(?:^|_)r(\d+)$/.exec(layerName);
    return match ? match[1] : null;
}

/**
 * The [min, max] this layer should be coloured against: the per-resolution
 * range, else the global one.
 */
export function rangeFor(spec, layerName) {
    const field = spec?.style?.color_field;
    const fallback = [spec?.style?.min ?? 0, spec?.style?.max ?? 1];
    if (!field) return fallback;

    const resolution = resolutionOf(layerName);
    const byResolution = resolution && spec?.field_ranges_by_resolution?.[resolution]?.[field];
    if (isRange(byResolution)) return [byResolution.min, byResolution.max];

    return fallback;
}

function isRange(value) {
    return value && typeof value.min === "number" && typeof value.max === "number";
}

/** A MapLibre `interpolate` expression mapping [min, max] onto the ramp. */
export function colorExpression(field, [min, max]) {
    // A degenerate range would make `interpolate` throw; fall back to a flat
    // mid-ramp colour rather than failing to render the layer at all.
    if (!(max > min)) return RAMP[Math.floor(RAMP.length / 2)];

    const stops = RAMP.flatMap((color, index) => [
        min + ((max - min) * index) / (RAMP.length - 1),
        color,
    ]);
    return [
        "interpolate",
        ["linear"],
        // `to-number` guards against the property being absent or a string.
        ["to-number", ["get", field], min],
        ...stops,
    ];
}

/** Turn a spec's `layers` into MapLibre layer definitions (none without a spec). */
export function buildLayers(spec) {
    const field = spec?.style?.color_field;
    const entries = Array.isArray(spec?.layers) ? spec.layers : [];
    return entries.flatMap((entry) => layersFor(entry, spec, field));
}

function layersFor(entry, spec, field) {
    const name = entry.layer;
    const geometry = entry.geometry ?? "polygon";
    const color = hasField(entry, field)
        ? colorExpression(field, rangeFor(spec, name))
        : POINT_COLOR;

    const common = {
        source: SOURCE_ID,
        "source-layer": name,
        ...(entry.minzoom != null ? { minzoom: entry.minzoom } : {}),
        // MapLibre's maxzoom is exclusive, the spec's is inclusive.
        ...(entry.maxzoom != null ? { maxzoom: Math.min(entry.maxzoom + 1, 24) } : {}),
    };

    if (geometry === "point") {
        return [
            {
                ...common,
                id: `${name}-circle`,
                type: "circle",
                paint: {
                    "circle-color": color,
                    "circle-opacity": 0.9,
                    // Light outline so points read against the basemap.
                    "circle-stroke-color": "#ffffff",
                    "circle-stroke-width": 1,
                    // Points are only shown at high zoom; grow them with zoom so
                    // they stay clickable without swamping the view.
                    "circle-radius": [
                        "interpolate", ["linear"], ["zoom"],
                        14, 2,
                        18, 4,
                        22, 9,
                    ],
                },
            },
        ];
    }

    return [
        {
            ...common,
            id: `${name}-fill`,
            type: "fill",
            paint: { "fill-color": color, "fill-opacity": 0.78 },
        },
        {
            ...common,
            id: `${name}-line`,
            type: "line",
            paint: {
                "line-color": "#ffffff",
                "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.1, 16, 0.5],
                "line-opacity": 0.5,
            },
        },
    ];
}

/**
 * Whether a layer carries the colour field. A layer that declares its own
 * `columns` without it (the reference spec's `centroids` has no `count`) is
 * drawn in a solid colour instead: interpolating a missing property would
 * paint every feature the palest ramp colour, invisible against the base.
 */
function hasField(entry, field) {
    if (!field) return false;
    if (!Array.isArray(entry.columns)) return true;
    return entry.columns.some((c) => c.field === field);
}

/** Popup labels for a layer: its own `columns` if it has them, else the global set. */
export function columnsFor(spec, layerName) {
    const entry = (spec?.layers ?? []).find((l) => l.layer === layerName);
    return entry?.columns ?? spec?.style?.columns ?? null;
}

/** Ids of every interactive (clickable) layer produced by {@link buildLayers}. */
export function interactiveLayerIds(layers) {
    return layers.filter((l) => l.type !== "line").map((l) => l.id);
}

export { POINT_COLOR, RAMP, SOURCE_ID };
