"""The dataset versioning rules, as a pure function.

This is the one piece of business logic in the system that must be exactly
right, so it is isolated from Postgres, S3 and Kafka and unit-tested on its
own. The worker calls it while holding a row lock on the dataset
(``SELECT ... FOR UPDATE``) and then applies the returned decision inside the
same transaction.

A version is identified by **what the map will show**: the archive bytes *and*
the layer/style spec published with them. Both are reduced to SHA-256 digests
and together form the :class:`VersionKey`.

==================================================  ====================================
Situation                                           Outcome
==================================================  ====================================
key equals the *current* version                    nothing changes, ``DEDUPLICATED``
key equals an older ``AVAILABLE`` version           pointer moves back, ``POINTER_MOVED``
key is new (new bytes, new spec, or both), or
only matches a ``RETIRED`` version                  new version ``latest_seq + 1``,
                                                    ``CREATED``
==================================================  ====================================

Republishing identical bytes with a changed spec therefore creates a new
version. It costs no storage: the archive's object key is content-addressed by
the bytes alone, so both versions point at the same object and the copy is
skipped. Rolling back restores the old styling along with the old pointer.

A ``RETIRED`` version is deliberately *not* reused: retiring is how an operator
says "never serve this again", so an identical publication must get a fresh
version row rather than silently resurrecting the retired one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from .enums import PublicationResult

__all__ = ["DatasetState", "VersionDecision", "VersionKey", "decide_version", "spec_digest"]

_HEX = frozenset("0123456789abcdef")


def spec_digest(spec: Mapping[str, Any] | None) -> str:
    """SHA-256 of a spec in canonical JSON form.

    Canonical means sorted keys, no insignificant whitespace, UTF-8: two specs
    that differ only in key order or formatting are the same spec. ``None``
    (no spec) has a digest too, so "no spec" is a value that compares like any
    other rather than a special case.
    """
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_hex(name: str, value: str) -> None:
    if len(value) != 64 or not set(value) <= _HEX:
        raise ValueError(f"{name} must be 64 lowercase hex characters, got {value!r}")


class VersionKey(NamedTuple):
    """What identifies a version: the archive bytes plus the spec."""

    sha256: str
    spec_sha256: str

    def validate(self) -> VersionKey:
        _check_hex("sha256", self.sha256)
        _check_hex("spec_sha256", self.spec_sha256)
        return self


@dataclass(frozen=True, slots=True)
class DatasetState:
    """The catalogue state of one dataset, read under a row lock."""

    latest_seq: int
    """Highest sequence number ever allocated for this dataset (never reused)."""

    current_seq: int | None = None
    """Sequence of the version the dataset pointer currently points at."""

    current_key: VersionKey | None = None
    """Key of the current version, if the dataset has one."""

    available: Mapping[VersionKey, int] = field(default_factory=dict)
    """``key -> seq`` for every version whose status is ``AVAILABLE``."""

    def __post_init__(self) -> None:
        if self.latest_seq < 0:
            raise ValueError("latest_seq cannot be negative")


@dataclass(frozen=True, slots=True)
class VersionDecision:
    """What the worker must do to the catalogue for this publication."""

    result: PublicationResult
    seq: int
    """Sequence number of the version that is current once this is applied."""

    create_version: bool
    """Insert a new ``dataset_versions`` row."""

    move_pointer: bool
    """Update ``datasets.current_version_id`` (and ``latest_seq`` when creating)."""


def decide_version(state: DatasetState, key: VersionKey) -> VersionDecision:
    """Decide what publishing ``key`` into ``state`` should do.

    Pure: no I/O, no clock, no randomness. Same inputs, same decision.
    """
    key.validate()

    if state.current_key == key:
        # The map would look exactly as it does now: a no-op the client can
        # retry freely.
        assert state.current_seq is not None
        return VersionDecision(
            result=PublicationResult.DEDUPLICATED,
            seq=state.current_seq,
            create_version=False,
            move_pointer=False,
        )

    existing_seq = state.available.get(key)
    if existing_seq is not None:
        # This exact bytes+spec combination already has a version row;
        # publishing it again is a rollback expressed as a publication.
        return VersionDecision(
            result=PublicationResult.POINTER_MOVED,
            seq=existing_seq,
            create_version=False,
            move_pointer=True,
        )

    return VersionDecision(
        result=PublicationResult.CREATED,
        seq=state.latest_seq + 1,
        create_version=True,
        move_pointer=True,
    )
