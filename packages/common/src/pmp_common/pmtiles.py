"""Minimal PMTiles v3 header reader.

The worker only needs to answer two questions about a staged object:

1. Is this actually a PMTiles v3 archive? (cheap structural validation, so a
   corrupt or mistyped upload fails fast and permanently instead of being
   published and breaking the map.)
2. What are its bounds, center and zoom range? (stored on the version row so
   the frontend can fit the map without downloading the archive first.)

Both are answered from the fixed-size 127-byte header, so the worker can do a
single ranged GET instead of downloading the file. The full spec lives at
https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md — we
deliberately do not depend on a PMTiles library for this.
"""

from __future__ import annotations

import struct
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "HEADER_SIZE",
    "PMTILES_MAGIC",
    "InvalidPMTiles",
    "PMTilesHeader",
    "UnsupportedPMTilesVersion",
    "parse_header",
]

PMTILES_MAGIC: Final = b"PMTiles"
HEADER_SIZE: Final = 127
SUPPORTED_VERSION: Final = 3

_COMPRESSION: Final = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
_TILE_TYPE: Final = {0: "unknown", 1: "mvt", 2: "png", 3: "jpeg", 4: "webp", 5: "avif"}

# <  little endian
# 7s magic | B version
# 8x Q     offsets/lengths (root dir, metadata, leaves, tile data)
# 3x Q     addressed tiles / tile entries / tile contents
# 6x B     clustered, internal compression, tile compression, tile type, minzoom, maxzoom
# 4x i     bbox as lon/lat * 1e7
# 1x B     center zoom
# 2x i     center lon/lat * 1e7
_STRUCT: Final = struct.Struct("<7sB QQQQQQQQ QQQ BBBBBB iiii B ii")


class InvalidPMTiles(ValueError):
    """The bytes are not a PMTiles archive (or are truncated)."""


class UnsupportedPMTilesVersion(InvalidPMTiles):
    """A PMTiles archive of a version this platform does not serve."""


class PMTilesHeader(BaseModel):
    """The subset of the PMTiles header the platform stores and serves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    tile_type: str
    tile_compression: str
    internal_compression: str
    clustered: bool
    min_zoom: int = Field(ge=0, le=30)
    max_zoom: int = Field(ge=0, le=30)
    bounds: tuple[float, float, float, float] = Field(
        description="[min_lon, min_lat, max_lon, max_lat] in WGS84 degrees."
    )
    center: tuple[float, float] = Field(description="[lon, lat] in WGS84 degrees.")
    center_zoom: int = Field(ge=0, le=30)
    addressed_tiles: int
    tile_entries: int
    tile_contents: int
    metadata_offset: int
    metadata_length: int


def parse_header(data: bytes) -> PMTilesHeader:
    """Parse the first :data:`HEADER_SIZE` bytes of a PMTiles archive.

    Raises :class:`InvalidPMTiles` or :class:`UnsupportedPMTilesVersion`; both
    are permanent failures for a publication job.
    """
    if len(data) < HEADER_SIZE:
        raise InvalidPMTiles(f"need {HEADER_SIZE} header bytes, got {len(data)}")
    fields = _STRUCT.unpack(data[:HEADER_SIZE])
    magic, version = fields[0], fields[1]
    if magic != PMTILES_MAGIC:
        raise InvalidPMTiles(f"bad magic {magic!r}, expected {PMTILES_MAGIC!r}")
    if version != SUPPORTED_VERSION:
        raise UnsupportedPMTilesVersion(
            f"PMTiles version {version} is not supported (expected {SUPPORTED_VERSION})"
        )

    (
        _root_off,
        _root_len,
        metadata_offset,
        metadata_length,
        _leaf_off,
        _leaf_len,
        _tile_off,
        _tile_len,
    ) = fields[2:10]
    addressed_tiles, tile_entries, tile_contents = fields[10:13]
    clustered, internal_compression, tile_compression, tile_type, min_zoom, max_zoom = fields[13:19]
    min_lon, min_lat, max_lon, max_lat = fields[19:23]
    center_zoom = fields[23]
    center_lon, center_lat = fields[24:26]

    if min_zoom > max_zoom:
        raise InvalidPMTiles(f"min_zoom {min_zoom} is greater than max_zoom {max_zoom}")

    bounds = (min_lon / 1e7, min_lat / 1e7, max_lon / 1e7, max_lat / 1e7)
    if not (-180.0 <= bounds[0] <= 180.0 and -90.0 <= bounds[1] <= 90.0):
        raise InvalidPMTiles(f"bounds out of range: {bounds}")
    if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
        raise InvalidPMTiles(f"degenerate bounds: {bounds}")

    return PMTilesHeader(
        version=version,
        tile_type=_TILE_TYPE.get(tile_type, "unknown"),
        tile_compression=_COMPRESSION.get(tile_compression, "unknown"),
        internal_compression=_COMPRESSION.get(internal_compression, "unknown"),
        clustered=bool(clustered),
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        bounds=bounds,
        center=(center_lon / 1e7, center_lat / 1e7),
        center_zoom=center_zoom,
        addressed_tiles=addressed_tiles,
        tile_entries=tile_entries,
        tile_contents=tile_contents,
        metadata_offset=metadata_offset,
        metadata_length=metadata_length,
    )
