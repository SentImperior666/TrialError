"""Typed ULIDs. Design Section 4: "all ids are ULIDs with human-readable
typed prefixes; per-program ``trialerror.toml`` can pin legacy prefix styles
(origin-project keeps ``C-####``, ``CR-###``, ``S###``) so migration preserves
citations."

A ULID is a 128-bit value: a 48-bit millisecond Unix timestamp followed by
80 bits of randomness, Crockford Base32-encoded to 26 characters. Unlike a
plain UUID4 it sorts lexicographically by creation time, which is a useful
property for ids that also serve as append-order clues in event/feed tables.

This module implements the ULID spec directly (no vendored dependency —
the encoding is ~20 lines and vendoring a whole package for it would be the
kind of thing ``vendored/VENDORED.md`` exists to keep honest about).
"""

from __future__ import annotations

import os
import re
import time

__all__ = ["new_ulid", "new_id", "split_id", "InvalidIdError"]

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LEN = 26
_TIMESTAMP_BITS = 48
_RANDOM_BYTES = 10  # 80 bits
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{0,15}$")


class InvalidIdError(ValueError):
    """Raised for a malformed id prefix or an id that fails to parse."""


def _encode_crockford(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_ulid(_timestamp_ms: int | None = None, _random_bytes: bytes | None = None) -> str:
    """Generate a new ULID string (26 Crockford-Base32 characters).

    ``_timestamp_ms``/``_random_bytes`` are internal seams for deterministic
    tests; production callers never pass them.
    """
    ts_ms = _timestamp_ms if _timestamp_ms is not None else int(time.time() * 1000)
    ts_ms &= (1 << _TIMESTAMP_BITS) - 1
    rand = _random_bytes if _random_bytes is not None else os.urandom(_RANDOM_BYTES)
    if len(rand) != _RANDOM_BYTES:
        raise InvalidIdError(f"_random_bytes must be {_RANDOM_BYTES} bytes, got {len(rand)}")
    value = (ts_ms << (_RANDOM_BYTES * 8)) | int.from_bytes(rand, "big")
    return _encode_crockford(value, _ULID_LEN)


def new_id(prefix: str) -> str:
    """Generate a typed id: ``"<PREFIX>-<ulid>"`` (e.g. ``"SRC-01J...".``).

    ``prefix`` must be uppercase letters/digits, starting with a letter
    (matches every native-id prefix used in design Section 4: SRC, DOC,
    CHK, ANC, LNCH, CR, ...). Raises :class:`InvalidIdError` otherwise.
    """
    if not _PREFIX_RE.match(prefix):
        raise InvalidIdError(
            f"invalid id prefix {prefix!r}: must match {_PREFIX_RE.pattern}"
        )
    return f"{prefix}-{new_ulid()}"


def split_id(value: str) -> tuple[str, str]:
    """Split a typed id back into ``(prefix, ulid)``. Raises
    :class:`InvalidIdError` if ``value`` doesn't look like a typed id."""
    prefix, sep, ulid = value.partition("-")
    if not sep or not prefix or not ulid:
        raise InvalidIdError(f"not a typed id: {value!r}")
    return prefix, ulid
