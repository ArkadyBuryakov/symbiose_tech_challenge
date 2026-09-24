"""Identifier helpers.

UUIDv7 is used for every generated identifier so that primary keys are
time-ordered (better index locality in Postgres, and job/version ids sort by
creation time in listings) while staying globally unique.
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["new_uuid", "uuid7"]


def uuid7() -> uuid.UUID:
    """Return a UUID version 7 (RFC 9562): 48-bit unix-ms prefix + randomness."""
    unix_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = os.urandom(10)
    value = bytearray(unix_ms.to_bytes(6, "big") + rand)
    value[6] = (value[6] & 0x0F) | 0x70  # version 7
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(value))


def new_uuid() -> uuid.UUID:
    """Alias used across the codebase so the id scheme can be swapped in one place."""
    return uuid7()
