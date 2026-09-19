"""``trialerror.law.chain`` — the hash-chain primitive. Covers the acceptance
criterion "tampered ledger detected by chain verify" (design Section 12, M4
row) at the unit level; ``tests/test_m4_acceptance.py`` re-runs the
end-to-end version through ``append_ruling``.
"""

from __future__ import annotations

from trialerror.law.chain import GENESIS_HASH, canonical_ruling_repr, compute_ledger_hash, verify_chain
from trialerror.stores import insert
from trialerror.util.timeutil import now


def _row(ruling_id: str, ts: str, summary: str) -> dict:
    return {
        "ruling_id": ruling_id,
        "ts": ts,
        "verbatim_quote": None,
        "summary": summary,
        "standing_clauses": "[]",
        "domains": "[]",
        "supersedes": None,
        "supersedes_note": None,
        "status": "active",
    }


def _insert_chained(store, ruling_id: str, ts: str, summary: str, prev_hash: str) -> str:
    row = _row(ruling_id, ts, summary)
    h = compute_ledger_hash(prev_hash, row)
    row["ledger_sha256_after"] = h
    insert(store, "ruling", row)
    return h


def test_canonical_ruling_repr_is_deterministic():
    row = {"ruling_id": "C-0001", "ts": "T", "summary": "x", "status": "active"}
    assert canonical_ruling_repr(row) == canonical_ruling_repr(dict(row))


def test_canonical_ruling_repr_ignores_fields_outside_the_chain_set():
    row1 = {"ruling_id": "C-0001", "ts": "T", "summary": "x", "status": "active"}
    row2 = dict(row1, ledger_sha256_after="whatever", rowid=42)
    assert canonical_ruling_repr(row1) == canonical_ruling_repr(row2)


def test_canonical_ruling_repr_changes_when_a_chained_field_changes():
    row1 = {"ruling_id": "C-0001", "ts": "T", "summary": "x", "status": "active"}
    row2 = dict(row1, summary="y")
    assert canonical_ruling_repr(row1) != canonical_ruling_repr(row2)


def test_compute_ledger_hash_depends_on_prev_hash():
    row = {"ruling_id": "C-0001", "ts": "T", "summary": "x", "status": "active"}
    h1 = compute_ledger_hash(GENESIS_HASH, row)
    h2 = compute_ledger_hash("a" * 64, row)
    assert h1 != h2
    assert len(h1) == 64
    # deterministic
    assert compute_ledger_hash(GENESIS_HASH, row) == h1


def test_verify_chain_on_empty_ledger_is_ok(store):
    result = verify_chain(store.ops)
    assert result.ok is True
    assert result.checked == 0
    assert result.recomputed_head == GENESIS_HASH
    assert result.offenders == []


def test_verify_chain_ok_over_a_correctly_chained_sequence(store):
    h1 = _insert_chained(store, "C-0001", now(), "first", GENESIS_HASH)
    h2 = _insert_chained(store, "C-0002", now(), "second", h1)
    h3 = _insert_chained(store, "C-0003", now(), "third", h2)

    result = verify_chain(store.ops)
    assert result.ok is True
    assert result.checked == 3
    assert result.recomputed_head == h3
    assert result.offenders == []


def test_verify_chain_catches_a_single_tampered_row_and_cascades_downstream(store):
    h1 = _insert_chained(store, "C-0001", now(), "first", GENESIS_HASH)
    h2 = _insert_chained(store, "C-0002", now(), "second", h1)
    _insert_chained(store, "C-0003", now(), "third", h2)

    # Tamper: rewrite ruling 2's summary directly (bypasses the ledger API
    # entirely -- simulates hand-editing the DB, or a legacy import).
    with store.ops:
        store.ops.execute("UPDATE ruling SET summary = ? WHERE ruling_id = ?", ("REWRITTEN", "C-0002"))

    result = verify_chain(store.ops)
    assert result.ok is False
    assert result.first_break_ruling_id == "C-0002"
    # Cascades: C-0003 was chained against C-0002's TRUE hash, which no
    # longer matches what a fresh walk recomputes once C-0002 is broken.
    assert set(result.offenders) == {"C-0002", "C-0003"}
    assert "C-0001" not in result.offenders


def test_verify_chain_catches_a_forged_hash_with_unchanged_content(store):
    _insert_chained(store, "C-0001", now(), "first", GENESIS_HASH)
    with store.ops:
        store.ops.execute(
            "UPDATE ruling SET ledger_sha256_after = ? WHERE ruling_id = ?", ("f" * 64, "C-0001")
        )
    result = verify_chain(store.ops)
    assert result.ok is False
    assert result.first_break_ruling_id == "C-0001"
