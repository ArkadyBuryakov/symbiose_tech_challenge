"""The dataset versioning rules, as a pure function.

The worker calls :func:`decide_version` while holding a row lock on the dataset
(``SELECT ... FOR UPDATE``) and applies the returned decision inside the same
transaction.

A version is identified by **what the map will show**: the archive bytes *and*
the layer/style spec published with them (see ``docs/DECISIONS.md``). Both are
reduced to SHA-256 digests and together form the :class:`VersionKey`.

==================================================  ====================================
Situation                                           Outcome
==================================================  ====================================
key equals the *current* version                    nothing changes, ``DEDUPLICATED``
key equals an older ``AVAILABLE`` version           pointer moves back, ``POINTER_MOVED``
key is new, or only matches a ``RETIRED`` version   new version ``latest_seq + 1``,
                                                    ``CREATED``
==================================================  ====================================

Identical bytes with a changed spec therefore create a new version that shares
the same content-addressed object. A ``RETIRED`` version is never reused.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from .enums import PublicationResult

__all__ = ["DatasetState", "VersionDecision", "VersionKey", "decide_version", "spec_digest"]


def spec_digest(spec: Mapping[str, Any] | None) -> str:
    """SHA-256 of a spec in canonical JSON form (sorted keys, no whitespace).

    ``None`` (no spec) has a digest too, so it compares like any other value.
    """
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class VersionKey(NamedTuple):
    """What identifies a version: the archive bytes plus the spec."""

    sha256: str
    spec_sha256: str


@dataclass(frozen=True, slots=True)
class DatasetState:
    """The catalogue state of one dataset, read under a row lock."""

    latest_seq: int
    """Highest sequence number ever allocated for this dataset (never reused)."""

    current_key: VersionKey | None = None
    """Key of the version the dataset pointer points at, if any."""

    available: Mapping[VersionKey, int] = field(default_factory=dict)
    """``key -> seq`` for every version whose status is ``AVAILABLE``."""


@dataclass(frozen=True, slots=True)
class VersionDecision:
    """What the worker must do to the catalogue for this publication."""

    result: PublicationResult
    seq: int
    """Sequence number of the version that is current once this is applied."""

    @property
    def create_version(self) -> bool:
        return self.result is PublicationResult.CREATED


def decide_version(state: DatasetState, key: VersionKey) -> VersionDecision:
    """Decide what publishing ``key`` into ``state`` should do. Pure."""
    existing_seq = state.available.get(key)
    if existing_seq is None:
        return VersionDecision(PublicationResult.CREATED, state.latest_seq + 1)
    if key == state.current_key:
        return VersionDecision(PublicationResult.DEDUPLICATED, existing_seq)
    return VersionDecision(PublicationResult.POINTER_MOVED, existing_seq)
