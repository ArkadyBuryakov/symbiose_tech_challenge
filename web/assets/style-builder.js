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

/** Sequential green ramp — forest data, dark = dense. Colour-blind safe. */
const RAMP = ["#f2f7f2", "#cde5d3", "#9ccfaf", "#65b58b", "#33986a", "#0f7749", "#08552f"];

const SOURCE_ID = "pmtiles-source";

/** Pull the H3 resolution out of a layer name like `h3_r11`. */
function resolutionOf(layerName) {
    const match = /(?:^|_)r(\d+)$/.exec(layerName);
    return match ? match[1] : null;
}

/**
 * The [min, max] this layer should be coloured against.
 * Prefers the per-resolution range, then the per-layer range, then the global one.
 */
export function rangeFor(spec, layerName) {
    const field = spec?.style?.color_field;
    const fallback = [spec?.style?.min ?? 0, spec?.style?.max ?? 1];
    if (!field) return fallback;

    const byLayer = spec?.field_ranges_by_layer?.[layerName]?.[field];
    if (isRange(byLayer)) return [byLayer.min, byLayer.max];

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

/**
 * Turn a spec (plus, optionally, the archive's own vector_layers metadata) into
 * MapLibre layer definitions.
 */
export function buildLayers(spec, { vectorLayers = [], headerMaxZoom = 22 } = {}) {
    const field = spec?.style?.color_field;
    const declared = Array.isArray(spec?.layers) ? spec.layers : [];

    // A spec without a `layers` array still renders: fall back to whatever the
    // archive declares in its own metadata.
    const entries = declared.length
        ? declared
        : vectorLayers.map((vl) => ({
              layer: vl.id,
              minzoom: vl.minzoom ?? 0,
              maxzoom: vl.maxzoom ?? headerMaxZoom,
              geometry: null,
          }));

    return entries.flatMap((entry) => layersFor(entry, spec, field, vectorLayers));
}

function layersFor(entry, spec, field, vectorLayers) {
    const name = entry.layer;
    const geometry = entry.geometry ?? guessGeometry(name, vectorLayers);
    const color = field ? colorExpression(field, rangeFor(spec, name)) : RAMP[3];

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
                    "circle-stroke-color": "#ffffff",
                    "circle-stroke-width": 0.6,
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

function guessGeometry(name, vectorLayers) {
    const declared = vectorLayers.find((vl) => vl.id === name);
    if (declared?.geometry) return declared.geometry.toLowerCase();
    // `centroids` in the reference dataset is the point layer.
    return /centroid|point|tree/i.test(name) ? "point" : "polygon";
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

export { RAMP, SOURCE_ID };
