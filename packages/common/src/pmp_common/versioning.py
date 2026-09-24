"""The dataset versioning rules, as a pure function.

This is the one piece of business logic in the system that must be exactly
right, so it is isolated from Postgres, S3 and Kafka and unit-tested on its
own. The worker calls it while holding a row lock on the dataset
(``SELECT ... FOR UPDATE``) and then applies the returned decision inside the
same transaction.

The rules (content addressing makes republication idempotent):

==================================================  ====================================
Situation                                           Outcome
==================================================  ====================================
sha256 equals the *current* version                 nothing changes, ``DEDUPLICATED``
sha256 equals an older ``AVAILABLE`` version        pointer moves back, ``POINTER_MOVED``
sha256 is new, or only matches a ``RETIRED`` one    new version ``latest_seq + 1``,
                                                    ``CREATED``
==================================================  ====================================

A ``RETIRED`` version is deliberately *not* reused: retiring is how an operator
says "never serve these bytes again", so an identical upload must get a fresh
version row rather than silently resurrecting the retired one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .enums import PublicationResult

__all__ = ["DatasetState", "VersionDecision", "decide_version"]


@dataclass(frozen=True, slots=True)
class DatasetState:
    """The catalogue state of one dataset, read under a row lock."""

    latest_seq: int
    """Highest sequence number ever allocated for this dataset (never reused)."""

    current_seq: int | None = None
    """Sequence of the version the dataset pointer currently points at."""

    current_sha256: str | None = None
    """Content hash of the current version, if the dataset has one."""

    available_shas: Mapping[str, int] = field(default_factory=dict)
    """``sha256 -> seq`` for every version whose status is ``AVAILABLE``."""

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

    @property
    def new_latest_seq(self) -> int:
        return self.seq if self.create_version else -1


def decide_version(state: DatasetState, sha256: str) -> VersionDecision:
    """Decide what publishing ``sha256`` into ``state`` should do.

    Pure: no I/O, no clock, no randomness. Same inputs, same decision.
    """
    if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
        raise ValueError(f"sha256 must be 64 lowercase hex characters, got {sha256!r}")

    if state.current_sha256 == sha256:
        # Republishing the bytes that are already live: a no-op the client can
        # retry freely.
        assert state.current_seq is not None
        return VersionDecision(
            result=PublicationResult.DEDUPLICATED,
            seq=state.current_seq,
            create_version=False,
            move_pointer=False,
        )

    existing_seq = state.available_shas.get(sha256)
    if existing_seq is not None:
        # These bytes already have a version row; publishing them again is a
        # rollback expressed as an upload. No copy, no new row.
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
