from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture(scope="session")
def ed25519_keypair() -> tuple[bytes, bytes]:
    """A throwaway Ed25519 keypair in PEM form (private, public)."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    public = key.public_key().public_bytes(ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo)
    return private, public
