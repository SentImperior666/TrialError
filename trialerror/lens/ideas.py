"""The idea content pipeline. Design Section 4.1 ``idea`` table (M1-landed
DDL, ``trialerror/stores/schema/ops.py``): "schema-now, writers-later landing
zone" — this module is that writer. Build brief: "the C-0047-style
full-text feed posting rides M5 (``post_feed`` binds authorship — your
tooling just supplies the content pipeline)": :func:`write_idea` creates the
``idea`` row; :func:`link_idea_to_feed_post` back-fills ``feed_post_ref``
once a caller (a skill, or the orchestrator) has posted the idea's full
text via ``trialerror.events.api.post_feed`` — this module never calls
``post_feed`` itself (that would put authorship-binding logic in two
places; M5's module docstring is explicit that it is the ONE place that
contract is enforced).

TRIALERROR-DEV-NOTE (idea-schema field gap — RESOLVED by schema-v2,
build-v1-schemav2): the build brief names ``home``/``assumed_circle``/
``provenance``/``tier``/``set_distance`` as fields the design's idea schema
carries. The M1-landed ``idea`` DDL had no such columns — only
``idea_id | round_id | author_launch | body | slice_ref | feed_post_ref |
status | created_ts`` — so this module used to pack them as a JSON object
into ``idea.slice_ref`` instead (docs/INTEGRATION_NOTES.md item 14;
docs/the migration-plan notes (internal, not in this export) Section 4 item 3). The
``knowledge_v2_idea_promoted_columns`` migration
(``trialerror/stores/schema/knowledge.py``) adds all five as real columns;
:func:`write_idea` below now writes them there directly. ``slice_ref`` is
kept populated for ONE version, via the exact same :func:`build_slice_ref`
JSON convention as before (now explicitly DEPRECATED — see its own
docstring) so any caller still reading ``idea.slice_ref`` does not break;
a future version may drop that write once nothing depends on it.
``assign_id``/``arm``/``distance_score``/``cluster_id`` were never named in
the promoted-columns list (INTEGRATION_NOTES item 14 names only the five
above), so they remain slice_ref-only.
"""

from __future__ import annotations

import json
from typing import Any

from trialerror.stores import insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["build_slice_ref", "write_idea", "link_idea_to_feed_post"]

#: ``idea.status`` CHECK constraint (design Section 4.1), transcribed for
#: caller-side validation before the DB round-trip.
IDEA_STATUSES: tuple[str, ...] = ("raw", "consolidated", "promoted")

#: ``idea.tier`` CHECK constraint (schema-v2), transcribed for caller-side
#: validation before the DB round-trip -- the same near/moderate/far
#: vocabulary ``trialerror.lens.stratify``/``trialerror.lens.assign`` already use.
IDEA_TIERS: tuple[str, ...] = ("near", "moderate", "far")


def build_slice_ref(
    *,
    assign_id: str | None = None,
    arm: str | None = None,
    distance_score: float | None = None,
    cluster_id: str | None = None,
    home: str | None = None,
    assumed_circle: str | None = None,
    tier: str | None = None,
    set_distance: float | None = None,
    provenance: Any = None,
) -> str:
    """Build the JSON convention this module packs into ``idea.slice_ref``.

    DEPRECATED as of schema-v2 for ``home``/``assumed_circle``/``tier``/
    ``set_distance``/``provenance`` — those five are now real ``idea``
    columns (see module docstring); :func:`write_idea` writes them there
    directly and calls this function only to keep ``slice_ref`` populated
    for backward compat during the deprecation window. ``assign_id``/
    ``arm``/``distance_score``/``cluster_id`` were never promoted and remain
    slice_ref-only — this function is still the right (only) way to carry
    those. Every field is optional and omitted from the JSON object when
    ``None`` (a lens output with no assignment behind it — a freeform idea —
    still gets a valid, if mostly-empty, slice_ref)."""
    obj: dict[str, Any] = {}
    for key, value in (
        ("assign_id", assign_id),
        ("arm", arm),
        ("distance_score", distance_score),
        ("cluster_id", cluster_id),
        ("home", home),
        ("assumed_circle", assumed_circle),
        ("tier", tier),
        ("set_distance", set_distance),
        ("provenance", provenance),
    ):
        if value is not None:
            obj[key] = value
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _text_or_json(value: Any) -> str | None:
    """``assumed_circle``/``provenance`` are documented ``TEXT/json``
    columns (docs/the migration-plan notes (internal, not in this export) Section 4 item 3): a caller-supplied
    plain string is stored verbatim (never re-quoted into a JSON string
    literal); anything else (dict/list/number/bool) is JSON-encoded. ``None``
    stays ``None`` (column left unset, not the literal string ``"null"``)."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def write_idea(
    store: Store,
    *,
    round_id: str | None,
    author_launch: str,
    body: str,
    home: str | None = None,
    assumed_circle: str | None = None,
    provenance: Any = None,
    tier: str | None = None,
    set_distance: float | None = None,
    slice_ref: str | None = None,
    status: str = "raw",
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Write one ``idea`` row. ``body`` is FULL TEXT (design: never a
    summary — the same C-0047 rule ``trialerror.events.api.post_feed`` enforces
    for feed posts, carried into this module's own docstring since the
    idea's body is exactly what a later ``post_feed`` call will post
    verbatim).

    ``home``/``assumed_circle``/``provenance``/``tier``/``set_distance`` are
    the schema-v2 promoted columns (docs/INTEGRATION_NOTES.md item 14) —
    written directly, not just packed into JSON. ``slice_ref`` remains for
    ``assign_id``/``arm``/``distance_score``/``cluster_id`` (never
    promoted — still JSON-only, via :func:`build_slice_ref`) and for
    backward compat: pass an already-built ``slice_ref`` (e.g. one carrying
    those four) and this function merges the five promoted fields' values
    into it too before writing, so a caller still reading ``idea.slice_ref``
    directly (the pre-schema-v2 convention) keeps seeing them. Omit
    ``slice_ref`` and this function builds one from just the promoted
    fields (``None`` if none were given — a plain freeform idea, unchanged
    from pre-schema-v2 behavior)."""
    if status not in IDEA_STATUSES:
        raise ValueError(f"write_idea: status must be one of {IDEA_STATUSES!r}, got {status!r}")
    if tier is not None and tier not in IDEA_TIERS:
        raise ValueError(f"write_idea: tier must be one of {IDEA_TIERS!r}, got {tier!r}")

    promoted = {"home": home, "assumed_circle": assumed_circle, "tier": tier, "set_distance": set_distance, "provenance": provenance}
    if slice_ref is not None:
        merged = json.loads(slice_ref)
        merged.update({k: v for k, v in promoted.items() if v is not None})
        effective_slice_ref: str | None = json.dumps(merged, sort_keys=True, ensure_ascii=False)
    else:
        built = build_slice_ref(**promoted)
        effective_slice_ref = built if built != "{}" else None

    row = {
        "idea_id": new_id("IDEA"),
        "round_id": round_id,
        "author_launch": author_launch,
        "body": body,
        "home": home,
        "assumed_circle": _text_or_json(assumed_circle),
        "provenance": _text_or_json(provenance),
        "tier": tier,
        "set_distance": set_distance,
        "slice_ref": effective_slice_ref,
        "feed_post_ref": None,
        "status": status,
        "created_ts": now_ts or now(),
    }
    return insert(store, "idea", row)


def link_idea_to_feed_post(store: Store, *, idea_id: str, feed_post_ref: str) -> None:
    """Back-fill ``idea.feed_post_ref`` after the caller has posted the
    idea's ``body`` via ``trialerror.events.api.post_feed`` (this module does
    not call ``post_feed`` itself — see module docstring). XID-validated
    against ``ops.feed_post`` by the same write API every other module
    uses (``trialerror.stores.xid``'s ``("idea", "feed_post_ref")`` entry)."""
    update(store, "idea", pk_column="idea_id", pk_value=idea_id, changes={"feed_post_ref": feed_post_ref})
