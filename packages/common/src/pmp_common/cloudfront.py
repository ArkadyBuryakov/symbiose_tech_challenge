"""CloudFront signed cookies.

Private tile archives are authorised by signed cookies rather than signed URLs,
because a PMTiles archive is read with many range requests to one URL: signing
the URL once and letting the cookie cover every subsequent read is both cheaper
and cacheable.

The format is CloudFront's, not ours, and is implemented exactly as AWS
documents it:

* the policy is compact JSON with **no whitespace** — CloudFront signs the
  literal bytes, so a space changes the signature;
* the signature is **RSA-SHA1**; this is not a choice, it is what CloudFront
  verifies with;
* both values are base64 with a CloudFront-specific alphabet
  (``+`` -> ``-``, ``=`` -> ``_``, ``/`` -> ``~``) because ``+``, ``=`` and
  ``/`` are not safe in a cookie value.

Locally the ``edge-verifier`` service checks these cookies; on AWS CloudFront
does, and that service is deleted. Because the format is identical, the backend
that *issues* the cookies does not change at all.

Reference: "Using signed cookies" in the Amazon CloudFront Developer Guide.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

__all__ = [
    "COOKIE_KEY_PAIR_ID",
    "COOKIE_POLICY",
    "COOKIE_SIGNATURE",
    "CloudFrontSigner",
    "CloudFrontVerifier",
    "PolicyError",
    "SignedCookies",
    "build_policy",
    "cf_b64decode",
    "cf_b64encode",
    "resource_matches",
]

COOKIE_POLICY = "CloudFront-Policy"
COOKIE_SIGNATURE = "CloudFront-Signature"
COOKIE_KEY_PAIR_ID = "CloudFront-Key-Pair-Id"

# CloudFront's base64 variant, applied *after* standard base64.
_TO_CF = str.maketrans({"+": "-", "=": "_", "/": "~"})
_FROM_CF = str.maketrans({"-": "+", "_": "=", "~": "/"})


class PolicyError(Exception):
    """The presented policy is missing, malformed, expired or out of scope."""


def cf_b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").translate(_TO_CF)


def cf_b64decode(value: str) -> bytes:
    return base64.b64decode(value.translate(_FROM_CF).encode("ascii"))


def build_policy(resource: str, expires_at: datetime) -> str:
    """Build a CloudFront custom policy for one resource pattern.

    Returned as compact JSON with sorted keys: the exact bytes are what gets
    signed, so they must be reproducible.
    """
    policy: dict[str, Any] = {
        "Statement": [
            {
                "Resource": resource,
                "Condition": {"DateLessThan": {"AWS:EpochTime": int(expires_at.timestamp())}},
            }
        ]
    }
    return json.dumps(policy, separators=(",", ":"))


def resource_matches(pattern: str, url: str) -> bool:
    """CloudFront resource matching: ``*`` any sequence, ``?`` any one character.

    Implemented by translating to a regex rather than using ``fnmatch``, whose
    ``[...]`` character classes are not part of CloudFront's syntax and would
    make some patterns match more than AWS would.
    """
    escaped = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(escaped, url) is not None


@dataclass(frozen=True, slots=True)
class SignedCookies:
    policy: str
    signature: str
    key_pair_id: str
    expires_at: datetime
    resource: str

    def as_dict(self) -> dict[str, str]:
        return {
            COOKIE_POLICY: self.policy,
            COOKIE_SIGNATURE: self.signature,
            COOKIE_KEY_PAIR_ID: self.key_pair_id,
        }


class CloudFrontSigner:
    """Issues signed cookies. Only the backend holds the private key."""

    def __init__(self, private_key_pem: str | bytes, *, key_pair_id: str) -> None:
        pem = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("CloudFront cookie signing requires an RSA private key")
        self._key = key
        self._key_pair_id = key_pair_id

    @classmethod
    def from_file(cls, path: str, *, key_pair_id: str) -> CloudFrontSigner:
        from .config import read_secret_file

        return cls(read_secret_file(path, what="CloudFront signing key"), key_pair_id=key_pair_id)

    def sign(self, resource: str, *, ttl_seconds: int) -> SignedCookies:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        policy = build_policy(resource, expires_at)
        # SHA-1 is not a choice: CloudFront verifies RSA-SHA1 signatures and
        # nothing else. The hash is over a short, self-signed, short-lived
        # policy document, so its collision weakness is not exploitable here.
        signature = self._key.sign(
            policy.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA1(),  # noqa: S303 - mandated by the CloudFront cookie format
        )
        return SignedCookies(
            policy=cf_b64encode(policy.encode("utf-8")),
            signature=cf_b64encode(signature),
            key_pair_id=self._key_pair_id,
            expires_at=expires_at,
            resource=resource,
        )


class CloudFrontVerifier:
    """Validates signed cookies against a requested URL.

    This is what CloudFront does for us on AWS; locally the ``edge-verifier``
    service runs it behind nginx's ``auth_request``.
    """

    def __init__(self, public_key_pem: str | bytes, *, key_pair_id: str | None = None) -> None:
        pem = public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
        key = serialization.load_pem_public_key(pem)
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("CloudFront cookie verification requires an RSA public key")
        self._key = key
        self._key_pair_id = key_pair_id

    @classmethod
    def from_file(cls, path: str, **kwargs: Any) -> CloudFrontVerifier:
        from .config import read_secret_file

        return cls(read_secret_file(path, what="CloudFront public key"), **kwargs)

    def verify(
        self,
        *,
        policy_b64: str | None,
        signature_b64: str | None,
        key_pair_id: str | None,
        url: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate the cookies for ``url``; raises :class:`PolicyError` if not valid.

        Order matters: the signature is checked *before* the policy is trusted
        for anything, so an attacker cannot make decisions happen based on a
        policy they wrote.
        """
        if not policy_b64 or not signature_b64:
            raise PolicyError("signed tile cookies are missing")
        if self._key_pair_id is not None and key_pair_id != self._key_pair_id:
            raise PolicyError("unknown key pair id")

        try:
            policy_bytes = cf_b64decode(policy_b64)
            signature = cf_b64decode(signature_b64)
        except (ValueError, TypeError) as exc:
            raise PolicyError("signed tile cookies are malformed") from exc

        try:
            self._key.verify(
                signature,
                policy_bytes,
                padding.PKCS1v15(),
                hashes.SHA1(),  # noqa: S303 - mandated by the CloudFront cookie format
            )
        except InvalidSignature as exc:
            raise PolicyError("signature does not match the policy") from exc

        try:
            policy = json.loads(policy_bytes)
            statement = policy["Statement"][0]
            resource = str(statement["Resource"])
            expires = int(statement["Condition"]["DateLessThan"]["AWS:EpochTime"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PolicyError("policy is not a well-formed CloudFront policy") from exc

        moment = now or datetime.now(UTC)
        if moment.timestamp() >= expires:
            raise PolicyError("policy has expired")

        if not resource_matches(resource, url):
            # The signature is valid but the policy does not cover this object:
            # this is the tenant-isolation check.
            raise PolicyError("policy does not cover the requested object")

        return {"resource": resource, "expires_at": expires}
