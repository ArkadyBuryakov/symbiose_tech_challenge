/**
 * Render a dataset's current version.
 *
 * The page knows nothing about the data: it asks the catalogue for the current
 * version, points the pmtiles protocol at the returned edge URL, and builds the
 * layers from the spec that was stored with that version.
 *
 * For a private dataset it first exchanges its session for short-lived signed
 * tile cookies, and refreshes them before they expire so a long map session
 * does not start 403-ing mid-pan.
 */

import { api, formatBytes } from "./api.js";
import {
    SOURCE_ID,
    RAMP,
    buildLayers,
    columnsFor,
    interactiveLayerIds,
    rangeFor,
} from "./style-builder.js";

const statusEl = document.getElementById("status");
const panelEl = document.getElementById("panel");
const panelTitle = document.getElementById("panel-title");
const panelBody = document.getElementById("panel-body");

// Basemap: the standard OpenStreetMap raster tiles.
//
// The OSM tile usage policy (https://operations.osmfoundation.org/policies/tiles/)
// requires visible attribution and forbids heavy use; that is fine for an
// interactive demo, but a production deployment should point this at its own
// or a commercial tile service. Tiles exist up to z19; MapLibre overzooms them
// for the data's deeper levels (the dataset goes to z22).
const BASE_STYLE = {
    version: 8,
    sources: {
        osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            maxzoom: 19,
            attribution:
                '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        },
    },
    layers: [
        { id: "background", type: "background", paint: { "background-color": "#e8e6e1" } },
        {
            id: "osm",
            type: "raster",
            source: "osm",
            // Slightly muted so the data, not the basemap, carries the colour.
            paint: { "raster-saturation": -0.35, "raster-opacity": 0.95 },
        },
    ],
};

function fail(message, detail) {
    statusEl.innerHTML = "";
    statusEl.classList.add("error");
    statusEl.textContent = message;
    if (detail) {
        const small = document.createElement("div");
        small.className = "muted mono";
        small.style.marginTop = "6px";
        small.textContent = detail;
        statusEl.appendChild(small);
    }
}

async function main() {
    const datasetId = new URLSearchParams(location.search).get("dataset");
    if (!datasetId) return fail("No dataset requested.", "Add ?dataset=<id> to the URL.");

    let dataset;
    let current;
    try {
        [dataset, current] = await Promise.all([
            api.getDataset(datasetId),
            api.getCurrent(datasetId),
        ]);
    } catch (error) {
        return fail("Could not load the dataset.", `${error.message} (${error.code ?? ""})`);
    }

    document.getElementById("dataset-name").textContent = dataset.name;
    document.title = `${dataset.name} · PMTiles platform`;

    // Private tiles are authorised by a signed cookie, not by the URL, so it
    // must be in place before MapLibre issues its first range request.
    if (current.visibility === "private") {
        try {
            await startTileSession();
        } catch (error) {
            return fail("Not permitted to view this private dataset.", error.message);
        }
    }

    // The map is drawn from the spec published with this version.
    const layers = buildLayers(current.spec);
    if (layers.length === 0) {
        return fail("This version has no layer spec to draw.");
    }

    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);

    const header = current.pmtiles_header ?? {};
    const maxZoom = header.max_zoom ?? 22;
    const map = new maplibregl.Map({
        container: "map",
        style: BASE_STYLE,
        // The header's own bounds are authoritative — no guessing, no extra request.
        bounds: header.bounds,
        fitBoundsOptions: { padding: 40 },
        // Capped at the archive's own max zoom: the spec's zoom windows end
        // there, so zooming further would show an empty map.
        maxZoom,
        attributionControl: { compact: false },
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120 }), "bottom-right");

    map.on("load", () => {
        map.addSource(SOURCE_ID, {
            type: "vector",
            url: `pmtiles://${current.url}`,
            ...(header.bounds ? { bounds: header.bounds } : {}),
            minzoom: header.min_zoom ?? 0,
            maxzoom: maxZoom,
        });
        for (const layer of layers) map.addLayer(layer);

        statusEl.hidden = true;
        renderPanel(dataset, current, layers);
        wirePopups(map, current.spec, interactiveLayerIds(layers));
    });

    map.on("error", (event) => {
        // A tile 403 here almost always means the signed cookie expired.
        console.error("[map]", event.error);
    });
}

/** Fetch signed tile cookies now, and keep refreshing them before they expire. */
async function startTileSession() {
    const session = await api.createTileSession();
    const refreshMs = Math.max((session.expires_in ?? 600) * 1000 * 0.6, 30_000);
    setInterval(() => {
        api.createTileSession().catch((error) =>
            console.error("[tiles] cookie refresh failed", error),
        );
    }, refreshMs);
}

function renderPanel(dataset, current, layers) {
    panelEl.hidden = false;
    panelTitle.textContent = dataset.name;

    const field = current.spec?.style?.color_field;
    const header = current.pmtiles_header ?? {};
    const rows = [
        ["Version", `v${current.seq}`],
        ["Visibility", dataset.visibility],
        ["Size", formatBytes(current.size_bytes)],
        ["Zooms", `${header.min_zoom ?? "?"}–${header.max_zoom ?? "?"}`],
        ["Layers", interactiveLayerIds(layers).length],
        ["Content", current.sha256.slice(0, 12) + "…"],
    ];

    panelBody.innerHTML = "";
    const dl = document.createElement("dl");
    for (const [key, value] of rows) {
        const dt = document.createElement("dt");
        dt.textContent = key;
        const dd = document.createElement("dd");
        dd.textContent = value;
        dl.append(dt, dd);
    }
    panelBody.appendChild(dl);

    if (field) panelBody.appendChild(buildLegend(current.spec, field));
}

/**
 * One legend per drawn layer: because each H3 resolution is coloured against
 * its own range, a single shared scale would be misleading.
 */
function buildLegend(spec, field) {
    const wrapper = document.createElement("div");
    wrapper.className = "legend";

    const label = document.createElement("div");
    label.className = "muted";
    label.style.marginTop = "10px";
    label.textContent = labelFor(spec, field);
    wrapper.appendChild(label);

    const gradient = `linear-gradient(to right, ${RAMP.join(", ")})`;
    for (const entry of spec?.layers ?? []) {
        if (entry.geometry === "point" && (spec.layers ?? []).length > 1) continue;
        const [min, max] = rangeFor(spec, entry.layer);

        const name = document.createElement("div");
        name.className = "muted mono";
        name.style.marginTop = "8px";
        name.textContent = `${entry.layer} · z${entry.minzoom}–${entry.maxzoom}`;

        const bar = document.createElement("div");
        bar.className = "legend-bar";
        bar.style.background = gradient;

        const scale = document.createElement("div");
        scale.className = "legend-scale";
        scale.innerHTML = `<span>${fmtNumber(min)}</span><span>${fmtNumber(max)}</span>`;

        wrapper.append(name, bar, scale);
    }
    return wrapper;
}

function labelFor(spec, field) {
    const column = (spec?.style?.columns ?? []).find((c) => c.field === field);
    return column?.label ?? field;
}

function fmtNumber(value) {
    if (typeof value !== "number") return String(value);
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
}

/** Click a feature to see its attributes, labelled by the spec's `columns`. */
function wirePopups(map, spec, layerIds) {
    map.on("click", (event) => {
        const features = map.queryRenderedFeatures(event.point, { layers: layerIds });
        if (features.length === 0) return;

        const feature = features[0];
        const sourceLayer = feature.layer["source-layer"];
        const columns = columnsFor(spec, sourceLayer);

        const grid = document.createElement("dl");
        grid.className = "popup-grid";
        const entries = columns
            ? columns
                  .filter((c) => feature.properties[c.field] !== undefined)
                  .map((c) => [c.label ?? c.field, feature.properties[c.field]])
            : Object.entries(feature.properties);

        for (const [key, value] of entries) {
            const dt = document.createElement("dt");
            dt.textContent = key;
            const dd = document.createElement("dd");
            dd.textContent = typeof value === "number" ? fmtNumber(value) : String(value);
            grid.append(dt, dd);
        }

        const content = document.createElement("div");
        content.className = "popup-body";
        const title = document.createElement("div");
        title.className = "popup-title";
        title.textContent = sourceLayer;
        content.append(title, grid);

        new maplibregl.Popup({ maxWidth: "320px", closeButton: true })
            .setLngLat(event.lngLat)
            .setDOMContent(content)
            .addTo(map);
    });

    for (const id of layerIds) {
        map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
        map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
    }
}

main().catch((error) => fail("Unexpected error.", error.message));
