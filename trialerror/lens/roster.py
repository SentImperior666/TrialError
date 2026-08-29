"""``lens_roster`` — the vantage axis (design Section 4.2/9.6: "roster
config (vantage axis)"). One row per lens; ``round_id`` is a free-form
grouping label (not a foreign key — same convention as ``event.workpackage``,
per ``trialerror.stores.xid``'s own "NON-member" note) shared by every lens
in one ideation round and by that round's ``lens_assignment``/``idea`` rows.
"""

from __future__ import annotations

from typing import Any

from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["SEATS", "add_lens", "list_roster"]

#: ``lens_roster.seat`` CHECK constraint (design Section 4.2), transcribed
#: here so callers/CLI validate before hitting the DB round-trip.
SEATS: tuple[str, ...] = ("standard", "assumption_buster")


def add_lens(
    store: Store,
    *,
    round_id: str,
    lens_name: str,
    vantage: str,
    model_class: str,
    seat: str = "standard",
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Add one lens to a round's roster. ``seat='assumption_buster'`` is
    "the assumption-buster seat as a roster role" (design Section 9.6 /
    C-0029) — a plain data value on this same table, not a separate
    mechanism."""
    if seat not in SEATS:
        raise ValueError(f"add_lens: seat must be one of {SEATS!r}, got {seat!r}")
    row = {
        "roster_id": new_id("ROST"),
        "round_id": round_id,
        "lens_name": lens_name,
        "vantage": vantage,
        "seat": seat,
        "model_class": model_class,
        "created_ts": now_ts or now(),
    }
    return insert(store, "lens_roster", row)


def list_roster(store: Store, *, round_id: str) -> list[dict[str, Any]]:
    """Every lens in ``round_id``'s roster, in the order they were added
    (``created_ts`` then ``rowid`` as the tiebreaker — same pattern
    ``trialerror.events.api._query_event_rows`` uses) — this is the order
    :mod:`trialerror.lens.assign` processes lenses in when a caller doesn't
    supply its own explicit order."""
    rows = store.ops.execute(
        "SELECT *, rowid AS _rowid FROM lens_roster WHERE round_id = ? ORDER BY created_ts ASC, _rowid ASC",
        (round_id,),
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]
