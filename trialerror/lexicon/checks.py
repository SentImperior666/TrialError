"""The lexicon's doctor checks. Design of record:
``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` Section 6, build step E3.

Nine checks, category ``lexicon``, auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` purely because this
file lives at ``trialerror/lexicon/checks.py`` -- the same directory
convention ``trialerror/feed_translate/checks.py`` and ``trialerror/events/
checks.py`` already document. Adding this file is the whole registration
step; no shared file is touched.

Every check reads ``knowledge.db`` directly with a fresh, read-only
``sqlite3.Connection`` (:func:`trialerror.stores.connection.connect`,
``read_only=True``) -- the same shape ``trialerror.feed_translate.checks``
uses, and for the same reason: a doctor check runs against whatever program
is on disk, including one whose store a write path has never opened, so it
must never itself trigger a migration or hold the store open.

**Three states, not two** (the parenthetical the E3 build brief names:
"ok / not_initialized / awaiting_migration"), and :func:`_open_knowledge`
is the one place that tells them apart:

1. ``not_initialized`` -- ``program_root`` isn't configured, or
   ``knowledge.db`` doesn't exist yet. Nothing has been scaffolded.
2. ``awaiting_migration`` -- the file exists, but the ``term`` table does
   not: this program's ``knowledge.db`` predates schema v5. Ruling L-E1's
   "any write path that opens this program's store picks up the migration
   automatically" means this is a real, ordinary, temporary state, not a
   broken one.
3. ``ok`` -- the tables are there; the check runs its real query.

Both non-``ok`` states report ``status="skip"`` (:class:`~trialerror.util.
doctor.CheckResult`'s status vocabulary is exactly ``pass|fail|warn|skip`` --
there is no ``not_initialized``/``awaiting_migration`` status value; those
are read from the message instead), distinguished only by message text --
matching the ``trialerror.feed_translate.checks._open_ops`` precedent this
module's ``_open_knowledge`` mirrors line for line.

**Fail vs. warn, restated from the design table.** Three of the nine are
``fail``: ``term_sense_without_evidence`` and ``term_split_missing_
disambiguator`` audit invariants ``trialerror.lexicon.api`` already enforces
on every write path (the grounding law; the split-head invariant) -- so a
positive count here means the invariant was routed around, not merely that
review is pending, and ``term_system_relation_decided`` is the MINING §5.3
constraint audited from the outside, the second of the two places the
design names for it. ``term_fts_in_sync`` fails on a count mismatch because
the index is API-maintained (:func:`trialerror.lexicon.api.reindex_term`)
-- drift is a bug, not a backlog. The other six are ``warn``: a standing
review queue (conflicts, duplicates, staleness, unprojected definition
claims, unlinked evidence sources) is the never-silent-auto-merge posture
doing its job, not damage.

Ruling L-E3, restated once here because it shapes what this module does
*not* do: there is no "unmapped family tag" check in this list. Instance
terms carry their tag codes from the register import regardless of whether
the level-1 family map has been supplied yet (the map is a separate,
later, orchestrator-supplied artifact -- Section 12), so nothing here can
observe "a tag with no family term" as anything other than the ordinary,
expected pre-map state; inventing a check that warned about it would be
warning about a condition ruling L-E3 already says is normal, which is
exactly the "unmapped tags -> WARN never FAIL" instruction turned into "no
check treats the unmapped, pre-map state as unhealthy at all."
"""

from __future__ import annotations

import sqlite3

from trialerror.lexicon.policy import LIVE_SENSE_STATUSES
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.util.timeutil import now

__all__ = [
    "check_term_conflicts_pending",
    "check_term_duplicates_pending",
    "check_term_senses_need_review",
    "check_term_sense_without_evidence",
    "check_term_split_missing_disambiguator",
    "check_term_evidence_source_unlinked",
    "check_term_fts_in_sync",
    "check_definition_claims_unprojected",
    "check_term_system_relation_decided",
]

_CATEGORY = "lexicon"


def _open_knowledge(ctx: DoctorContext, name: str) -> tuple[sqlite3.Connection | None, CheckResult | None]:
    """Resolve ``knowledge.db`` read-only, or hand back the ``skip`` result
    for whichever of the two absent-table states applies. See the module
    docstring's "three states" section -- this is the one place that tells
    them apart, mirroring ``trialerror.feed_translate.checks._open_ops``."""
    if ctx.program_root is None:
        return None, CheckResult(
            name=name, category=_CATEGORY, status="skip",
            message="program_root not configured; cannot resolve knowledge.db path",
        )
    path = paths.knowledge_db_path(ctx.program_root)
    if not path.exists():
        return None, CheckResult(
            name=name, category=_CATEGORY, status="skip",
            message="knowledge.db not found (program not yet initialized)",
        )
    conn = connect(path, read_only=True)
    if not _table_exists(conn, "term"):
        conn.close()
        return None, CheckResult(
            name=name, category=_CATEGORY, status="skip",
            message="term table not present (knowledge.db predates schema v5 -- awaiting migration; "
            "any write path that opens this program's store picks up the migration automatically)",
        )
    return conn, None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# 1. term_conflicts_pending -- warn
# ---------------------------------------------------------------------------


@register_check("term_conflicts_pending", category=_CATEGORY)
def check_term_conflicts_pending(ctx: DoctorContext) -> CheckResult:
    """Open ``conflicts_with`` candidates -- the review load the never-
    silent-auto-merge posture creates (design §6). Details carry the count
    and the oldest ``marked_ts`` so the operator sees how long the queue
    has been standing, not just its size."""
    name = "term_conflicts_pending"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT rel_id, marked_ts FROM term_relation "
            "WHERE verb = 'conflicts_with' AND status = 'pending' ORDER BY marked_ts, rel_id"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "warn" if count else "pass"
    message = (
        f"{count} pending term-sense conflict(s) awaiting a decision" if count
        else "no pending term-sense conflicts"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={
            "count": count,
            "rel_ids": [r["rel_id"] for r in rows],
            "oldest_marked_ts": rows[0]["marked_ts"] if rows else None,
        },
    )


# ---------------------------------------------------------------------------
# 2. term_duplicates_pending -- warn
# ---------------------------------------------------------------------------


@register_check("term_duplicates_pending", category=_CATEGORY)
def check_term_duplicates_pending(ctx: DoctorContext) -> CheckResult:
    """Open ``same_as`` candidates -- engram-F4 machine-surfaced duplicates
    awaiting a launch's verdict (design §6). ``variant_of`` is a decision a
    judge reaches, never a verb the scan itself opens, so this counts
    ``same_as`` only.

    The message splits the count into **system-opened** and
    **human-touched** (build step 1c, amending §6). The two halves are
    different work: the system-opened half is a machine's guesses, and
    ``trialerror term scan --rescan`` can take back the ones a recalibrated
    gate no longer stands behind -- which on a freshly imported corpus is
    most of them. The human-touched half -- a candidate somebody opened by
    hand, or one a launch has already reached into -- is nobody's to
    withdraw and stays exactly the size it is. An operator looking at a
    five-figure number needs to know which half of it a re-scan can move
    before deciding whether the queue is a crisis or a knob."""
    name = "term_duplicates_pending"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT rel_id, marked_ts, marked_by_kind, decided_ts, decided_by_launch "
            "FROM term_relation "
            "WHERE verb = 'same_as' AND status = 'pending' ORDER BY marked_ts, rel_id"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    system_opened = sum(
        1
        for r in rows
        if r["marked_by_kind"] == "system" and r["decided_ts"] is None and r["decided_by_launch"] is None
    )
    human_touched = count - system_opened
    status = "warn" if count else "pass"
    message = (
        f"{count} pending term duplicate candidate(s) awaiting a decision "
        f"({system_opened} system-opened, {human_touched} human-touched)" if count
        else "no pending term duplicate candidates"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={
            "count": count,
            "system_opened": system_opened,
            "human_touched": human_touched,
            "rel_ids": [r["rel_id"] for r in rows],
        },
    )


# ---------------------------------------------------------------------------
# 3. term_senses_need_review -- warn
# ---------------------------------------------------------------------------


@register_check("term_senses_need_review", category=_CATEGORY)
def check_term_senses_need_review(ctx: DoctorContext) -> CheckResult:
    """engram-F5 as a doctor check, never a mutation (MINING §5.7):
    ``current`` senses whose ``review_after`` has passed. Grouped by
    ``origin_kind`` in details because the four origins age on different
    clocks (``trialerror.lexicon.policy.REVIEW_AFTER_DAYS``) and a reader
    triaging the queue wants to know which kind is piling up."""
    name = "term_senses_need_review"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT sense_id, term_id, origin_kind FROM term_sense "
            "WHERE status = 'current' AND review_after IS NOT NULL AND review_after < ? "
            "ORDER BY review_after, sense_id",
            (now(),),
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    by_origin: dict[str, int] = {}
    for r in rows:
        by_origin[r["origin_kind"]] = by_origin.get(r["origin_kind"], 0) + 1
    status = "warn" if count else "pass"
    message = (
        f"{count} current sense(s) past their review_after window" if count
        else "no current senses are past their review window"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={"count": count, "by_origin_kind": by_origin, "sense_ids": [r["sense_id"] for r in rows]},
    )


# ---------------------------------------------------------------------------
# 4. term_sense_without_evidence -- FAIL
# ---------------------------------------------------------------------------


@register_check("term_sense_without_evidence", category=_CATEGORY)
def check_term_sense_without_evidence(ctx: DoctorContext) -> CheckResult:
    """The grounding law, audited from the outside. Every LIVE sense -- one
    that is ``proposed`` or ``current``, i.e. a reading the store is still
    offering as an answer -- must have at least one non-retracted evidence
    row. ``trialerror.lexicon.api`` has exactly one code path to ``current``
    and it enforces this before the write, and ``retract_evidence`` refuses
    to take the last live row out from under a live sense (fix pass, finding
    F1), so a positive count here means the invariant was routed around (a
    direct write, a bug, a hand-edited row), not that anything is merely
    pending review. ``fail``, not ``warn``.

    The population is :data:`trialerror.lexicon.policy.LIVE_SENSE_STATUSES`,
    deliberately narrower than "not ``rejected``". A ``superseded`` reading
    has a successor carrying its evidence forward and a ``retired`` one
    stopped being used and says so; neither is the store claiming a reading
    is grounded now, and counting them would make this check reachable from
    an ordinary retraction the API is right to allow -- a fail-level check
    that ships red on day one gets muted, which costs more than it audits.
    """
    name = "term_sense_without_evidence"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    marks = ",".join("?" for _ in LIVE_SENSE_STATUSES)
    try:
        rows = conn.execute(
            "SELECT sense_id, term_id, status FROM term_sense s "
            f"WHERE s.status IN ({marks}) AND NOT EXISTS ("
            "  SELECT 1 FROM term_sense_evidence e "
            "  WHERE e.sense_id = s.sense_id AND e.retracted_ts IS NULL"
            ") ORDER BY sense_id",
            LIVE_SENSE_STATUSES,
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "fail" if count else "pass"
    message = (
        f"{count} live sense(s) with no live evidence (the grounding invariant was bypassed)" if count
        else "every proposed or current sense has at least one live evidence row"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={"count": count, "sense_ids": [r["sense_id"] for r in rows]},
    )


# ---------------------------------------------------------------------------
# 5. term_split_missing_disambiguator -- FAIL
# ---------------------------------------------------------------------------


@register_check("term_split_missing_disambiguator", category=_CATEGORY)
def check_term_split_missing_disambiguator(ctx: DoctorContext) -> CheckResult:
    """The head layer's one invariant, audited from the outside: a
    ``split`` term's current senses must each carry a ``disambiguator`` --
    ``decide_relation``'s ``scoped`` branch refuses to leave one unset
    (:class:`trialerror.lexicon.errors.MissingDisambiguatorError`), so a
    positive count here means a split term reached that state some other
    way."""
    name = "term_split_missing_disambiguator"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT s.sense_id, s.term_id FROM term_sense s "
            "JOIN term t ON t.term_id = s.term_id "
            "WHERE t.status = 'split' AND s.status = 'current' "
            "AND (s.disambiguator IS NULL OR TRIM(s.disambiguator) = '') "
            "ORDER BY s.sense_id"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "fail" if count else "pass"
    message = (
        f"{count} current sense(s) of a split term with no disambiguator" if count
        else "every split term's current senses carry a disambiguator"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={"count": count, "sense_ids": [r["sense_id"] for r in rows]},
    )


# ---------------------------------------------------------------------------
# 6. term_evidence_source_unlinked -- warn
# ---------------------------------------------------------------------------


@register_check("term_evidence_source_unlinked", category=_CATEGORY)
def check_term_evidence_source_unlinked(ctx: DoctorContext) -> CheckResult:
    """D31 made visible: evidence rows whose ``source_key`` matches no
    ``source.source_id`` -- typically an imported register row's
    ``register_key``, still waiting for the document it names to be
    ingested. Grouped by key
    (design §6: "by key") because ``trialerror term relink`` (E2) rewrites
    one register key at a time, and this is the count that shrinks as each
    one lands."""
    name = "term_evidence_source_unlinked"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT source_key, COUNT(*) AS n FROM term_sense_evidence "
            "WHERE retracted_ts IS NULL "
            "AND source_key NOT IN (SELECT source_id FROM source) "
            "GROUP BY source_key ORDER BY source_key"
        ).fetchall()
    finally:
        conn.close()

    by_key = {r["source_key"]: r["n"] for r in rows}
    count = len(by_key)
    status = "warn" if count else "pass"
    message = (
        f"{count} distinct source_key(s) still unlinked to a source row" if count
        else "every evidence source_key resolves to a source row"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={"count": count, "by_source_key": by_key},
    )


# ---------------------------------------------------------------------------
# 7. term_fts_in_sync -- FAIL
# ---------------------------------------------------------------------------


@register_check("term_fts_in_sync", category=_CATEGORY)
def check_term_fts_in_sync(ctx: DoctorContext) -> CheckResult:
    """``term_fts`` carries one row per term plus one row per alias
    (``trialerror.lexicon.api.reindex_term``'s own docstring); the index is
    API-maintained, so a count mismatch is a bug, not drift to warn about.
    A plain count comparison rather than a text diff, per the same
    reasoning."""
    name = "term_fts_in_sync"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        fts_count = conn.execute("SELECT COUNT(*) AS n FROM term_fts").fetchone()["n"]
        term_count = conn.execute("SELECT COUNT(*) AS n FROM term").fetchone()["n"]
        alias_count = conn.execute("SELECT COUNT(*) AS n FROM term_alias").fetchone()["n"]
    finally:
        conn.close()

    expected = term_count + alias_count
    in_sync = fts_count == expected
    status = "pass" if in_sync else "fail"
    message = (
        f"term_fts has {fts_count} row(s), expected {expected} (term + term_alias) -- index is out of sync"
        if not in_sync
        else f"term_fts is in sync ({fts_count} rows == {term_count} term + {alias_count} alias)"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={"fts_count": fts_count, "term_count": term_count, "alias_count": alias_count, "expected": expected},
    )


# ---------------------------------------------------------------------------
# 8. definition_claims_unprojected -- warn
# ---------------------------------------------------------------------------


@register_check("definition_claims_unprojected", category=_CATEGORY)
def check_definition_claims_unprojected(ctx: DoctorContext) -> CheckResult:
    """The migration-from-proxy backlog (design §6/§8): live (bi-temporal
    ``expired_at IS NULL``) ``claim`` rows of ``kind='definition'`` with no
    ``term_sense_evidence(evidence_kind='claim')`` pointing at them yet --
    ``trialerror term backfill-claims`` (E2) is what projects them."""
    name = "definition_claims_unprojected"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        if not _table_exists(conn, "claim"):  # pragma: no cover - claim ships with knowledge v1
            conn.close()
            return CheckResult(
                name=name, category=_CATEGORY, status="skip",
                message="claim table not present",
            )
        rows = conn.execute(
            "SELECT claim_id FROM claim c "
            "WHERE c.kind = 'definition' AND c.expired_at IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM term_sense_evidence e "
            "  WHERE e.evidence_kind = 'claim' AND e.ref_id = c.claim_id AND e.retracted_ts IS NULL"
            ") ORDER BY c.claim_id"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "warn" if count else "pass"
    message = (
        f"{count} live definition claim(s) not yet projected into the lexicon" if count
        else "every live definition claim is projected into the lexicon"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={"count": count, "claim_ids": [r["claim_id"] for r in rows][:200]},
    )


# ---------------------------------------------------------------------------
# 9. term_system_relation_decided -- FAIL
# ---------------------------------------------------------------------------


@register_check("term_system_relation_decided", category=_CATEGORY)
def check_term_system_relation_decided(ctx: DoctorContext) -> CheckResult:
    """MINING §5.3, audited from the outside -- the second of the two
    places the design names for this constraint (``decide_relation``'s own
    unreachable-by-construction guard is the first). A system-marked
    relation may only ever be ``pending``; one that reached ``confirmed``
    with no deciding launch means a machine judgment became a decision on
    its own."""
    name = "term_system_relation_decided"
    conn, skip = _open_knowledge(ctx, name)
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT rel_id FROM term_relation "
            "WHERE marked_by_kind = 'system' AND status = 'confirmed' AND decided_by_launch IS NULL "
            "ORDER BY rel_id"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "fail" if count else "pass"
    message = (
        f"{count} system-marked relation(s) confirmed with no deciding launch" if count
        else "no system-marked relation was confirmed without a deciding launch"
    )
    return CheckResult(
        name=name, category=_CATEGORY, status=status, message=message,
        details={"count": count, "rel_ids": [r["rel_id"] for r in rows]},
    )
