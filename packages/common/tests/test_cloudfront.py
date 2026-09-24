"""CloudFront signed cookies.

The format is CloudFront's, so the tests check it against botocore's
``CloudFrontSigner`` — AWS's own reference implementation — rather than against
our own idea of what it should look like. If botocore and this module ever
disagree, CloudFront would reject the cookies on AWS.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from botocore.signers import CloudFrontSigner as BotocoreSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pmp_common.cloudfront import (
    COOKIE_KEY_PAIR_ID,
    COOKIE_POLICY,
    COOKIE_SIGNATURE,
    CloudFrontSigner,
    CloudFrontVerifier,
    PolicyError,
    build_policy,
    cf_b64decode,
    cf_b64encode,
    resource_matches,
)

ORIGIN = "http://localhost:8080"
TENANT_A = f"{ORIGIN}/tiles/private/org_tenant-a/*"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[bytes, bytes, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public, key


@pytest.fixture
def signer(rsa_keys: tuple[bytes, bytes, rsa.RSAPrivateKey]) -> CloudFrontSigner:
    return CloudFrontSigner(rsa_keys[0], key_pair_id="K2JCJMDEHXQW5F")


@pytest.fixture
def verifier(rsa_keys: tuple[bytes, bytes, rsa.RSAPrivateKey]) -> CloudFrontVerifier:
    return CloudFrontVerifier(rsa_keys[1], key_pair_id="K2JCJMDEHXQW5F")


# botocore's `_url_b64encode` is private, but it *is* AWS's reference encoder —
# which is exactly why these tests call it (hence the type-ignores below).
def botocore_signer(key: rsa.RSAPrivateKey) -> BotocoreSigner:
    def rsa_signer(message: bytes) -> bytes:
        return key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    return BotocoreSigner("K2JCJMDEHXQW5F", rsa_signer)


# --------------------------------------------------------------------------
# Format, checked against AWS's reference implementation
# --------------------------------------------------------------------------
def test_policy_is_byte_identical_to_botocore(
    rsa_keys: tuple[bytes, bytes, rsa.RSAPrivateKey],
) -> None:
    """CloudFront signs the literal policy bytes, so key order and whitespace
    must match exactly."""
    expires = datetime(2030, 1, 1, tzinfo=UTC)

    ours = build_policy(TENANT_A, expires)
    theirs = botocore_signer(rsa_keys[2]).build_policy(TENANT_A, expires)

    assert ours == theirs


def test_policy_has_no_whitespace() -> None:
    policy = build_policy(TENANT_A, datetime(2030, 1, 1, tzinfo=UTC))

    assert " " not in policy
    assert "\n" not in policy


def test_policy_structure_matches_the_documented_custom_policy() -> None:
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    policy = json.loads(build_policy(TENANT_A, expires))

    statement = policy["Statement"][0]
    assert statement["Resource"] == TENANT_A
    assert statement["Condition"]["DateLessThan"]["AWS:EpochTime"] == int(expires.timestamp())


def test_base64_uses_cloudfront_s_alphabet(
    rsa_keys: tuple[bytes, bytes, rsa.RSAPrivateKey],
) -> None:
    """`+`, `=` and `/` are not cookie-safe; CloudFront substitutes `-`, `_`, `~`."""
    # Bytes chosen so that standard base64 contains all three characters.
    data = bytes([0xFB, 0xFF, 0xBF]) + b"?"

    encoded = cf_b64encode(data)

    assert encoded == botocore_signer(rsa_keys[2])._url_b64encode(data).decode()  # type: ignore[attr-defined]
    assert not set(encoded) & {"+", "=", "/"}
    assert cf_b64decode(encoded) == data


def test_cookie_signed_by_botocore_verifies_here(
    rsa_keys: tuple[bytes, bytes, rsa.RSAPrivateKey], verifier: CloudFrontVerifier
) -> None:
    """The edge-verifier must accept exactly what CloudFront would accept."""
    expires = datetime.now(UTC) + timedelta(minutes=10)
    boto = botocore_signer(rsa_keys[2])
    policy = boto.build_policy(TENANT_A, expires).encode()
    signature = rsa_keys[2].sign(policy, padding.PKCS1v15(), hashes.SHA1())

    result = verifier.verify(
        policy_b64=boto._url_b64encode(policy).decode(),  # type: ignore[attr-defined]
        signature_b64=boto._url_b64encode(signature).decode(),  # type: ignore[attr-defined]
        key_pair_id="K2JCJMDEHXQW5F",
        url=f"{ORIGIN}/tiles/private/org_tenant-a/ds/abc/data.pmtiles",
    )

    assert result["resource"] == TENANT_A


def test_cookie_names_are_cloudfront_s() -> None:
    assert COOKIE_POLICY == "CloudFront-Policy"
    assert COOKIE_SIGNATURE == "CloudFront-Signature"
    assert COOKIE_KEY_PAIR_ID == "CloudFront-Key-Pair-Id"


# --------------------------------------------------------------------------
# Round trip and the checks that enforce tenant isolation
# --------------------------------------------------------------------------
def test_round_trip(signer: CloudFrontSigner, verifier: CloudFrontVerifier) -> None:
    cookies = signer.sign(TENANT_A, ttl_seconds=600).as_dict()

    verifier.verify(
        policy_b64=cookies[COOKIE_POLICY],
        signature_b64=cookies[COOKIE_SIGNATURE],
        key_pair_id=cookies[COOKIE_KEY_PAIR_ID],
        url=f"{ORIGIN}/tiles/private/org_tenant-a/ds/sha/data.pmtiles",
    )


def test_another_tenant_s_objects_are_refused(
    signer: CloudFrontSigner, verifier: CloudFrontVerifier
) -> None:
    """A valid tenant-a cookie must not open tenant-b's tiles."""
    cookies = signer.sign(TENANT_A, ttl_seconds=600).as_dict()

    with pytest.raises(PolicyError, match="does not cover"):
        verifier.verify(
            policy_b64=cookies[COOKIE_POLICY],
            signature_b64=cookies[COOKIE_SIGNATURE],
            key_pair_id=cookies[COOKIE_KEY_PAIR_ID],
            url=f"{ORIGIN}/tiles/private/org_tenant-b/ds/sha/data.pmtiles",
        )


def test_a_tenant_whose_id_is_a_prefix_of_another_is_refused(
    signer: CloudFrontSigner, verifier: CloudFrontVerifier
) -> None:
    """`org_tenant-a/*` must not match `org_tenant-ab/...`: the trailing slash
    in the pattern is what separates them."""
    cookies = signer.sign(TENANT_A, ttl_seconds=600).as_dict()

    with pytest.raises(PolicyError, match="does not cover"):
        verifier.verify(
            policy_b64=cookies[COOKIE_POLICY],
            signature_b64=cookies[COOKIE_SIGNATURE],
            key_pair_id=cookies[COOKIE_KEY_PAIR_ID],
            url=f"{ORIGIN}/tiles/private/org_tenant-ab/ds/sha/data.pmtiles",
        )


def test_an_expired_cookie_is_refused(
    signer: CloudFrontSigner, verifier: CloudFrontVerifier
) -> None:
    cookies = signer.sign(TENANT_A, ttl_seconds=600).as_dict()

    with pytest.raises(PolicyError, match="expired"):
        verifier.verify(
            policy_b64=cookies[COOKIE_POLICY],
            signature_b64=cookies[COOKIE_SIGNATURE],
            key_pair_id=cookies[COOKIE_KEY_PAIR_ID],
            url=f"{ORIGIN}/tiles/private/org_tenant-a/x",
            now=datetime.now(UTC) + timedelta(minutes=11),
        )


def test_a_rewritten_policy_is_refused(
    signer: CloudFrontSigner, verifier: CloudFrontVerifier
) -> None:
    """Widening the resource to `*` breaks the signature."""
    cookies = signer.sign(TENANT_A, ttl_seconds=600).as_dict()
    forged = cf_b64encode(
        build_policy(f"{ORIGIN}/tiles/private/*", datetime.now(UTC) + timedelta(days=1)).encode()
    )

    with pytest.raises(PolicyError, match="signature"):
        verifier.verify(
            policy_b64=forged,
            signature_b64=cookies[COOKIE_SIGNATURE],
            key_pair_id=cookies[COOKIE_KEY_PAIR_ID],
            url=f"{ORIGIN}/tiles/private/org_tenant-b/x",
        )


def test_a_cookie_signed_with_another_key_is_refused(verifier: CloudFrontVerifier) -> None:
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_pem = attacker_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cookies = CloudFrontSigner(attacker_pem, key_pair_id="K2JCJMDEHXQW5F").sign(
        TENANT_A, ttl_seconds=600
    )

    with pytest.raises(PolicyError, match="signature"):
        verifier.verify(
            policy_b64=cookies.policy,
            signature_b64=cookies.signature,
            key_pair_id=cookies.key_pair_id,
            url=f"{ORIGIN}/tiles/private/org_tenant-a/x",
        )


@pytest.mark.parametrize(
    ("policy", "signature"),
    [(None, "x"), ("x", None), ("", ""), ("!!!not-base64!!!", "!!!")],
)
def test_missing_or_garbage_cookies_are_refused(
    verifier: CloudFrontVerifier, policy: str | None, signature: str | None
) -> None:
    with pytest.raises(PolicyError):
        verifier.verify(
            policy_b64=policy,
            signature_b64=signature,
            key_pair_id="K2JCJMDEHXQW5F",
            url=f"{ORIGIN}/tiles/private/org_tenant-a/x",
        )


def test_an_unknown_key_pair_id_is_refused(
    signer: CloudFrontSigner, verifier: CloudFrontVerifier
) -> None:
    cookies = signer.sign(TENANT_A, ttl_seconds=600)

    with pytest.raises(PolicyError, match="key pair"):
        verifier.verify(
            policy_b64=cookies.policy,
            signature_b64=cookies.signature,
            key_pair_id="SOMEONE-ELSES-KEY",
            url=f"{ORIGIN}/tiles/private/org_tenant-a/x",
        )


# --------------------------------------------------------------------------
# Resource matching
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("http://h/tiles/private/a/*", "http://h/tiles/private/a/x/y.pmtiles", True),
        ("http://h/tiles/private/a/*", "http://h/tiles/private/b/x", False),
        ("http://h/tiles/private/*", "http://h/tiles/private/b/x", True),
        ("http://h/a?c", "http://h/abc", True),
        ("http://h/a?c", "http://h/abbc", False),
        # Regex metacharacters in the pattern are literal to CloudFront.
        ("http://h/a.b/*", "http://h/aXb/c", False),
        ("http://h/[ab]/*", "http://h/a/c", False),
        ("http://h/[ab]/*", "http://h/[ab]/c", True),
    ],
)
def test_resource_matching_follows_cloudfront_semantics(
    pattern: str, url: str, expected: bool
) -> None:
    assert resource_matches(pattern, url) is expected


def test_a_non_rsa_key_is_rejected_up_front() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="RSA"):
        CloudFrontSigner(pem, key_pair_id="x")
