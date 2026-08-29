"""Seeds this repo's own 12 built-in artifact templates (bundled under
``trialerror/artifacts/templates/*.md``) into a program's ``template`` table.

This module is fully standalone: a fresh v0 program with no other tooling
ever run has no rows in ``template`` until :func:`seed_builtin_templates`
(or ``trialerror artifact templates --seed``) is called; it makes no edits to
any other module and has no dependency beyond the bundled files it reads.

**Why this exists as a bundled port rather than a live read from an
external checkout:** an earlier design considered reading these files live
from an external tenant-migration tool's own template directory instead of
bundling a copy. This module takes the other path deliberately -- copy the
12 files in at build time, once, byte-exact -- specifically so a fresh
install never depends on an external checkout being present just to seed
its own template rows. A compatible external migration tool can still
import a matching set of rows against the same convention afterward; this
module's ``type_key`` values (title-cased filename stem) are chosen to
line up with that convention, so seeding from either source (or both, in
either order) lands on the same rows, idempotently -- proven by a test
against the shared convention table.

The bundled files under ``trialerror/artifacts/templates/*.md`` are the
templates themselves (see that directory for the full set): 11 canonical
ID-prefix-mapped stems plus a general ``methods-note`` template, 12 rows
total.
"""


from __future__ import annotations

from pathlib import Path
from typing import Any

from trialerror.stores import get as store_get
from trialerror.stores.store import Store
from trialerror.stores.writer import insert as store_insert

__all__ = [
    "TEMPLATES_DIR",
    "CANONICAL_PREFIXED_STEMS",
    "builtin_template_rows",
    "seed_builtin_templates",
    "list_builtin_templates",
]

#: The bundled, byte-exact port of origin-project's research/templates/*.md (READ-ONLY
#: source at C:\...\origin-project\research\templates\; nothing
#: in this module ever writes back to that repo).
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: The 11 canonical ID-prefix-mapped stems (the migration-plan notes (internal, not in this export) §2.3's
#: table: PR/SV/TD/PF/PB/MN/CR/AJ/SY/WP/PX -> these 11 filenames). Kept
#: here ONLY as a cross-check assertion target for tests -- the seed path
#: itself globs every ``*.md`` file present in TEMPLATES_DIR, so the 12th,
#: unprefixed file (``saturation-certificate``) is never silently dropped
#: just because it has no entry in this set.
CANONICAL_PREFIXED_STEMS = frozenset(
    {
        "protocol",
        "survey",
        "theory-draft",
        "proof-note",
        "probe-report",
        "methods-note",
        "critique",
        "adjudication",
        "synthesis",
        "white-paper",
        "paper-export",
    }
)


def builtin_template_rows() -> list[dict[str, Any]]:
    """One ``template``-shaped dict per bundled ``trialerror/artifacts/templates
    /*.md`` file. Field set and derivation are deliberately identical to
    ``the (excluded) tenant-migration module`` (title-cased stem, version
    "1.0", ``gated=0`` -- a migration run's ``the (excluded) tenant-migration module
    .commit`` is the only thing that ever flips ``gated`` to 1, via its D21
    permissive-gating pass) -- the convention is duplicated on purpose
    (this module reads a bundled dir, not an external --source-root checkout),
    never imported from that module directly."""
    rows: list[dict[str, Any]] = []
    for path in sorted(TEMPLATES_DIR.glob("*.md")):
        stem = path.stem
        rows.append(
            {
                "type_key": stem,
                "title": stem.replace("-", " ").title(),
                "version": "1.0",
                # Provenance string, not resolved at read time -- mirrors
                # the (excluded) tenant-migration module's own "path relative to the source repo root"
                # convention, just rooted at this repo instead of origin-project's.
                "path": f"trialerror/artifacts/templates/{path.name}",
                "gated": 0,
                "schema_ref": None,
            }
        )
    return rows


def seed_builtin_templates(store: Store) -> list[dict[str, Any]]:
    """Insert every bundled template not already present in ``template``
    (keyed by ``type_key``); a row that already exists -- whether seeded by
    a prior call to this function, or landed earlier by a real
    ``the (excluded) tenant-migration module``/``seed`` migration run -- is left
    completely untouched (idempotent, skip-if-exists, same pattern
    ``the (excluded) tenant-migration module`` uses). Returns the rows
    that were newly inserted by THIS call."""
    inserted: list[dict[str, Any]] = []
    for row in builtin_template_rows():
        existing = store_get(store, "template", pk_column="type_key", pk_value=row["type_key"])
        if existing is not None:
            continue
        store_insert(store, "template", row)
        inserted.append(row)
    return inserted


def list_builtin_templates(store: Store | None = None) -> list[dict[str, Any]]:
    """Bundled template metadata (independent of ``store``), each annotated
    with whether that ``type_key`` is already registered in ``store`` --
    informational listing for ``trialerror artifact templates`` that is useful
    both before and after ``--seed`` has ever run. Pass ``store=None`` for
    a store-free listing (every row comes back ``registered: False``)."""
    out: list[dict[str, Any]] = []
    for row in builtin_template_rows():
        registered = False
        gated_in_store: bool | None = None
        if store is not None:
            existing = store_get(store, "template", pk_column="type_key", pk_value=row["type_key"])
            if existing is not None:
                registered = True
                gated_in_store = bool(existing.get("gated"))
        out.append({**row, "registered": registered, "gated_in_store": gated_in_store})
    return out
