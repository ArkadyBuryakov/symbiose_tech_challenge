/**
 * Tests for the spec-driven style builder.
 *
 * Run with: node --test web/tests/
 *
 * Node's built-in test runner is used deliberately — the frontend has no build
 * step and no package.json, and this is the only part of it with real logic.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
    POINT_COLOR,
    RAMP,
    buildLayers,
    colorExpression,
    columnsFor,
    interactiveLayerIds,
    mergeSpecs,
    rangeFor,
    specFromMetadata,
} from "../assets/style-builder.js";

const SPEC_PATH = fileURLToPath(
    new URL("../../sample-data/input_forest_crowns_pmtiles.spec.json", import.meta.url),
);
const realSpec = JSON.parse(readFileSync(SPEC_PATH, "utf8"));
// vector_layers + tilestats of the real archive (sample-data/input_h3_multires.pmtiles).
const realMetadata = JSON.parse(
    readFileSync(new URL("./fixtures/input_h3_multires.metadata.json", import.meta.url), "utf8"),
);
const archiveSpec = specFromMetadata(realMetadata);

// --------------------------------------------------------------------- ranges
test("per-resolution range beats the global range", () => {
    // The whole point: `count` at r12 tops out far lower than at r10, so using
    // the global max would render the r12 layer almost uniformly pale.
    assert.deepEqual(rangeFor(realSpec, "h3_r10"), [1, 728]);
    assert.deepEqual(rangeFor(realSpec, "h3_r11"), [1, 148]);
    assert.deepEqual(rangeFor(realSpec, "h3_r12"), [1, 32]);
});

test("a layer without a resolution uses the global range", () => {
    assert.deepEqual(rangeFor(realSpec, "centroids"), [realSpec.style.min, realSpec.style.max]);
});

test("an unknown resolution falls back rather than throwing", () => {
    assert.deepEqual(rangeFor(realSpec, "h3_r99"), [realSpec.style.min, realSpec.style.max]);
});

test("a spec with no style at all still yields a usable range", () => {
    assert.deepEqual(rangeFor({}, "anything"), [0, 1]);
});

// ---------------------------------------------------------------- expressions
test("colour expression interpolates across the whole ramp", () => {
    const expression = colorExpression("count", [0, 100]);

    assert.equal(expression[0], "interpolate");
    assert.deepEqual(expression[1], ["linear"]);
    assert.deepEqual(expression[2], ["to-number", ["get", "count"], 0]);

    const stops = expression.slice(3);
    assert.equal(stops.length, RAMP.length * 2);
    assert.equal(stops[0], 0);
    assert.equal(stops.at(-2), 100);
    assert.equal(stops.at(-1), RAMP.at(-1));
});

test("stops increase monotonically, as MapLibre requires", () => {
    const stops = colorExpression("count", [1, 148]).slice(3);
    const inputs = stops.filter((_, index) => index % 2 === 0);

    for (let i = 1; i < inputs.length; i += 1) {
        assert.ok(inputs[i] > inputs[i - 1], `stop ${i} is not greater than ${i - 1}`);
    }
});

test("a degenerate range produces a flat colour instead of an invalid expression", () => {
    // A single-valued field (min === max) would make `interpolate` throw and
    // take the whole layer down with it.
    assert.equal(typeof colorExpression("count", [5, 5]), "string");
    assert.equal(typeof colorExpression("count", [10, 2]), "string");
});

// --------------------------------------------------------------------- layers
test("the real spec produces fill layers per resolution and a circle layer", () => {
    const layers = buildLayers(realSpec);
    const ids = layers.map((l) => l.id);

    assert.ok(ids.includes("h3_r10-fill"));
    assert.ok(ids.includes("h3_r11-fill"));
    assert.ok(ids.includes("h3_r12-fill"));
    assert.ok(ids.includes("centroids-circle"));
    assert.ok(!ids.includes("centroids-fill"));
});

test("zoom windows come from the spec, with maxzoom made exclusive", () => {
    const layers = buildLayers(realSpec);
    const r10 = layers.find((l) => l.id === "h3_r10-fill");
    const r11 = layers.find((l) => l.id === "h3_r11-fill");

    assert.equal(r10.minzoom, 0);
    // The spec's maxzoom is inclusive (15); MapLibre's is exclusive.
    assert.equal(r10.maxzoom, 16);
    assert.equal(r11.minzoom, 16);
    assert.equal(r11.maxzoom, 17);
});

test("the zoom windows tile the range without gaps", () => {
    const polygons = realSpec.layers.filter((l) => l.geometry === "polygon");
    for (let i = 1; i < polygons.length; i += 1) {
        assert.equal(
            polygons[i].minzoom,
            polygons[i - 1].maxzoom + 1,
            `gap between ${polygons[i - 1].layer} and ${polygons[i].layer}`,
        );
    }
});

test("every layer reads from the same vector source and its own source-layer", () => {
    for (const layer of buildLayers(realSpec)) {
        assert.equal(layer.source, "pmtiles-source");
        assert.ok(layer["source-layer"]);
        assert.ok(layer.id.startsWith(layer["source-layer"]));
    }
});

test("each polygon layer is coloured against its own range", () => {
    const layers = buildLayers(realSpec);
    const stopsOf = (id) =>
        layers.find((l) => l.id === id).paint["fill-color"].slice(3).filter((_, i) => i % 2 === 0);

    assert.notDeepEqual(stopsOf("h3_r10-fill"), stopsOf("h3_r12-fill"));
    assert.equal(stopsOf("h3_r12-fill").at(-1), 32);
    assert.equal(stopsOf("h3_r10-fill").at(-1), 728);
});

// ------------------------------------------------------------------- no spec
test("a missing spec or one without layers yields nothing to draw", () => {
    assert.deepEqual(buildLayers(null), []);
    assert.deepEqual(buildLayers({ style: { color_field: "count" } }), []);
});

// -------------------------------------------------------------------- popups
test("a layer's own columns override the global ones", () => {
    const centroids = columnsFor(realSpec, "centroids");
    const hexes = columnsFor(realSpec, "h3_r10");

    assert.ok(centroids.some((c) => c.field === "id" && c.label === "Tree id"));
    assert.ok(hexes.some((c) => c.field === "h3_index"));
    assert.notDeepEqual(centroids, hexes);
});

test("a layer with no columns of its own inherits the global set", () => {
    assert.deepEqual(columnsFor(realSpec, "h3_r11"), realSpec.style.columns);
});

test("outline layers are not clickable", () => {
    const layers = buildLayers(realSpec);
    const interactive = interactiveLayerIds(layers);

    assert.ok(interactive.includes("h3_r10-fill"));
    assert.ok(interactive.includes("centroids-circle"));
    assert.ok(!interactive.some((id) => id.endsWith("-outline")));
});

test("a layer whose own columns lack the colour field gets a solid colour", () => {
    // The reference spec's `centroids` declares columns without `count`;
    // interpolating it would paint every tree the palest ramp colour.
    const circle = buildLayers(realSpec).find((l) => l.id === "centroids-circle");

    assert.equal(circle.paint["circle-color"], POINT_COLOR);
});

test("layers without their own columns are still coloured by the field", () => {
    const fill = buildLayers(realSpec).find((l) => l.id === "h3_r10-fill");

    assert.equal(fill.paint["fill-color"][0], "interpolate");
});

// ------------------------------------------------------------------- archive
test("the archive alone describes every layer, its zooms and geometry", () => {
    const byName = Object.fromEntries(archiveSpec.layers.map((l) => [l.layer, l]));

    assert.deepEqual(Object.keys(byName).sort(), ["centroids", "h3_r10", "h3_r11", "h3_r12"]);
    assert.equal(byName.h3_r10.geometry, "polygon");
    assert.equal(byName.centroids.geometry, "point");
    assert.equal(byName.h3_r10.minzoom, 4);
    assert.ok(byName.h3_r10.fields.includes("count"));
    assert.ok(!byName.centroids.fields.includes("count"));
    assert.deepEqual(archiveSpec.field_ranges_by_layer.h3_r12.count, { min: 1, max: 32 });
});

test("with no spec, the archive still draws: polygons first, points on top", () => {
    const ids = buildLayers(mergeSpecs(archiveSpec, null)).map((l) => l.id);

    assert.ok(ids.includes("h3_r10-fill"));
    assert.equal(ids.at(-1), "centroids-circle");
});

test("with no spec, popups list the archive's fields", () => {
    const columns = columnsFor(archiveSpec, "h3_r10");

    assert.ok(columns.some((c) => c.field === "h3_index" && c.label === undefined));
});

test("with no metadata, the spec alone still draws", () => {
    const layers = buildLayers(mergeSpecs(specFromMetadata(null), realSpec));

    assert.deepEqual(
        layers.map((l) => l.id),
        buildLayers(realSpec).map((l) => l.id),
    );
});

// --------------------------------------------------------------------- merge
test("the spec overrides the archive's zoom windows", () => {
    const merged = mergeSpecs(archiveSpec, realSpec);
    const r10 = buildLayers(merged).find((l) => l.id === "h3_r10-fill");

    // Archive says z4-15, the spec says z0-15: the spec wins.
    assert.equal(r10.minzoom, 0);
    assert.equal(r10.maxzoom, 16);
});

test("the spec's labels replace the archive's bare field names", () => {
    const merged = mergeSpecs(archiveSpec, realSpec);

    assert.deepEqual(columnsFor(merged, "h3_r11"), realSpec.style.columns);
    assert.ok(columnsFor(merged, "centroids").some((c) => c.label === "Tree id"));
});

test("the spec's per-resolution range beats the archive's per-layer one", () => {
    const merged = mergeSpecs(archiveSpec, {
        ...realSpec,
        field_ranges_by_resolution: { 12: { count: { min: 5, max: 9 } } },
    });

    assert.deepEqual(rangeFor(merged, "h3_r12"), [5, 9]);
});

test("the archive's per-layer range fills in where the spec has none", () => {
    const spec = { style: { color_field: "count", min: 0, max: 1000 } };
    const merged = mergeSpecs(archiveSpec, spec);

    assert.deepEqual(rangeFor(merged, "h3_r11"), [1, 148]);
    assert.deepEqual(rangeFor(spec, "h3_r11"), [0, 1000]);
});

test("the spec's per-layer ranges merge per field over the archive's", () => {
    const merged = mergeSpecs(archiveSpec, {
        field_ranges_by_layer: { h3_r10: { count: { min: 2, max: 3 } } },
    });

    assert.deepEqual(merged.field_ranges_by_layer.h3_r10.count, { min: 2, max: 3 });
    assert.ok(merged.field_ranges_by_layer.h3_r10.trees_ha, "other fields are kept");
});

test("layers only the archive knows about are still drawn", () => {
    const merged = mergeSpecs(archiveSpec, { layers: [{ layer: "h3_r10", minzoom: 0 }] });

    assert.deepEqual(merged.layers.map((l) => l.layer).sort(), [
        "centroids",
        "h3_r10",
        "h3_r11",
        "h3_r12",
    ]);
    assert.equal(merged.layers.find((l) => l.layer === "h3_r10").geometry, "polygon");
});

test("a layer without the colour field in the archive gets a solid colour", () => {
    const merged = mergeSpecs(archiveSpec, { style: { color_field: "count" } });
    const circle = buildLayers(merged).find((l) => l.id === "centroids-circle");

    assert.equal(circle.paint["circle-color"], POINT_COLOR);
});
