import re

import pytest

from trialerror.util.ids import InvalidIdError, new_id, new_ulid, split_id

_CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_new_ulid_shape():
    u = new_ulid()
    assert len(u) == 26
    assert _CROCKFORD_RE.match(u), u
    # Crockford alphabet excludes I, L, O, U
    assert not (set(u) & {"I", "L", "O", "U"})


def test_new_ulid_uniqueness():
    values = {new_ulid() for _ in range(500)}
    assert len(values) == 500


def test_new_ulid_deterministic_with_seams():
    a = new_ulid(_timestamp_ms=0, _random_bytes=b"\x00" * 10)
    assert a == "0" * 26
    b = new_ulid(_timestamp_ms=0, _random_bytes=b"\xff" * 10)
    c = new_ulid(_timestamp_ms=0, _random_bytes=b"\x00" * 10)
    assert a == c
    assert a != b


def test_new_ulid_rejects_wrong_random_length():
    with pytest.raises(InvalidIdError):
        new_ulid(_random_bytes=b"\x00")


def test_new_id_prefix_and_ulid():
    value = new_id("SRC")
    prefix, ulid = value.split("-", 1)
    assert prefix == "SRC"
    assert _CROCKFORD_RE.match(ulid)


@pytest.mark.parametrize("bad_prefix", ["", "src", "S RC", "S-RC", "1SRC", "srcid"])
def test_new_id_rejects_bad_prefix(bad_prefix):
    with pytest.raises(InvalidIdError):
        new_id(bad_prefix)


def test_split_id_roundtrip():
    value = new_id("LNCH")
    prefix, ulid = split_id(value)
    assert prefix == "LNCH"
    assert value == f"{prefix}-{ulid}"


def test_split_id_rejects_non_typed_id():
    with pytest.raises(InvalidIdError):
        split_id("nodashatall")
    with pytest.raises(InvalidIdError):
        split_id("-emptyprefix")
    with pytest.raises(InvalidIdError):
        split_id("trailingdash-")
