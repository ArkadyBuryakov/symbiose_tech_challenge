"""Copy-part planning: wrong byte ranges corrupt a published archive silently."""

from __future__ import annotations

from itertools import pairwise

import pytest

from pmp_worker.multipart import (
    MAX_PARTS,
    MIN_PART_BYTES,
    CopyPart,
    plan_copy_parts,
)

MIB = 1024**2
GIB = 1024**3


def assert_covers_exactly(parts: list[CopyPart], size: int) -> None:
    """Every byte covered once, in order, with no gaps or overlaps."""
    assert parts[0].first_byte == 0
    assert parts[-1].last_byte == size - 1
    assert [p.part_number for p in parts] == list(range(1, len(parts) + 1))
    for previous, current in pairwise(parts):
        assert current.first_byte == previous.last_byte + 1
    assert sum(p.size for p in parts) == size


def test_exact_multiple_of_part_size() -> None:
    parts = plan_copy_parts(4 * 100 * MIB, part_size=100 * MIB)

    assert len(parts) == 4
    assert_covers_exactly(parts, 4 * 100 * MIB)
    assert all(p.size == 100 * MIB for p in parts)


def test_ragged_final_part() -> None:
    size = 3 * 100 * MIB + 7 * MIB
    parts = plan_copy_parts(size, part_size=100 * MIB)

    assert len(parts) == 4
    assert_covers_exactly(parts, size)
    assert parts[-1].size == 7 * MIB


def test_tiny_remainder_is_its_own_last_part() -> None:
    """S3 allows the *last* part to be below 5 MiB."""
    size = 100 * MIB + 1
    parts = plan_copy_parts(size, part_size=100 * MIB)

    assert len(parts) == 2
    assert parts[-1].size == 1
    assert_covers_exactly(parts, size)


def test_every_part_except_the_last_meets_the_minimum() -> None:
    size = 2 * GIB + 3
    parts = plan_copy_parts(size, part_size=MIN_PART_BYTES)

    assert_covers_exactly(parts, size)
    assert all(p.size >= MIN_PART_BYTES for p in parts[:-1])


def test_part_size_grows_to_respect_the_10000_part_limit() -> None:
    """A 200 GiB object with an 8 MiB part size would need ~25 600 parts."""
    size = 200 * GIB
    parts = plan_copy_parts(size, part_size=8 * MIB)

    assert len(parts) <= MAX_PARTS
    assert_covers_exactly(parts, size)


def test_copy_source_range_is_the_inclusive_form_s3_expects() -> None:
    parts = plan_copy_parts(10 * MIB, part_size=6 * MIB)

    assert parts[0].copy_source_range == f"bytes=0-{6 * MIB - 1}"
    assert parts[1].copy_source_range == f"bytes={6 * MIB}-{10 * MIB - 1}"


def test_single_byte_object() -> None:
    parts = plan_copy_parts(1, part_size=MIN_PART_BYTES)

    assert len(parts) == 1
    assert parts[0].size == 1


@pytest.mark.parametrize("size", [0, -1])
def test_non_positive_sizes_are_rejected(size: int) -> None:
    with pytest.raises(ValueError, match="cannot plan a copy"):
        plan_copy_parts(size, part_size=MIN_PART_BYTES)


def test_part_size_below_the_s3_minimum_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least"):
        plan_copy_parts(100 * MIB, part_size=MIN_PART_BYTES - 1)
