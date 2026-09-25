"""The versioning rules are the correctness core of the platform."""

from __future__ import annotations

from pmp_common.enums import PublicationResult
from pmp_common.versioning import DatasetState, VersionKey, decide_version, spec_digest

SPEC_1 = spec_digest({"style": {"color_field": "count"}})
SPEC_2 = spec_digest({"style": {"color_field": "trees_ha"}})

A1 = VersionKey("a" * 64, SPEC_1)  # bytes A, spec 1
A2 = VersionKey("a" * 64, SPEC_2)  # same bytes A, different spec
B1 = VersionKey("b" * 64, SPEC_1)
C1 = VersionKey("c" * 64, SPEC_1)


def test_first_publication_creates_version_1() -> None:
    decision = decide_version(DatasetState(latest_seq=0), A1)

    assert decision.result is PublicationResult.CREATED
    assert decision.seq == 1
    assert decision.create_version


def test_same_bytes_and_same_spec_deduplicates() -> None:
    state = DatasetState(latest_seq=2, current_key=B1, available={A1: 1, B1: 2})

    decision = decide_version(state, B1)

    assert decision.result is PublicationResult.DEDUPLICATED
    assert decision.seq == 2
    assert not decision.create_version


def test_same_bytes_with_a_changed_spec_creates_a_new_version() -> None:
    """The case that used to be silently deduplicated, dropping the new spec."""
    state = DatasetState(latest_seq=1, current_key=A1, available={A1: 1})

    decision = decide_version(state, A2)

    assert decision.result is PublicationResult.CREATED
    assert decision.seq == 2


def test_new_bytes_with_the_same_spec_creates_a_new_version() -> None:
    state = DatasetState(latest_seq=1, current_key=A1, available={A1: 1})

    assert decide_version(state, B1).result is PublicationResult.CREATED


def test_republishing_an_older_bytes_and_spec_pair_moves_the_pointer_back() -> None:
    state = DatasetState(latest_seq=2, current_key=A2, available={A1: 1, A2: 2})

    decision = decide_version(state, A1)

    assert decision.result is PublicationResult.POINTER_MOVED
    assert decision.seq == 1
    assert not decision.create_version


def test_older_bytes_with_a_spec_never_paired_with_them_is_new() -> None:
    """Matching the bytes of an old version is not enough; the pair must match."""
    state = DatasetState(latest_seq=2, current_key=B1, available={A1: 1, B1: 2})

    decision = decide_version(state, A2)

    assert decision.result is PublicationResult.CREATED
    assert decision.seq == 3


def test_retired_versions_are_not_resurrected() -> None:
    """A retired version is excluded from ``available`` by the caller, so the
    same publication must allocate a fresh sequence number."""
    state = DatasetState(latest_seq=4, current_key=B1, available={B1: 2})

    decision = decide_version(state, C1)  # C1 was seq 3, now RETIRED

    assert decision.result is PublicationResult.CREATED
    assert decision.seq == 5


def test_sequence_numbers_are_never_reused_after_a_rollback() -> None:
    state = DatasetState(latest_seq=2, current_key=A1, available={A1: 1, B1: 2})

    assert decide_version(state, C1).seq == 3


# --------------------------------------------------------------------------
# spec_digest
# --------------------------------------------------------------------------
def test_spec_digest_ignores_key_order_and_formatting() -> None:
    assert spec_digest({"a": 1, "b": {"c": 2, "d": 3}}) == spec_digest(
        {"b": {"d": 3, "c": 2}, "a": 1}
    )


def test_spec_digest_distinguishes_real_changes() -> None:
    assert spec_digest({"max": 728}) != spec_digest({"max": 729})
    assert spec_digest({"layers": [1, 2]}) != spec_digest({"layers": [2, 1]})


def test_no_spec_has_a_stable_digest_distinct_from_an_empty_one() -> None:
    assert spec_digest(None) == spec_digest(None)
    assert spec_digest(None) != spec_digest({})
    assert len(spec_digest(None)) == 64
