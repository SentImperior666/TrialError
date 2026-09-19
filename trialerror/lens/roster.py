"""``lens_roster`` — the vantage axis (design Section 4.2/9.6: "roster
config (vantage axis)"). One row per lens; ``round_id`` is a free-form
grouping label (not a foreign key — same convention as ``event.workpackage``,
per ``trialerror.stores.xid``'s own "NON-member" note) shared by every lens
in one ideation round and by that round's ``lens_assignment``/``idea`` rows.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "SEATS",
    "CONTROL_VANTAGE_PREFIX",
    "BUSTER_ONLY_CARD",
    "add_lens",
    "roster_cards",
    "list_roster",
]

#: ``lens_roster.seat`` CHECK constraint (design Section 4.2, widened by ops
#: schema-v9), transcribed here so callers/CLI validate before hitting the
#: DB round-trip.
#:
#: ``control`` is the matched-budget measurement seat: same slice size, same
#: arm as the modal lens, same budget, but the pre-framework brief — no
#: recipe card and no ``requirements`` field. It is what turns "the
#: framework improved ideation" into a measured comparison rather than an
#: assertion, and it sits OUTSIDE the roster counts a round's charter fixes
#: for standard and assumption-buster seats.
SEATS: tuple[str, ...] = ("standard", "assumption_buster", "control")

#: The pre-v9 spelling of the control seat: before ``seat='control'``
#: existed, a control lens was marked by this prefix on its ``vantage``
#: string (the ``CONTROL:no-recipe`` convention). Historical rows still
#: carry it; new rows use the seat value. Named here so a reader can
#: recognise the old convention instead of rediscovering it.
CONTROL_VANTAGE_PREFIX = "CONTROL:"

#: The one card in the catalogue that is a SEAT's card. Every other card is
#: drawn into a standard lens's block; this one comes with the
#: assumption-buster seat, because its instruction presumes the stake that
#: seat carries. Named here (rather than inside the doctor check that also
#: enforces it) so the write API and the audit read the same constant.
BUSTER_ONLY_CARD = "NEGATE"


def add_lens(
    store: Store,
    *,
    round_id: str,
    lens_name: str,
    vantage: str,
    model_class: str,
    seat: str = "standard",
    recipe_cards: Sequence[str] | None = None,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Add one lens to a round's roster. ``seat='assumption_buster'`` is
    "the assumption-buster seat as a roster role" (design Section 9.6 /
    C-0029) — a plain data value on this same table, not a separate
    mechanism; ``seat='control'`` (ops schema-v9) is the matched-budget
    measurement seat described on :data:`SEATS`.

    ``recipe_cards`` is this lens's card BLOCK: the card names it writes
    under, **in the seeded order it writes them in** — order is meaningful
    and is preserved verbatim, because a within-lens block design is what
    keeps card from being confounded with vantage, slice and seed. Stored
    as a JSON array.

    Two card/seat pairings are refused outright rather than recorded.

    A control seat takes no cards at all — that is precisely what makes it
    the control, and a round holding one would report a comparison it did
    not actually run.

    :data:`BUSTER_ONLY_CARD` belongs to the assumption-buster seat and to
    no other: it is the one card whose instruction presumes the stake that
    seat carries (attack a standing assumption, citing your own far slice),
    so on any other lens it is a card in name only, and the round would be
    reporting a dissent arm it never seated (finding V-3)."""
    if seat not in SEATS:
        raise ValueError(f"add_lens: seat must be one of {SEATS!r}, got {seat!r}")
    cards = [str(c) for c in recipe_cards] if recipe_cards else []
    if cards and seat == "control":
        raise ValueError(
            "add_lens: a control seat carries no recipe cards (that is what makes it the "
            f"control) - got {cards!r}; drop the cards, or use seat='standard'"
        )
    if BUSTER_ONLY_CARD in cards and seat != "assumption_buster":
        raise ValueError(
            f"add_lens: {BUSTER_ONLY_CARD} is the assumption-buster's card and no other seat's "
            f"(it presumes the stake that seat carries) - got seat={seat!r} with {cards!r}; "
            "use seat='assumption_buster', or draw a different card"
        )
    row = {
        "roster_id": new_id("ROST"),
        "round_id": round_id,
        "lens_name": lens_name,
        "vantage": vantage,
        "seat": seat,
        "model_class": model_class,
        "recipe_cards": json.dumps(cards, ensure_ascii=False) if cards else None,
        "created_ts": now_ts or now(),
    }
    return insert(store, "lens_roster", row)


def roster_cards(row: Mapping[str, Any]) -> list[str]:
    """The card block on a roster row, decoded, in seeded order — ``[]`` for
    a lens with no cards (a control seat, or a round that ran before cards
    existed). Never raises on a malformed value: an undecodable
    ``recipe_cards`` reads as no cards, the same way the rest of this
    package treats an absent optional field, so one bad row cannot take a
    doctor check or an export down with it."""
    raw = row.get("recipe_cards")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(c) for c in raw]
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(c) for c in decoded] if isinstance(decoded, list) else []


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
