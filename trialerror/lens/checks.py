"""M13's doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every other
subsystem's ``checks.py`` (design Section 5.2 doctor row) — dropping this
file is the entire registration step, no shared file touched.

Build brief: "doctor checks in ``trialerror/lens/checks.py`` (e.g.
assignment-coverage invariants: every cluster in >=N sets, far-arm floor
honored, no duplicate sets)." Three checks, all scoped to
``DoctorContext.program_root`` (ops.db is per-program), all reading
``lens_assignment`` joined back to ``lens_roster`` for ``round_id``:

- ``far_arm_floor_honored``: every lens's logged far-arm count must meet
  the ``far_floor`` value its own rows carry — the write-time invariant
  ``trialerror.lens.assign.run_assignment`` enforces via
  ``trialerror.lens.quota.draw_quota``, re-checked here the same way
  ``trialerror.artifacts.checks``'s trio re-check THEIR write-time invariants:
  only reachable via a direct write bypassing the validated assignment
  path.
- ``no_duplicate_slice``: no candidate (``slice_spec.candidate_id``) is
  assigned to more than one lens within the same round — the "no
  duplicate sets" coverage invariant; the seeded draw's shrinking-pool
  construction makes this structurally impossible through
  ``run_assignment`` itself, so a violation here is a direct-write
  adversarial signal.
- ``cluster_coverage``: for rounds whose assignments carry a
  ``cluster_id`` (i.e. the round used ``cluster_of``), every distinct
  cluster referenced by that round's assignment rows appears in at least
  ``min_sets`` (default 1) of them — a WARN (not a FAIL) by default, since
  "under-represented cluster" is a data-shape observation, not a
  structural violation the write API could have refused. Rounds with no
  cluster labels at all are skipped, not flagged.

Two more arrived with the roster-level assignment mode (ops schema-v9):

- ``far_lens_floor_honored``: under ``arm_mode='per_lens'`` the far arm is
  a minority arm of LENSES, not of slices, so the floor that matters is
  "at least N lenses sit in the far arm". The slice-level sibling above
  structurally cannot see that — a round can satisfy every lens's own
  ``far_floor`` and still have put nobody in the far arm. Rounds written
  under ``per_slice`` (or before the mode existed) are skipped, never
  judged against a floor they did not claim.
- ``recipe_rotation_honored``: the card block is a within-lens block
  design, and it only does its job when all FOUR of its rules hold — every
  standard lens writes under exactly two cards, every card in play is held
  by at least two standard lenses, a round draws at most four distinct
  cards, and NEGATE is the assumption-buster's card and nobody else's.
  Holders are counted over STANDARD seats only (the buster's card is its
  seat, held by one lens by design; a control seat holds none at all), but
  the four-card ceiling counts every non-NEGATE card wherever it sits, so
  a fifth card cannot hide on a seat the holder bar excludes.

A sixth arrived with the framework's inventory reference set:

- ``lens_citations_within_slice``: a lens is supposed to read its own
  assigned slice and nothing else. The per-launch retrieval scope
  (``trialerror.retrieve.engine.launch_slice_doc_ids``) enforces that for
  every launch whose booking DECLARES its slice; this check covers the
  rest -- a launch booked without ``attrs.slice_doc_ids``, a post written
  before the scope existed, or an id pasted in from somewhere the
  retrieval tools never went. It reads each lens post's own text,
  resolves every document/chunk/anchor id in it back to a document, and
  asks whether that document was in the posting lens's slice.

Three more arrived with the framework's round procedure:

- ``idea_missing_dossier``: an idea reaches ``consolidated`` by passing
  through the screen, and the screen writes one dossier per idea. A
  consolidated row with no dossier on file is a row whose status asserts a
  measurement nothing can read back.
- ``round_collapse_flag_unacknowledged``: a raised collapse flag that nobody
  ruled on is what makes the collapse monitor decorative — the batch record
  says the round narrowed, the round closed anyway, and the pre-registered
  contingency never fired.
- ``lens_brief_contains_verdict_text``: a generator that can see a dossier
  label, a room verdict or a scoring rubric writes toward it. The textual
  rule is design §3.4's; this is the audit that makes it checkable.

Any DB file that doesn't exist yet, or a program with no lens_assignment
rows at all, is reported ``skip`` — not a doctor failure (same convention
``trialerror.artifacts.checks`` uses).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict

from trialerror.lens.roster import BUSTER_ONLY_CARD, roster_cards
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_far_arm_floor_honored",
    "check_no_duplicate_slice",
    "check_cluster_coverage",
    "check_far_lens_floor_honored",
    "check_recipe_rotation_honored",
    "check_lens_citations_within_slice",
    "check_idea_missing_dossier",
    "check_round_collapse_flag_unacknowledged",
    "check_lens_brief_contains_verdict_text",
]

#: Default floor for the WARN-only cluster-coverage check (build brief:
#: "every cluster in >=N sets"; N is not pinned by the design, so this
#: module's default is 1 — "referenced at least once" — the loosest
#: reading that is still a real check, callers wanting AMENDMENT-3's
#: stricter bar pass a larger ``min_sets`` via a future CLI flag).
DEFAULT_MIN_SETS = 1

#: ``recipe_rotation_honored``'s four bars (design Section 3.2 / the
#: charter amendment's card-block item): every card in play is held by at
#: least :data:`MIN_LENSES_PER_CARD` standard lenses, a round draws at most
#: :data:`MAX_CARDS_PER_ROUND` distinct cards, every standard lens writes
#: under exactly :data:`CARDS_PER_STANDARD_LENS` of them in its seeded
#: order, and :data:`BUSTER_ONLY_CARD` sits on the assumption-buster seat
#: alone.
#: (:data:`~trialerror.lens.roster.BUSTER_ONLY_CARD` is re-exported from
#: :mod:`trialerror.lens.roster`, where the write API refuses the same
#: pairing, so audit and writer cannot drift apart.)
MIN_LENSES_PER_CARD = 2
MAX_CARDS_PER_ROUND = 4
CARDS_PER_STANDARD_LENS = 2

#: ``far_lens_floor_honored``'s HARD floor: the amendment states the far
#: arm as a floor of two AGENTS, so a round that declared a smaller
#: ``far_lens_floor`` is judged against this, not against its own
#: declaration (verification finding V-2). A round may ask for MORE far
#: lenses than the amendment does; it may not ask for fewer. The per-slice
#: sibling ``far_arm_floor_honored`` keeps its round-declared bar, because
#: the slice-level floor is a harness parameter and not the amendment's.
MIN_FAR_LENSES = 2


def _ops_conn_or_none(ctx: DoctorContext) -> sqlite3.Connection | None:
    if ctx.program_root is None:
        return None
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


def _has_arm_mode_columns(conn: sqlite3.Connection) -> bool:
    """Whether this ops.db has been migrated to schema-v9 yet.

    Doctor reads the DB read-only and never migrates it, so a program whose
    ops.db is still at v8 would otherwise make the two checks below raise
    "no such column" — which the doctor framework catches and reports as a
    FAIL. That would be a false alarm about the wrong thing: the finding
    there is "this store is behind", which ``store_schema_version`` already
    reports precisely. These two skip instead."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(lens_assignment)").fetchall()}
    return "arm_mode" in columns


def _arm_mode_rows(conn: sqlite3.Connection) -> list[dict]:
    """One row per assignment, carrying the schema-v9 mode columns the two
    roster-level checks read. Kept separate from :func:`_assignment_rows` so
    the three original checks keep their own narrow column list."""
    rows = conn.execute(
        """
        SELECT a.assign_id, a.roster_id, a.arm, a.arm_mode, a.far_lens_floor,
               a.recipe_cards, r.round_id, r.seat, r.lens_name
        FROM lens_assignment a
        JOIN lens_roster r ON a.roster_id = r.roster_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _assignment_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.assign_id, a.roster_id, a.slice_spec, a.arm, a.far_floor, r.round_id
        FROM lens_assignment a
        JOIN lens_roster r ON a.roster_id = r.roster_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


@register_check("far_arm_floor_honored", category="lens")
def check_far_arm_floor_honored(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="far_arm_floor_honored", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = _assignment_rows(conn)
        if not rows:
            return CheckResult(
                name="far_arm_floor_honored", category="lens", status="skip",
                message="no lens_assignment rows on file",
            )
        by_lens: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_lens[row["roster_id"]].append(row)

        offenders = []
        for roster_id, lens_rows in by_lens.items():
            far_count = sum(1 for r in lens_rows if r["arm"] == "far")
            # The far_floor a lens's rows carry is written uniformly by one
            # run_assignment call; take the most recently-seen value as the
            # applicable floor (see module docstring).
            required_floor = lens_rows[-1]["far_floor"]
            if far_count < required_floor:
                offenders.append(
                    {"roster_id": roster_id, "far_count": far_count, "far_floor": required_floor}
                )

        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} lens(es) with fewer far-arm assignments than their far_floor"
            if offenders
            else "every lens's far-arm assignment count meets its far_floor"
        )
        return CheckResult(
            name="far_arm_floor_honored", category="lens", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


@register_check("no_duplicate_slice", category="lens")
def check_no_duplicate_slice(ctx: DoctorContext) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="no_duplicate_slice", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = _assignment_rows(conn)
        if not rows:
            return CheckResult(
                name="no_duplicate_slice", category="lens", status="skip",
                message="no lens_assignment rows on file",
            )
        seen: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in rows:
            spec = json.loads(row["slice_spec"])
            candidate_id = spec.get("candidate_id")
            if candidate_id is None:
                continue
            seen[(row["round_id"], candidate_id)].append(row["assign_id"])

        offenders = [
            {"round_id": round_id, "candidate_id": candidate_id, "assign_ids": assign_ids}
            for (round_id, candidate_id), assign_ids in seen.items()
            if len(assign_ids) > 1
        ]
        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} candidate(s) assigned to more than one lens within the same round"
            if offenders
            else "no candidate is assigned to more than one lens within any round"
        )
        return CheckResult(
            name="no_duplicate_slice", category="lens", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


@register_check("cluster_coverage", category="lens")
def check_cluster_coverage(ctx: DoctorContext, *, min_sets: int = DEFAULT_MIN_SETS) -> CheckResult:
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="cluster_coverage", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = _assignment_rows(conn)
        if not rows:
            return CheckResult(
                name="cluster_coverage", category="lens", status="skip",
                message="no lens_assignment rows on file",
            )
        by_round_cluster: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        any_cluster_labeled = False
        for row in rows:
            spec = json.loads(row["slice_spec"])
            cluster_id = spec.get("cluster_id")
            if cluster_id is None:
                continue
            any_cluster_labeled = True
            by_round_cluster[row["round_id"]][cluster_id] += 1

        if not any_cluster_labeled:
            return CheckResult(
                name="cluster_coverage", category="lens", status="skip",
                message="no round's assignments carry cluster_id labels (cluster_of not used)",
            )

        under_covered = [
            {"round_id": round_id, "cluster_id": cluster_id, "set_count": count, "min_sets": min_sets}
            for round_id, clusters in by_round_cluster.items()
            for cluster_id, count in clusters.items()
            if count < min_sets
        ]
        status = "warn" if under_covered else "pass"
        message = (
            f"{len(under_covered)} cluster(s) referenced in fewer than {min_sets} assignment set(s)"
            if under_covered
            else f"every referenced cluster appears in >= {min_sets} assignment set(s)"
        )
        return CheckResult(
            name="cluster_coverage", category="lens", status=status, message=message,
            details={"under_covered": under_covered},
        )
    finally:
        conn.close()


@register_check("far_lens_floor_honored", category="lens")
def check_far_lens_floor_honored(ctx: DoctorContext) -> CheckResult:
    """Under ``arm_mode='per_lens'``, every round must seat at least
    ``max(far_lens_floor, MIN_FAR_LENSES)`` LENSES in the far arm. This is
    the floor that keeps the far arm a real, measured minority arm rather
    than a label: a round can satisfy every lens's own slice-level
    ``far_floor`` and still have nobody reading far, and the per-slice
    check above cannot tell the difference.

    The bar is the HARDER of the round's own declaration and
    :data:`MIN_FAR_LENSES` (finding V-2). Reading the declared value alone
    made the check enforce whatever the round asked for, so
    ``--far-floor 1`` bought a passing round with one far lens — the
    amendment's floor of two agents is not a default a round gets to lower,
    and a check that lets the audited party set its own bar is not an
    audit. Asking for MORE far lenses is still honored.

    Only ``per_lens`` rounds are judged. A ``per_slice`` round mixes the
    arms inside each lens and has no per-lens arm to count, so it is
    excluded rather than failed for not honoring a rule it never claimed —
    which is also what keeps the documented interim (an expansion round run
    on the per-slice mix, declared as such in its pre-registration) out of
    this check's way.
    """
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="far_lens_floor_honored", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        if not _has_arm_mode_columns(conn):
            return CheckResult(
                name="far_lens_floor_honored", category="lens", status="skip",
                message="ops.db predates the assignment-mode columns (schema-v9); "
                        "store_schema_version reports a store that is behind",
            )
        rows = [r for r in _arm_mode_rows(conn) if r["arm_mode"] == "per_lens"]
        if not rows:
            return CheckResult(
                name="far_lens_floor_honored", category="lens", status="skip",
                message="no round on file was assigned with arm_mode='per_lens'",
            )

        far_lenses: dict[str, set[str]] = defaultdict(set)
        all_lenses: dict[str, set[str]] = defaultdict(set)
        floors: dict[str, int] = {}
        for row in rows:
            round_id = row["round_id"]
            all_lenses[round_id].add(row["roster_id"])
            if row["arm"] == "far":
                far_lenses[round_id].add(row["roster_id"])
            floor = row["far_lens_floor"]
            declared = int(floor) if floor is not None else MIN_FAR_LENSES
            floors[round_id] = max(declared, MIN_FAR_LENSES)

        offenders = [
            {
                "round_id": round_id,
                "far_lens_count": len(far_lenses[round_id]),
                "far_lens_floor": floors[round_id],
                "hard_floor": MIN_FAR_LENSES,
                "roster_size": len(lenses),
            }
            for round_id, lenses in sorted(all_lenses.items())
            if len(far_lenses[round_id]) < floors[round_id]
        ]
        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} per-lens round(s) with fewer far-arm LENSES than their floor "
            f"(the harder of the round's own far_lens_floor and the hard floor of {MIN_FAR_LENSES})"
            if offenders
            else f"every per-lens round seats at least {MIN_FAR_LENSES} lenses (and its own "
                 f"far_lens_floor) in the far arm ({len(all_lenses)} round(s) checked)"
        )
        return CheckResult(
            name="far_lens_floor_honored", category="lens", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


@register_check("recipe_rotation_honored", category="lens")
def check_recipe_rotation_honored(ctx: DoctorContext) -> CheckResult:
    """The four rules of the card block, over one round:

    1. every card in play is held by at least :data:`MIN_LENSES_PER_CARD`
       standard lenses;
    2. no round draws more than :data:`MAX_CARDS_PER_ROUND` distinct cards;
    3. every standard lens writes under exactly
       :data:`CARDS_PER_STANDARD_LENS` cards;
    4. :data:`BUSTER_ONLY_CARD` sits on the assumption-buster seat alone.

    They exist for one reason between them: cards are a within-lens block
    design, and a block that is not the same size everywhere, or a card
    held by ONE lens, is a card whose effect cannot be separated from that
    lens's vantage, slice and seed -- reporting it as a card effect would
    be reporting a confound. The four-card ceiling is the exposure budget a
    lens can actually write under in a round; NEGATE is the buster's stake
    and means nothing on a lens that has no stake.

    Rules 1 and 3 count STANDARD seats only. The assumption-buster's card
    comes with its seat and is held by one lens by design; a control seat
    holds none, which is what makes it the control. Rule 2 counts every
    non-NEGATE card wherever it sits, so a fifth card parked on a seat the
    other rules exclude cannot hide from the ceiling (finding V-3).

    Rounds where no lens carries a card at all are skipped, not flagged --
    a round that predates card blocks is not a round that broke the
    rotation rule.
    """
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="recipe_rotation_honored", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        if not _has_arm_mode_columns(conn):
            return CheckResult(
                name="recipe_rotation_honored", category="lens", status="skip",
                message="ops.db predates the recipe-card columns (schema-v9); "
                        "store_schema_version reports a store that is behind",
            )
        rows = _arm_mode_rows(conn)
        if not rows:
            return CheckResult(
                name="recipe_rotation_honored", category="lens", status="skip",
                message="no lens_assignment rows on file",
            )

        # roster_id -> (round_id, seat, cards); one entry per LENS, however
        # many assignment rows that lens has.
        lenses: dict[str, tuple[str, str, list[str]]] = {}
        for row in rows:
            lenses[row["roster_id"]] = (row["round_id"], row["seat"], roster_cards(row))

        # card -> the STANDARD lenses holding it (rules 1 and 3), and the
        # distinct non-NEGATE cards in play on ANY seat (rule 2).
        by_round: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        in_play: dict[str, set[str]] = defaultdict(set)
        blocks: dict[str, list[tuple[str, int]]] = defaultdict(list)
        misplaced: dict[str, list[str]] = defaultdict(list)
        rounds_with_cards: set[str] = set()
        for roster_id, (round_id, seat, cards) in lenses.items():
            if cards:
                rounds_with_cards.add(round_id)
            in_play[round_id].update(c for c in cards if c != BUSTER_ONLY_CARD)
            if BUSTER_ONLY_CARD in cards and seat != "assumption_buster":
                misplaced[round_id].append(roster_id)
            if seat != "standard":
                continue
            blocks[round_id].append((roster_id, len(cards)))
            for card in cards:
                by_round[round_id][card].add(roster_id)

        if not rounds_with_cards:
            return CheckResult(
                name="recipe_rotation_honored", category="lens", status="skip",
                message="no round on file carries recipe cards",
            )

        offenders = []
        for round_id in sorted(rounds_with_cards):
            cards = by_round.get(round_id, {})
            played = in_play.get(round_id, set())
            thin = sorted(
                c for c in played if len(cards.get(c, ())) < MIN_LENSES_PER_CARD
            )
            if thin:
                offenders.append(
                    {
                        "round_id": round_id, "reason": "card held by too few lenses",
                        "cards": thin, "min_lenses_per_card": MIN_LENSES_PER_CARD,
                        "holders": {c: len(cards.get(c, ())) for c in thin},
                    }
                )
            if len(played) > MAX_CARDS_PER_ROUND:
                offenders.append(
                    {
                        "round_id": round_id, "reason": "too many distinct cards in one round",
                        "cards": sorted(played), "max_cards_per_round": MAX_CARDS_PER_ROUND,
                    }
                )
            wrong_block = sorted(
                roster_id for roster_id, size in blocks.get(round_id, ())
                if size != CARDS_PER_STANDARD_LENS
            )
            if wrong_block:
                offenders.append(
                    {
                        "round_id": round_id, "reason": "standard lens not writing under a full block",
                        "lenses": wrong_block, "cards_per_standard_lens": CARDS_PER_STANDARD_LENS,
                        "block_sizes": {
                            roster_id: size for roster_id, size in blocks.get(round_id, ())
                            if size != CARDS_PER_STANDARD_LENS
                        },
                    }
                )
            if misplaced.get(round_id):
                offenders.append(
                    {
                        "round_id": round_id,
                        "reason": f"{BUSTER_ONLY_CARD} held by a seat that is not the assumption-buster",
                        "lenses": sorted(misplaced[round_id]), "card": BUSTER_ONLY_CARD,
                    }
                )

        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} recipe-rotation violation(s) across {len(rounds_with_cards)} carded round(s)"
            if offenders
            else f"every standard lens writes under {CARDS_PER_STANDARD_LENS} cards, every card in play "
                 f"is held by >= {MIN_LENSES_PER_CARD} of them, no round draws more than "
                 f"{MAX_CARDS_PER_ROUND} cards, and {BUSTER_ONLY_CARD} sits on the assumption-buster "
                 f"alone ({len(rounds_with_cards)} round(s) checked)"
        )
        return CheckResult(
            name="recipe_rotation_honored", category="lens", status=status, message=message,
            details={"offenders": offenders},
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# lens_citations_within_slice -- the audit behind the per-launch scope
# ---------------------------------------------------------------------------

#: Typed ids a lens post can cite that resolve to a document: the document
#: itself, one of its chunks, or one of its quote anchors. ULID bodies are
#: Crockford Base32 (no I, L, O or U), which is what keeps this pattern from
#: matching ordinary prose that happens to contain a hyphen.
_CITED_ID_RE = re.compile(r"\b(DOC|CHK|ANC)-([0-9A-HJKMNP-TV-Z]{26})\b")

#: ``launch.attrs`` keys that identify a launch as a LENS launch and name
#: what it was assigned. ``slice_doc_ids`` is the retrieval scope's own key
#: (and the most direct answer); the other two are what
#: ``trialerror.lens.export.export_launch_bookable`` puts on every bookable
#: row, and are how a launch booked before that key existed still resolves.
_SLICE_ATTR = "slice_doc_ids"
_ASSIGN_ATTRS = ("assign_ids", "roster_id")


def _launch_attrs(conn: sqlite3.Connection) -> dict[str, dict]:
    """``launch_id -> attrs dict`` for every launch that has readable
    attrs. A launch whose attrs will not decode simply carries none --
    a malformed JSON blob is not this check's finding to report."""
    out: dict[str, dict] = {}
    for row in conn.execute("SELECT launch_id, attrs FROM launch WHERE attrs IS NOT NULL").fetchall():
        try:
            attrs = json.loads(row["attrs"])
        except (TypeError, ValueError):
            continue
        if isinstance(attrs, dict):
            out[row["launch_id"]] = attrs
    return out


def _slice_docs_for_launch(
    ops: sqlite3.Connection, attrs: dict, *, launch_id: str | None = None
) -> set[str] | None:
    """The document ids this launch was assigned, or ``None`` when the
    launch is not a lens launch at all.

    Four sources, most direct first: the ``slice_doc_ids`` the retrieval
    scope itself reads; the ``assign_ids`` a bookable row carries; the
    ``roster_id`` it also carries, whose lens's whole logged slice is the
    answer; and the ``lens_assignment.lens_launch_id`` link
    ``budget book --assign-id`` writes from the assignment side (ops
    schema-v10). The fourth exists because the first three all live in
    ``launch.attrs``: a lens booked through the CLI carries none of them,
    resolved to ``None``, and this whole check SKIPped -- reporting on a
    barrier that never engaged for exactly the launches it exists for.

    A lens launch that resolves to an EMPTY slice is still a lens
    launch -- it returns an empty set, and every id it cites is outside it,
    which is the honest reading of "assigned nothing, cited something"."""
    raw_slice = attrs.get(_SLICE_ATTR)
    if isinstance(raw_slice, (list, tuple)):
        return {str(d) for d in raw_slice}

    assign_ids = attrs.get("assign_ids")
    if isinstance(assign_ids, (list, tuple)) and assign_ids:
        placeholders = ",".join("?" for _ in assign_ids)
        rows = ops.execute(
            f"SELECT slice_spec FROM lens_assignment WHERE assign_id IN ({placeholders})",
            [str(a) for a in assign_ids],
        ).fetchall()
        return _candidate_ids(rows)

    roster_id = attrs.get("roster_id")
    if roster_id:
        rows = ops.execute(
            "SELECT slice_spec FROM lens_assignment WHERE roster_id = ?", (str(roster_id),)
        ).fetchall()
        return _candidate_ids(rows)

    if launch_id:
        rows = ops.execute(
            "SELECT slice_spec FROM lens_assignment WHERE lens_launch_id = ?", (str(launch_id),)
        ).fetchall()
        if rows:
            return _candidate_ids(rows)

    return None


def _candidate_ids(rows) -> set[str]:
    out: set[str] = set()
    for row in rows:
        try:
            spec = json.loads(row["slice_spec"])
        except (TypeError, ValueError):
            continue
        candidate_id = spec.get("candidate_id") if isinstance(spec, dict) else None
        if candidate_id:
            out.add(str(candidate_id))
    return out


def _resolve_to_docs(knowledge: sqlite3.Connection | None, cited: set[tuple[str, str]]) -> tuple[dict[str, str], set[str]]:
    """``(cited id -> its doc_id, unresolvable cited ids)``.

    ``DOC-`` ids resolve to themselves ONLY if the document exists: an id
    that names no row in this corpus is not a citation of the slice, it is
    a citation of nothing, and the two are reported apart."""
    resolved: dict[str, str] = {}
    unresolved: set[str] = set()
    if knowledge is None:
        return resolved, {f"{prefix}-{body}" for prefix, body in cited}

    by_kind: dict[str, list[str]] = defaultdict(list)
    for prefix, body in cited:
        by_kind[prefix].append(f"{prefix}-{body}")

    lookups = {
        "DOC": ("SELECT doc_id AS id, doc_id AS resolved FROM document WHERE doc_id IN ({ph})",),
        "CHK": ("SELECT chunk_id AS id, doc_id AS resolved FROM chunk WHERE chunk_id IN ({ph})",),
        "ANC": ("SELECT anchor_id AS id, doc_id AS resolved FROM quote_anchor WHERE anchor_id IN ({ph})",),
    }
    for prefix, ids in by_kind.items():
        sql = lookups[prefix][0].format(ph=",".join("?" for _ in ids))
        found = {r["id"]: r["resolved"] for r in knowledge.execute(sql, ids).fetchall()}
        for cited_id in ids:
            if cited_id in found:
                resolved[cited_id] = found[cited_id]
            else:
                unresolved.add(cited_id)
    return resolved, unresolved


@register_check("lens_citations_within_slice", category="lens")
def check_lens_citations_within_slice(ctx: DoctorContext) -> CheckResult:
    """Every document/chunk/anchor id a lens post cites must resolve to a
    document in that lens's own assigned slice.

    Slice isolation is what makes a round's arms mean anything: a lens that
    reads outside its slice is not reporting from the vantage the assignment
    gave it, and every per-arm number computed afterwards is about a slice
    that lens did not actually hold. The retrieval engine enforces the scope
    for launches whose booking declares one; this check is the audit for
    everything that enforcement cannot see -- an undeclared booking, a post
    predating the scope, an id carried in from outside the tools.

    Two offender kinds, kept apart (the same split
    ``agent_model_matches_booking`` makes, for the same reason): an id that
    resolves to a document OUTSIDE the slice is a barrier that was crossed
    (**fail**); an id that resolves to no row in this corpus at all crossed
    nothing -- it names something this store has never held, which is a
    finding about the citation, not about the slice (**warn**). A run with
    both reports the crossing, and still counts the unresolvable ids in its
    message so they cannot hide behind the headline.

    Posts by launches that are not lens launches (no slice attrs and no
    assignment rows) are skipped, not judged: the orchestrator's own posts
    cite the whole corpus by design.
    """
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="lens_citations_within_slice", category="lens", status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    platform_path = paths.platform_db_path(root=ctx.platform_root)
    knowledge_path = paths.knowledge_db_path(ctx.program_root) if ctx.program_root is not None else None
    platform = knowledge = None
    try:
        if not platform_path.exists():
            return CheckResult(
                name="lens_citations_within_slice", category="lens", status="skip",
                message="platform.db not found (no lens launch has been booked yet)",
            )
        platform = connect(platform_path, read_only=True)
        attrs_by_launch = _launch_attrs(platform)

        posts = conn.execute(
            "SELECT post_id, launch_id, author, body FROM feed_post WHERE launch_id IS NOT NULL"
        ).fetchall()
        lens_posts = [
            (
                row,
                _slice_docs_for_launch(
                    conn, attrs_by_launch.get(row["launch_id"], {}), launch_id=row["launch_id"]
                ),
            )
            for row in posts
        ]
        lens_posts = [(row, slice_docs) for row, slice_docs in lens_posts if slice_docs is not None]
        if not lens_posts:
            n_assignments = conn.execute("SELECT COUNT(*) AS n FROM lens_assignment").fetchone()["n"]
            n_linked = conn.execute(
                "SELECT COUNT(*) AS n FROM lens_assignment WHERE lens_launch_id IS NOT NULL"
            ).fetchone()["n"]
            return CheckResult(
                name="lens_citations_within_slice", category="lens", status="skip",
                message=(
                    "no feed posts by a launch carrying a lens slice: of "
                    f"{n_assignments} assignment row(s), {n_linked} name a lens launch. A lens launch is "
                    "linked to its slice either by booking straight off `trialerror lens export` (its "
                    "attrs carry slice_doc_ids/assign_ids/roster_id) or by `trialerror budget book "
                    "--assign-id <id>`, which records lens_assignment.lens_launch_id. Without one of "
                    "those, this audit has no slice to judge a citation against"
                ),
            )

        if knowledge_path is not None and knowledge_path.exists():
            knowledge = connect(knowledge_path, read_only=True)

        offenders: list[dict] = []
        unresolved_rows: list[dict] = []
        cited_total = 0
        for row, slice_docs in lens_posts:
            cited = set(_CITED_ID_RE.findall(row["body"] or ""))
            if not cited:
                continue
            cited_total += len(cited)
            resolved, unresolved = _resolve_to_docs(knowledge, cited)
            outside = sorted(
                cited_id for cited_id, doc_id in resolved.items() if doc_id not in slice_docs
            )
            if outside:
                offenders.append(
                    {
                        "post_id": row["post_id"], "launch_id": row["launch_id"], "author": row["author"],
                        "cited_outside_slice": outside,
                        "resolved_docs": sorted({resolved[c] for c in outside}),
                        "slice_size": len(slice_docs),
                    }
                )
            if unresolved:
                unresolved_rows.append(
                    {
                        "post_id": row["post_id"], "launch_id": row["launch_id"],
                        "cited_unresolvable": sorted(unresolved),
                    }
                )

        checked = len(lens_posts)
        if offenders:
            status = "fail"
            message = (
                f"{len(offenders)} lens post(s) cite a document outside their own assigned slice"
                + (f"; {len(unresolved_rows)} post(s) also cite ids this corpus does not hold" if unresolved_rows else "")
            )
        elif unresolved_rows:
            status = "warn"
            message = (
                f"{len(unresolved_rows)} lens post(s) cite ids that resolve to no row in this corpus "
                "(nothing crossed a slice boundary -- these name nothing at all)"
            )
        else:
            status = "pass"
            message = (
                f"every cited id in {checked} lens post(s) resolves to a document in that lens's own "
                f"slice ({cited_total} citation(s) checked)"
            )
        return CheckResult(
            name="lens_citations_within_slice", category="lens", status=status, message=message,
            details={"offenders": offenders, "unresolvable": unresolved_rows, "posts_checked": checked},
        )
    finally:
        conn.close()
        if platform is not None:
            platform.close()
        if knowledge is not None:
            knowledge.close()


# ---------------------------------------------------------------------------
# Three more arrived with the framework's round procedure. All three read a
# round's own on-disk artifacts alongside the stores, because that is where
# the screen writes (``trialerror.lens.novelty.round_dir``) and doctor is the
# only reader that can notice a store row and a file disagreeing.
# ---------------------------------------------------------------------------

#: The two idea statuses that mean "this record went through the screen", and
#: therefore the two that owe a dossier. ``raw`` owes nothing yet; ``merged``
#: and ``eliminated`` were dispositioned rather than screened.
_DOSSIER_OWED_STATUSES: tuple[str, ...] = ("consolidated", "promoted")

#: Event types that ACKNOWLEDGE a collapse flag. Either one closes the
#: finding: an orchestrator that ruled on the flag and recorded the ruling,
#: or the pre-registered ``collapse_rerun`` contingency actually firing.
_COLLAPSE_ACK_EVENTS: tuple[str, ...] = ("round_collapse_acknowledged", "round_collapse_rerun")

#: Where a recorded lens BRIEF is read from. ``launch.attrs.brief`` is the
#: booking's own copy of what the lens was handed; a ``lens_brief`` event is
#: the orchestrator's record of the same thing for a brief that did not ride
#: on the booking. Both are read, because a brief the round never recorded
#: anywhere is a recording-discipline finding rather than a leak, and this
#: check says so instead of passing vacuously.
_BRIEF_ATTR_KEYS: tuple[str, ...] = ("brief", "prompt")
_BRIEF_EVENT_TYPE = "lens_brief"

#: What makes a lens launch an IDEATION booking -- the purpose
#: ``trialerror.lens.export.export_launch_bookable`` writes, or, for a booking
#: made by hand, the two attrs only an assignment can produce.
#:
#: The scope matters because a later phase books lenses too: a Phase 5
#: room-turn booking carries ``lens_name`` and its envelope legitimately
#: carries the dossier LABELS (the room barrier allows exactly those). Reading
#: its prompt as a generation brief would fail a correctly run round, which is
#: the same reason a critic's brief is excluded.
_BRIEF_PURPOSE = "ideation"
_BRIEF_ASSIGNMENT_ATTRS: tuple[str, ...] = ("roster_id", "slice_doc_ids")

#: Judge-output vocabulary that must never appear in a lens brief (design
#: §3.4: "no dossier, verdict or rubric text in any lens brief"). Every term
#: is specific to the screen's or the room's own output — the literature
#: labels (``stated``/``implied``/``adjacent``/``absent``) are deliberately
#: NOT here, because they are ordinary English words and a check that flagged
#: them would flag briefs that said nothing of the kind.
_VERDICT_MARKERS: tuple[str, ...] = (
    "label_inventory",
    "label_corpus",
    "new-mechanism",
    "no-close-neighbour",
    "known-mechanic",
    "unscreenable",
    "recombination",
    "dossier",
    "rubric",
    "agreement_pct",
    "meets-a",
    "meets-b",
    "meets-both",
    "d_prov",
    "h_prior",
)


def _round_dirs(ctx: DoctorContext) -> dict[str, "object"]:
    """``round_id -> the round's own artifact directory``, for every round
    directory that exists on disk. The path convention is
    :func:`trialerror.lens.novelty.round_dir`'s, imported lazily rather than
    re-derived here: the screen writes those paths and must stay the one
    place that decides them, but importing the screen at module scope would
    pull retrieval and verification into every doctor run."""
    from trialerror.lens.novelty import round_dir

    if ctx.program_root is None:
        return {}
    base = round_dir(ctx.program_root, "_").parent
    if not base.is_dir():
        return {}
    return {path.name: path for path in sorted(base.iterdir()) if path.is_dir()}


@register_check("idea_missing_dossier", category="lens")
def check_idea_missing_dossier(ctx: DoctorContext) -> CheckResult:
    """Every screened idea has a novelty dossier on file.

    An idea reaches ``consolidated`` by passing through the screen, and the
    screen writes one dossier per idea. A consolidated row with no dossier is
    therefore a row whose status asserts a measurement nobody can read: the
    reference sets it was compared against, the distances, the labels and the
    snapshot they were measured at are all in that file, and a round that
    rooms such an idea is rooming a record with no provenance for its own
    position. It is also the shape a hand-edited status leaves behind.

    Rounds with no directory on disk at all are not judged — a program whose
    screen has never run has no dossiers to miss, and "this round predates
    the screen" is a different statement from "this round lost a dossier"."""
    name = "idea_missing_dossier"
    if ctx.program_root is None:
        return CheckResult(name=name, category="lens", status="skip", message="program_root not configured")
    knowledge_path = paths.knowledge_db_path(ctx.program_root)
    if not knowledge_path.exists():
        return CheckResult(
            name=name, category="lens", status="skip",
            message="knowledge.db not found (program not yet initialized)",
        )
    dirs = _round_dirs(ctx)
    if not dirs:
        return CheckResult(
            name=name, category="lens", status="skip",
            message="no round directory on disk (the novelty screen has not run in this program)",
        )

    knowledge = connect(knowledge_path, read_only=True)
    try:
        placeholders = ",".join("?" for _ in _DOSSIER_OWED_STATUSES)
        rows = knowledge.execute(
            f"SELECT idea_id, round_id, status FROM idea WHERE round_id IS NOT NULL "
            f"AND status IN ({placeholders})",
            list(_DOSSIER_OWED_STATUSES),
        ).fetchall()
    finally:
        knowledge.close()

    checked = 0
    offenders: list[dict] = []
    for row in rows:
        round_path = dirs.get(str(row["round_id"]))
        if round_path is None:
            continue  # a round that never ran the screen here
        checked += 1
        if not (round_path / "novelty" / f"{row['idea_id']}.json").is_file():
            offenders.append({"idea_id": row["idea_id"], "round_id": row["round_id"], "status": row["status"]})

    if not checked:
        return CheckResult(
            name=name, category="lens", status="skip",
            message=f"no screened idea belongs to a round with a directory on disk ({len(dirs)} round dir(s) found)",
            details={"rounds": sorted(dirs)},
        )
    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} screened idea(s) carry no novelty dossier on file -- their status asserts a "
        "measurement nothing can read back"
        if offenders
        else f"all {checked} screened idea(s) have a dossier on file"
    )
    return CheckResult(
        name=name, category="lens", status=status, message=message,
        details={"offenders": offenders, "ideas_checked": checked},
    )


@register_check("round_collapse_flag_unacknowledged", category="lens")
def check_round_collapse_flag_unacknowledged(ctx: DoctorContext) -> CheckResult:
    """A raised collapse flag has been acknowledged.

    The collapse monitor is the framework's own early warning that a round's
    output is narrowing, and its whole point is the PRE-REGISTERED
    contingency it feeds. A flag that nobody ruled on is the failure mode
    that makes the monitor decorative: the batch record says the round
    collapsed, the round closed anyway, and the contingency that was escrowed
    at FRAME never fired. Either acknowledgement closes it —
    ``round_collapse_acknowledged`` (the orchestrator ruled and recorded the
    ruling) or ``round_collapse_rerun`` (the contingency actually fired) —
    matched on the batch, so acknowledging batch 0 does not silently cover
    batch 3.

    Batches whose collapse section is ``descriptive`` are not judged: with no
    pre-registered alarm values there is no flag to raise, which the screen
    states about itself rather than inventing a threshold."""
    name = "round_collapse_flag_unacknowledged"
    if ctx.program_root is None:
        return CheckResult(name=name, category="lens", status="skip", message="program_root not configured")
    dirs = _round_dirs(ctx)
    if not dirs:
        return CheckResult(
            name=name, category="lens", status="skip",
            message="no round directory on disk (the novelty screen has not run in this program)",
        )

    flagged: list[tuple[str, str]] = []
    armed = 0
    for round_id, round_path in dirs.items():
        batch_dir = round_path / "batches"
        if not batch_dir.is_dir():
            continue
        for batch_file in sorted(batch_dir.glob("*.json")):
            try:
                record = json.loads(batch_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            collapse = record.get("collapse") or {}
            if collapse.get("mode") != "armed":
                continue
            armed += 1
            if collapse.get("flag"):
                flagged.append((round_id, str(record.get("batch_id") or batch_file.stem)))

    if not armed:
        return CheckResult(
            name=name, category="lens", status="skip",
            message="no batch carries armed collapse alarms (alarm values are descriptive until a round's own "
                    "first batch sets them, so there is no flag to raise)",
        )
    if not flagged:
        return CheckResult(
            name=name, category="lens", status="pass",
            message=f"no collapse flag is raised across {armed} batch(es) with armed alarms",
            details={"batches_with_armed_alarms": armed},
        )

    conn = _ops_conn_or_none(ctx)
    acknowledged: set[tuple[str, str]] = set()
    if conn is not None:
        try:
            placeholders = ",".join("?" for _ in _COLLAPSE_ACK_EVENTS)
            rows = conn.execute(
                f"SELECT payload FROM event WHERE type IN ({placeholders})", list(_COLLAPSE_ACK_EVENTS)
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                round_id, batch_id = payload.get("round_id"), payload.get("batch_id")
                if round_id and batch_id:
                    acknowledged.add((str(round_id), str(batch_id)))
        finally:
            conn.close()

    offenders = [{"round_id": r, "batch_id": b} for r, b in flagged if (r, b) not in acknowledged]
    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} raised collapse flag(s) were never acknowledged -- record the ruling as a "
        f"{_COLLAPSE_ACK_EVENTS[0]!r} event naming the round and the batch, or fire the pre-registered "
        "contingency"
        if offenders
        else f"every raised collapse flag ({len(flagged)}) carries an acknowledgement"
    )
    return CheckResult(
        name=name, category="lens", status=status, message=message,
        details={"offenders": offenders, "flagged": [{"round_id": r, "batch_id": b} for r, b in flagged]},
    )


def _recorded_briefs(ctx: DoctorContext) -> list[dict]:
    """Every lens brief this program recorded, from both places one can be:
    a lens launch's own ``attrs`` and a ``lens_brief`` event. Each entry
    carries ``source``, ``ref`` and ``text`` so an offender names where to
    look."""
    briefs: list[dict] = []
    platform_path = paths.platform_db_path(root=ctx.platform_root)
    if platform_path.exists():
        platform = connect(platform_path, read_only=True)
        try:
            rows = platform.execute(
                "SELECT launch_id, agent_kind, purpose, attrs FROM launch WHERE attrs IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    attrs = json.loads(row["attrs"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(attrs, dict):
                    continue
                # A brief belongs to a LENS launch; a critic's or a
                # moderator's brief is supposed to carry verdict text.
                if str(row["agent_kind"]) != "lens" and not attrs.get("lens_name"):
                    continue
                # And to an IDEATION lens launch specifically: a Phase 5
                # room-turn booking is a lens booking whose envelope carries
                # the dossier labels by design.
                if str(row["purpose"] or "") != _BRIEF_PURPOSE and not any(
                    attrs.get(key) for key in _BRIEF_ASSIGNMENT_ATTRS
                ):
                    continue
                for key in _BRIEF_ATTR_KEYS:
                    text = attrs.get(key)
                    if isinstance(text, str) and text.strip():
                        briefs.append(
                            {
                                "source": f"launch.attrs.{key}",
                                "ref": row["launch_id"],
                                "lens_name": attrs.get("lens_name"),
                                "text": text,
                            }
                        )
        finally:
            platform.close()

    conn = _ops_conn_or_none(ctx)
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT event_id, payload FROM event WHERE type = ?", (_BRIEF_EVENT_TYPE,)
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                text = payload.get("text") or payload.get("brief")
                if isinstance(text, str) and text.strip():
                    briefs.append(
                        {
                            "source": f"event:{_BRIEF_EVENT_TYPE}",
                            "ref": row["event_id"],
                            "lens_name": payload.get("lens_name"),
                            "text": text,
                        }
                    )
        finally:
            conn.close()
    return briefs


@register_check("lens_brief_contains_verdict_text", category="lens")
def check_lens_brief_contains_verdict_text(ctx: DoctorContext) -> CheckResult:
    """No recorded lens brief carries judge output.

    A generator that can see a dossier label, a room verdict or a scoring
    rubric is a generator writing toward it, and every novelty number the
    round reports afterwards is then measuring the brief rather than the
    corpus. The textual rule (design §3.4) is that no lens brief contains
    dossier, verdict or rubric text; this is the audit that makes it
    checkable after the fact.

    Two readers, because a brief can be recorded in two places: the
    booking's own ``attrs`` (``brief``/``prompt``) and a ``lens_brief``
    event. Only IDEATION lens launches are read — the ones the lens export
    books at ``purpose='ideation'``, or that carry an assignment's own
    ``roster_id``/``slice_doc_ids``. Two exclusions, each for the same
    reason: a critic's brief is SUPPOSED to carry the pre-mortem and the
    rubric, and a Phase 5 ROOM-TURN booking is a lens booking whose envelope
    legitimately carries the dossier LABELS (the room barrier allows exactly
    those and nothing else). Flagging either would teach an operator to
    ignore this check. A program that records no lens brief anywhere is
    reported ``skip`` with both places named: nothing to audit is a statement
    about recording discipline, not a clean result."""
    name = "lens_brief_contains_verdict_text"
    briefs = _recorded_briefs(ctx)
    if not briefs:
        return CheckResult(
            name=name, category="lens", status="skip",
            message=(
                "no lens brief is recorded in this program -- nothing to audit. A brief is read from an "
                f"ideation lens launch's attrs ({'/'.join(_BRIEF_ATTR_KEYS)}) or from a "
                f"{_BRIEF_EVENT_TYPE!r} event; a critic's brief and a room-turn booking are out of scope, "
                "because both carry judge output by design"
            ),
        )
    offenders: list[dict] = []
    for brief in briefs:
        lowered = brief["text"].lower()
        found = sorted(marker for marker in _VERDICT_MARKERS if marker in lowered)
        if found:
            offenders.append(
                {"source": brief["source"], "ref": brief["ref"], "lens_name": brief["lens_name"], "markers": found}
            )
    status = "fail" if offenders else "pass"
    message = (
        f"{len(offenders)} recorded lens brief(s) carry judge output -- a generator that can see a dossier "
        "label, a verdict or a rubric writes toward it"
        if offenders
        else (
            f"none of {len(briefs)} recorded ideation lens brief(s) carries dossier, verdict or rubric text "
            "(a critic's brief and a room-turn booking are out of scope -- both carry judge output by design)"
        )
    )
    return CheckResult(
        name=name, category="lens", status=status, message=message,
        details={"offenders": offenders, "briefs_checked": len(briefs)},
    )
