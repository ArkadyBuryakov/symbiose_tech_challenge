"""The versioning rules are the correctness core of the platform."""

from __future__ import annotations

import pytest

from pmp_common.enums import PublicationResult
from pmp_common.versioning import DatasetState, decide_version

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def test_first_publication_creates_version_1() -> None:
    decision = decide_version(DatasetState(latest_seq=0), SHA_A)

    assert decision.result is PublicationResult.CREATED
    assert decision.seq == 1
    assert decision.create_version
    assert decision.move_pointer


def test_republishing_the_current_content_deduplicates() -> None:
    state = DatasetState(
        latest_seq=2, current_seq=2, current_sha256=SHA_B, available_shas={SHA_A: 1, SHA_B: 2}
    )

    decision = decide_version(state, SHA_B)

    assert decision.result is PublicationResult.DEDUPLICATED
    assert decision.seq == 2
    assert not decision.create_version
    assert not decision.move_pointer


def test_publishing_an_older_available_version_moves_the_pointer_back() -> None:
    state = DatasetState(
        latest_seq=3, current_seq=3, current_sha256=SHA_C, available_shas={SHA_A: 1, SHA_C: 3}
    )

    decision = decide_version(state, SHA_A)

    assert decision.result is PublicationResult.POINTER_MOVED
    assert decision.seq == 1
    assert not decision.create_version
    assert decision.move_pointer


def test_new_content_allocates_latest_seq_plus_one() -> None:
    state = DatasetState(
        latest_seq=7, current_seq=7, current_sha256=SHA_A, available_shas={SHA_A: 7}
    )

    decision = decide_version(state, SHA_B)

    assert decision.result is PublicationResult.CREATED
    assert decision.seq == 8
    assert decision.new_latest_seq == 8


def test_retired_content_is_not_resurrected() -> None:
    """A retired version is excluded from ``available_shas`` by the caller, so
    re-uploading the same bytes must allocate a fresh sequence number."""
    state = DatasetState(
        latest_seq=4, current_seq=2, current_sha256=SHA_B, available_shas={SHA_B: 2}
    )

    decision = decide_version(state, SHA_C)  # SHA_C was seq 3, now RETIRED

    assert decision.result is PublicationResult.CREATED
    assert decision.seq == 5


def test_sequence_numbers_are_never_reused_after_a_rollback() -> None:
    """After rolling back to seq 1, publishing new content still gets seq 3."""
    state = DatasetState(
        latest_seq=2, current_seq=1, current_sha256=SHA_A, available_shas={SHA_A: 1, SHA_B: 2}
    )

    decision = decide_version(state, SHA_C)

    assert decision.seq == 3


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
def test_malformed_hashes_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        decide_version(DatasetState(latest_seq=0), bad)


def test_negative_latest_seq_is_rejected() -> None:
    with pytest.raises(ValueError, match="latest_seq"):
        DatasetState(latest_seq=-1)
