"""Planning a server-side copy.

S3 ``CopyObject`` is limited to 5 GiB. Above that the copy must be split into
``UploadPartCopy`` calls with explicit byte ranges. Getting those ranges wrong
corrupts the published archive in a way that is invisible until someone opens
the map, so the arithmetic lives here on its own and is unit-tested.

Constraints this encodes (S3 API limits):

* a single ``CopyObject`` handles objects up to 5 GiB;
* every part except the last must be at least 5 MiB (the last may be smaller);
* a multipart upload has at most 10 000 parts.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MAX_PARTS",
    "MAX_SINGLE_COPY_BYTES",
    "MIN_PART_BYTES",
    "CopyPart",
    "plan_copy_parts",
]

MAX_SINGLE_COPY_BYTES = 5 * 1024**3  # 5 GiB
MIN_PART_BYTES = 5 * 1024**2  # 5 MiB
MAX_PARTS = 10_000


@dataclass(frozen=True, slots=True)
class CopyPart:
    """One ``UploadPartCopy`` call."""

    part_number: int
    """1-based, as the S3 API requires."""

    first_byte: int
    last_byte: int
    """Inclusive, matching the ``bytes=first-last`` range header S3 expects."""

    @property
    def size(self) -> int:
        return self.last_byte - self.first_byte + 1

    @property
    def copy_source_range(self) -> str:
        return f"bytes={self.first_byte}-{self.last_byte}"


def plan_copy_parts(size_bytes: int, *, part_size: int) -> list[CopyPart]:
    """Split ``size_bytes`` into copy parts of at most ``part_size``.

    ``part_size`` is grown automatically if the object would otherwise need more
    than :data:`MAX_PARTS` parts, so a very large object still copies with a
    legal plan instead of failing late.
    """
    if size_bytes <= 0:
        raise ValueError(f"cannot plan a copy of {size_bytes} bytes")
    if part_size < MIN_PART_BYTES:
        raise ValueError(f"part_size must be at least {MIN_PART_BYTES} bytes, got {part_size}")

    # Round up so the part count fits, then round the size up to a whole MiB to
    # keep the ranges tidy.
    if -(-size_bytes // part_size) > MAX_PARTS:
        required = -(-size_bytes // MAX_PARTS)
        part_size = -(-required // (1024**2)) * 1024**2

    return [
        CopyPart(part_number=n, first_byte=first, last_byte=min(first + part_size, size_bytes) - 1)
        for n, first in enumerate(range(0, size_bytes, part_size), start=1)
    ]
