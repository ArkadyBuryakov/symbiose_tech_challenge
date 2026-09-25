"""Request-model validation: the API's first line of defence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pmp_backend.schemas import CreatePublicationRequest, DemoUploadRequest, RollbackRequest
from pmp_common.enums import Visibility


def a_request(**overrides: object) -> CreatePublicationRequest:
    base: dict[str, object] = {
        "dataset_slug": "forest-crowns",
        "source_key": "org_a/upload-1/data.pmtiles",
    }
    return CreatePublicationRequest.model_validate(base | overrides)


def test_minimal_request_defaults_to_no_visibility_override() -> None:
    request = a_request()

    assert request.visibility is None
    assert request.name is None
    assert request.spec is None


@pytest.mark.parametrize(
    "slug",
    ["Forest", "-leading", "trailing-", "a", "with_underscore", "with space", "x" * 65, "ünïcode"],
)
def test_invalid_slugs_are_rejected(slug: str) -> None:
    """Slugs end up in URLs and object keys, so the allowed set stays narrow."""
    with pytest.raises(ValidationError):
        a_request(dataset_slug=slug)


@pytest.mark.parametrize("slug", ["ab", "forest-crowns", "h3-r10-2024", "x" * 64])
def test_valid_slugs_are_accepted(slug: str) -> None:
    assert a_request(dataset_slug=slug).dataset_slug == slug


@pytest.mark.parametrize(
    "source_key",
    ["/absolute/key", "org_a/../org_b/data.pmtiles", "../escape", ""],
)
def test_source_keys_that_could_escape_the_tenant_prefix_are_rejected(source_key: str) -> None:
    with pytest.raises(ValidationError):
        a_request(source_key=source_key)


def test_unknown_fields_are_rejected() -> None:
    """A typo in a client payload should fail loudly, not be silently dropped."""
    with pytest.raises(ValidationError):
        a_request(visibilty="private")


def test_visibility_is_parsed_from_its_wire_value() -> None:
    assert a_request(visibility="private").visibility is Visibility.PRIVATE


def test_spec_is_passed_through_unchanged() -> None:
    spec = {"layers": [{"layer": "h3_r10", "minzoom": 0}], "style": {"color_field": "count"}}

    assert a_request(spec=spec).spec == spec


def test_rollback_requires_a_positive_sequence() -> None:
    assert RollbackRequest(seq=1).seq == 1
    with pytest.raises(ValidationError):
        RollbackRequest(seq=0)


def test_demo_upload_sha256_must_be_lowercase_hex() -> None:
    assert DemoUploadRequest(sha256="a" * 64).sha256 == "a" * 64
    with pytest.raises(ValidationError):
        DemoUploadRequest()  # type: ignore[call-arg]  # the checksum is mandatory
    for bad in ["A" * 64, "z" * 64, "abc"]:
        with pytest.raises(ValidationError):
            DemoUploadRequest(sha256=bad)
