"""M4 acceptance criteria, design Section 12 row, gathered in one place —
mirrors the ``tests/test_m0_acceptance.py`` / ``tests/test_m1_acceptance.py``
convention: this file IS the acceptance-criteria mapping, each test here
re-running (not replacing) a narrower assertion that already lives in its
dedicated module.

    | Acceptance criterion (design Section 12, M4 row)      | Test |
    |----------------------------------------------------------|------|
    | append w/o digest impossible (single API)                | test_append_without_digest_is_impossible (see test_law_service.py) |
    | tampered ledger detected by chain verify                 | test_tampered_ledger_detected_by_chain_verify (see test_law_chain.py, test_law_service.py, test_law_checks.py) |
    | stale pin fails `law verify`                              | test_stale_pin_fails_law_verify (see test_law_service.py, test_cli_law.py) |
    | diff-foreign lists post-pin rows                          | test_diff_foreign_lists_post_pin_rows (see test_law_service.py, test_cli_law.py) |
"""

from __future__ import annotations

import pytest

from trialerror.law.chain import verify_chain
from trialerror.law.service import append_ruling, diff_foreign, verify_pin
from trialerror.stores.errors import ValidationError

pytestmark = pytest.mark.acceptance


def test_append_without_digest_is_impossible(store):
    """The public surface of ``trialerror.law`` offers exactly one mutator for
    the ruling ledger, and it always regenerates the digest in the same
    ops.db transaction. Positive case: one call, both rows land, linked.
    Negative case: a mid-transaction failure leaves ZERO trace (not a
    ruling with no digest) — real transactional atomicity, not just "one
    Python function calls two others."
    """
    result = append_ruling(store, summary="the only way in")
    assert store.ops.execute("SELECT COUNT(*) FROM ruling").fetchone()[0] == 1
    assert store.ops.execute("SELECT COUNT(*) FROM law_digest").fetchone()[0] == 1
    digest_row = store.ops.execute(
        "SELECT * FROM law_digest WHERE version = ?", (result.digest_version,)
    ).fetchone()
    assert digest_row is not None

    with pytest.raises(ValidationError):
        append_ruling(store, summary="doomed append", supersedes="C-does-not-exist")
    # still exactly one of each -- the failed attempt left no partial row
    assert store.ops.execute("SELECT COUNT(*) FROM ruling").fetchone()[0] == 1
    assert store.ops.execute("SELECT COUNT(*) FROM law_digest").fetchone()[0] == 1


def test_tampered_ledger_detected_by_chain_verify(store):
    r1 = append_ruling(store, summary="one")
    append_ruling(store, summary="two")

    # a clean chain verifies ok first (negative control)
    assert verify_chain(store.ops).ok is True

    with store.ops:
        store.ops.execute("UPDATE ruling SET summary = ? WHERE ruling_id = ?", ("TAMPERED", r1.ruling_id))

    result = verify_chain(store.ops)
    assert result.ok is False
    assert result.first_break_ruling_id == r1.ruling_id

    # law verify (the function hooks call) refuses too, even with an
    # otherwise-fresh pin, because it checks chain integrity as well as
    # freshness (design Sec 12 names both criteria as things this ONE
    # entry point must catch).
    latest = append_ruling(store, summary="three")  # bumps the pin forward past the tamper
    pin_verify = verify_pin(store, latest.pin)
    assert pin_verify.valid is False
    assert pin_verify.chain_ok is False


def test_stale_pin_fails_law_verify(store):
    first = append_ruling(store, summary="one", ts="2026-01-01T00:00:00.000Z")
    append_ruling(store, summary="two", ts="2026-01-02T00:00:00.000Z")

    result = verify_pin(store, first.pin)
    assert result.valid is False
    assert result.pin_stale is True
    assert result.current_pin != first.pin

    # the current pin, by contrast, verifies fine
    current = verify_pin(store, result.current_pin)
    assert current.valid is True


def test_diff_foreign_lists_post_pin_rows(store):
    r1 = append_ruling(store, summary="mine", ts="2026-01-01T00:00:00.000Z")
    r2 = append_ruling(store, summary="foreign one", ts="2026-01-02T00:00:00.000Z")
    r3 = append_ruling(store, summary="foreign two", ts="2026-01-03T00:00:00.000Z")

    foreign = diff_foreign(store, r1.pin)
    assert [r["ruling_id"] for r in foreign] == [r2.ruling_id, r3.ruling_id]
    assert diff_foreign(store, r3.pin) == []
