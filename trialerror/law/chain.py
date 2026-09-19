"""The ruling ledger's tamper-evident hash chain. Design Section 4.2
(ruling row): "``ledger_sha256_after`` (hash chain over the append
sequence; semantica pattern)."

Each ``ruling`` row stores the sha256 of the chain *after* it was appended:
``hash_i = sha256(hash_{i-1} + canonical_repr(ruling_i))``, where
``hash_0`` is a fixed genesis value. Recomputing the chain from row 1 and
comparing each recomputed value against what the row actually stored is
how tampering (an edited ``summary``, a rewritten ``supersedes``, a row
deleted/reordered/inserted after the fact) is detected — the whole point
of a hash chain: changing anything in ruling *i* changes ``hash_i`` and
therefore every ``hash_j`` for ``j > i`` no longer matches what's stored.

Append order is the SQLite ``rowid`` (implicit even on a ``TEXT PRIMARY
KEY`` table that isn't declared ``WITHOUT ROWID`` — true here, see
``trialerror/stores/schema/ops.py``), not ``ts`` or ``ruling_id`` string sort:
the ledger is append-only and rowid is the one column that can never be
retroactively edited to reorder history without SQLite itself objecting.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["GENESIS_HASH", "canonical_ruling_repr", "compute_ledger_hash", "ChainVerifyResult", "verify_chain"]

#: The chain's fixed starting value ("hash before the first ruling ever
#: appended"). Deliberately not a real sha256 output (all-zero, same style
#: as the placeholder ``ledger_sha256_after`` value M1's
#: ``tests/_store_fixtures.py`` uses for its single manually-seeded row) --
#: unmistakably a sentinel, never a value a real append could produce.
GENESIS_HASH = "0" * 64

#: The exact fields (in this order) that participate in a ruling's chain
#: entry. ``status`` is deliberately included: a forged compensating
#: supersession status-flip on THIS row (as opposed to the row it
#: supersedes -- see below) would be caught. The row a NEW ruling
#: supersedes gets its status flipped to 'superseded' as a side effect of
#: append_ruling(); that side-effect mutation is intentionally NOT part of
#: any row's own chain entry (the chain protects the append log, not
#: arbitrary in-place mutations) -- it is instead reconstructable and
#: cross-checked at read time because the NEW ruling's own chained
#: ``supersedes`` field names the row it superseded.
_CHAIN_FIELDS = (
    "ruling_id",
    "ts",
    "verbatim_quote",
    "summary",
    "standing_clauses",
    "domains",
    "supersedes",
    "supersedes_note",
    "status",
)


def canonical_ruling_repr(row: Mapping[str, Any]) -> str:
    """Deterministic, whitespace-stable JSON serialization of the chain
    fields of one ruling row. Same input always produces the same string
    (``sort_keys`` is redundant given ``_CHAIN_FIELDS`` is already a fixed
    order, but cheap insurance against a future reordering)."""
    payload = {k: row.get(k) for k in _CHAIN_FIELDS}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_ledger_hash(prev_hash: str, ruling_row: Mapping[str, Any]) -> str:
    """``hash_i = sha256(hash_{i-1} + canonical_repr(ruling_i))``."""
    material = prev_hash + canonical_ruling_repr(ruling_row)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class ChainVerifyResult:
    ok: bool
    checked: int
    first_break_ruling_id: str | None = None
    detail: str = ""
    recomputed_head: str = GENESIS_HASH
    offenders: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "first_break_ruling_id": self.first_break_ruling_id,
            "detail": self.detail,
            "recomputed_head": self.recomputed_head,
            "offenders": list(self.offenders),
        }


def verify_chain(conn: sqlite3.Connection) -> ChainVerifyResult:
    """Recompute the hash chain over every ``ruling`` row (in rowid /
    append order) and compare each recomputed value against the row's
    stored ``ledger_sha256_after``. Returns ``ok=True`` only if every row
    matches; otherwise the FIRST offending ruling_id is reported (a
    tampered row invalidates every hash after it too, so listing only the
    first break is the actionable signal -- ``offenders`` still lists every
    row from that point on, for full visibility)."""
    rows = conn.execute("SELECT rowid, * FROM ruling ORDER BY rowid").fetchall()
    prev = GENESIS_HASH
    first_break: str | None = None
    offenders: list[str] = []
    for row in rows:
        row_d = dict(row)
        expected = compute_ledger_hash(prev, row_d)
        stored = row_d.get("ledger_sha256_after")
        if expected != stored:
            offenders.append(row_d["ruling_id"])
            if first_break is None:
                first_break = row_d["ruling_id"]
        # The chain continues from the RECOMPUTED (expected) value, not the
        # stored one: this is what makes a single tampered row cascade —
        # every row appended after it was chained against what its
        # predecessor's hash SHOULD be, so once one row's stored hash stops
        # matching, every later row's stored hash stops matching too
        # (unless the tamperer also rewrote the entire remaining chain to
        # match, the standard hash-chain property). One ``doctor`` pass
        # therefore surfaces the full blast radius, not just the first row.
        prev = expected

    ok = first_break is None
    detail = (
        f"chain intact over {len(rows)} ruling(s)"
        if ok
        else f"chain break at {first_break!r} ({len(offenders)} row(s) affected)"
    )
    return ChainVerifyResult(
        ok=ok,
        checked=len(rows),
        first_break_ruling_id=first_break,
        detail=detail,
        recomputed_head=prev,
        offenders=offenders,
    )
