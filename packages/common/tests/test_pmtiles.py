"""PMTiles v3 header parsing — the worker's cheap structural validation."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from pmp_common.pmtiles import (
    HEADER_SIZE,
    InvalidPMTiles,
    parse_header,
)

SAMPLE = Path(__file__).resolve().parents[3] / "sample-data" / "input_h3_multires.pmtiles"


def make_header(
    *,
    magic: bytes = b"PMTiles",
    version: int = 3,
    min_zoom: int = 0,
    max_zoom: int = 14,
    bbox: tuple[int, int, int, int] = (-1_0000000, -1_0000000, 1_0000000, 1_0000000),
    tile_type: int = 1,
) -> bytes:
    return struct.pack(
        "<7sB QQQQQQQQ QQQ BBBBBB iiii B ii",
        magic,
        version,
        127,
        59,
        186,
        100,  # root dir, metadata
        300,
        100,
        400,
        5000,  # leaves, tile data
        10,
        10,
        10,  # addressed / entries / contents
        1,
        2,
        2,
        tile_type,
        min_zoom,
        max_zoom,
        *bbox,
        7,
        0,
        0,
    )


def test_parses_a_well_formed_header() -> None:
    header = parse_header(make_header())

    assert header.version == 3
    assert header.tile_type == "mvt"
    assert header.tile_compression == "gzip"
    assert header.clustered is True
    assert header.min_zoom == 0
    assert header.max_zoom == 14
    assert header.bounds == (-1.0, -1.0, 1.0, 1.0)


def test_ignores_trailing_bytes() -> None:
    """The worker fetches a ranged read; extra bytes must not confuse the parser."""
    assert parse_header(make_header() + b"\x00" * 500).version == 3


def test_rejects_a_truncated_header() -> None:
    with pytest.raises(InvalidPMTiles, match="header bytes"):
        parse_header(make_header()[: HEADER_SIZE - 1])


def test_rejects_a_non_pmtiles_file() -> None:
    with pytest.raises(InvalidPMTiles, match="bad magic"):
        parse_header(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)


def test_rejects_an_unsupported_version() -> None:
    with pytest.raises(InvalidPMTiles, match="version 2"):
        parse_header(make_header(version=2))


def test_rejects_zoom_above_30_as_invalid_pmtiles() -> None:
    """Must be InvalidPMTiles (permanent), not a pydantic ValidationError."""
    with pytest.raises(InvalidPMTiles, match="zoom above"):
        parse_header(make_header(max_zoom=31))


def test_rejects_inverted_zooms() -> None:
    with pytest.raises(InvalidPMTiles, match="min_zoom"):
        parse_header(make_header(min_zoom=10, max_zoom=3))


def test_rejects_out_of_range_bounds() -> None:
    with pytest.raises(InvalidPMTiles, match="out of range"):
        parse_header(make_header(bbox=(200_0000000, 0, 210_0000000, 10_0000000)))


def test_rejects_degenerate_bounds() -> None:
    with pytest.raises(InvalidPMTiles, match="degenerate"):
        parse_header(make_header(bbox=(10_0000000, 0, 1_0000000, 10_0000000)))


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample-data/*.pmtiles is not committed")
def test_parses_the_real_sample_archive() -> None:
    header = parse_header(SAMPLE.read_bytes()[:HEADER_SIZE])

    assert header.version == 3
    assert header.tile_type == "mvt"
    assert header.max_zoom == 22
    assert header.bounds[0] == pytest.approx(1.566067)
    assert header.bounds[3] == pytest.approx(47.606626)
