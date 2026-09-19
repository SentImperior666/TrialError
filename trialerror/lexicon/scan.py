"""The disjoint-source conflict rule (design
``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` §3, "Conflict rule"), and the
idempotent whole-store scan the CLI's ``term scan`` verb drives.

**What a conflict is here.** Two *current* readings of one lemma whose
evidence comes from **disjoint source sets**. Polysemy on its own is not a
defect and never opens anything: a lemma read two ways by two documents
that never cite each other is precisely the case nobody has adjudicated,
and a lemma read two ways by sources that overlap is nuance -- the same
material, described twice. The artboard's "WHY THIS IS A CONFLICT AND NOT A
NUANCE" line is computed from the sets this module builds, not asserted.

**One item per term, not per pair.** The relation opened is term-scoped
(``src_kind = dst_kind = 'term'``, both ids the term), with the member
senses carried in its ``evidence`` JSON. A lemma read nine ways is one
queue item, not thirty-six; the reviewer's decision (``scoped`` /
``not_conflict --into`` / ``rejected``) applies to the whole member set at
once, which is what :func:`trialerror.lexicon.api.decide_relation` already
expects.

**The mixed case, stated because the design's two sentences leave it
open.** Design §3 opens a conflict when "≥2 senses and every pair is
disjoint", and in the next breath says non-disjoint pairs are "recorded in
the relation's ``evidence`` as ``shared_sources`` and never flagged". Those
only both hold if a conflict can be opened while *some* pair shares a
source. So the rule implemented here is:

* no pair disjoint  -> nothing opens (every reading overlaps: nuance);
* every pair disjoint -> one item over all current senses (the literal rule);
* some pairs disjoint -> one item over exactly the senses that take part in
  a disjoint pair, with the overlapping pairs listed in ``shared_sources``
  so the reviewer sees which of them are nuance.

The third case is the one the strict reading would have dropped on the
floor: three readings, two of them from one register and one from another,
is a real cross-source disagreement, and refusing to raise it because one
pair happens to overlap would hide it forever.

**A sense with no live evidence takes part in nothing.** Set-theoretically
the empty set is disjoint from everything, so an ungrounded sense would
manufacture a conflict with every sibling it has. It is excluded here and
reported under ``ungrounded``; the ``term_sense_without_evidence`` doctor
check is what fails on it, because through the write API that state is
unreachable.

Nothing in this module decides anything: every row it writes is
``marked_by_kind='system'``, ``status='pending'``, unattributed (MINING
§5.3).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from trialerror.lexicon import api, policy

__all__ = [
    "source_sets_for_term",
    "source_sets_for_terms",
    "source_keys_by_sense",
    "blocking_conflicts_by_term",
    "terms_by_id",
    "conflicts_for_term",
    "scan_terms",
]

#: How many ids one ``IN (...)`` clause carries. SQLite's own parameter
#: ceiling is 999 on builds before 3.32 and 32,766 after, so 500 is under
#: both with room to spare. What matters is the SHAPE: the statement count of
#: a whole-store read is ``ceil(ids / 500)``, not one per row -- and for the
#: reads below that are unfiltered (every current sense, every blocking
#: conflict) it is ONE, whatever the term count.
_ID_CHUNK = 500

#: Statuses of an existing ``conflicts_with`` row that stop the same member
#: set being raised again. ``rejected`` is in the list on purpose: the
#: program looked at exactly these readings and said "not a conflict", and a
#: scan that reopened it would turn a decision into a treadmill (design §3,
#: "Any later system scan sees the rejected row and does not reopen the same
#: member set"). ``superseded`` is absent: that row was replaced, and the
#: replacement is what blocks -- or nothing does, and the question is open
#: again.
_BLOCKING_STATUSES: tuple[str, ...] = ("pending", "confirmed", "rejected")


def _chunked(ids: Sequence[str]) -> list[list[str]]:
    """``ids`` in :data:`_ID_CHUNK`-sized runs, duplicates dropped, order
    preserved. Every id lands in exactly ONE chunk, which is what lets the
    grouped reads below keep each group's internal ordering: a chunk is
    ordered by the same ``ORDER BY`` the per-row query used, and no group is
    ever split across two of them."""
    unique = list(dict.fromkeys(ids))
    return [unique[i : i + _ID_CHUNK] for i in range(0, len(unique), _ID_CHUNK)]


def source_keys_by_sense(store, sense_ids: Sequence[str]) -> dict[str, list[str]]:
    """``{sense_id: [source_key, ...]}`` for MANY senses -- ONE statement per
    chunk of ids, standing in for one
    :func:`trialerror.lexicon.api.source_keys_for_sense` call per sense.

    The same three rules that function applies, moved into the grouped read:
    retracted rows excluded (a retraction is a read rule -- the row stays,
    the audit trail is intact), ``DISTINCT`` on the pair, and sorted by
    ``source_key`` within a sense (the leading ``sense_id`` in the ORDER BY
    only makes the grouping contiguous; it does not change that order). A
    sense with no live evidence maps to ``[]``, which is what the per-sense
    function returns for it -- not a missing key."""
    out: dict[str, list[str]] = {sid: [] for sid in sense_ids}
    conn = store.conn_for_table("term_sense_evidence")
    for chunk in _chunked(sense_ids):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT DISTINCT sense_id, source_key FROM term_sense_evidence WHERE sense_id IN ({marks}) "
            "AND retracted_ts IS NULL ORDER BY sense_id, source_key",
            chunk,
        ).fetchall()
        for sense_id, source_key in rows:
            out[sense_id].append(source_key)
    return out


def source_sets_for_term(
    store, term_id: str, *, prefetched: Mapping[str, Mapping[str, list[str]]] | None = None
) -> dict[str, list[str]]:
    """``{sense_id: [source_key, ...]}`` for the term's current senses --
    the sets the disjointness rule intersects, in one read for consumers
    (the dashboard's per-sense source counts) that want them without the
    verdict.

    TWO statements, whatever the term's sense count: the senses, then their
    evidence in one grouped read (:func:`source_keys_by_sense`). It used to
    be one per sense, which is the pattern backlog item (c) exists to remove
    -- the same per-row-query shape the Lexicon panel was measured at 48 s
    for on the live store.

    ``prefetched`` is :func:`source_sets_for_terms`' whole-store map: a
    caller that already has it (a scan over every term) passes it and this
    function reads it instead of going to the database at all. Same rows
    either way -- that map is built from the same two statements, ordered
    the same way."""
    if prefetched is not None:
        return dict(prefetched.get(term_id) or {})
    senses = api.senses_for_term(store, term_id, statuses=("current",))
    keys = source_keys_by_sense(store, [s["sense_id"] for s in senses])
    return {s["sense_id"]: keys[s["sense_id"]] for s in senses}


def source_sets_for_terms(
    store, term_ids: Sequence[str] | None = None
) -> dict[str, dict[str, list[str]]]:
    """:func:`source_sets_for_term` for a whole store (or a named subset), in
    a bounded number of statements: the current senses grouped by term, then
    their evidence grouped by sense.

    Each term's inner dict is exactly what ``source_sets_for_term(store,
    term_id)`` returns for it, in the same order: the sense stream is read
    under ``senses_for_term``'s own ``ORDER BY created_at, sense_id`` and
    grouping a globally-ordered stream preserves that order inside each
    group. A term named in ``term_ids`` with no current sense maps to an
    empty dict, which is what the per-term call returns for it; the
    whole-store form has no list of terms to seed from and simply does not
    carry a key for such a term (verify V-6), which is why every caller in
    this module passes ids and every consumer reads with a default.

    ``term_ids=None`` reads every current sense in ONE statement. A named
    subset is read in chunks, and since a term's senses cannot straddle two
    chunks (the chunking is on ``term_id``) the per-term ordering survives
    that too."""
    conn = store.conn_for_table("term_sense")
    by_term: dict[str, dict[str, list[str]]] = {}
    sense_ids: list[str] = []
    order = "ORDER BY created_at, sense_id"
    if term_ids is None:
        streams = [
            conn.execute(f"SELECT sense_id, term_id FROM term_sense WHERE status = 'current' {order}").fetchall()
        ]
    else:
        for tid in term_ids:
            by_term.setdefault(tid, {})
        streams = []
        for chunk in _chunked(list(term_ids)):
            marks = ",".join("?" for _ in chunk)
            streams.append(
                conn.execute(
                    f"SELECT sense_id, term_id FROM term_sense WHERE status = 'current' "
                    f"AND term_id IN ({marks}) {order}",
                    chunk,
                ).fetchall()
            )
    pairs: list[tuple[str, str]] = []
    for stream in streams:
        for sense_id, term_id in stream:
            pairs.append((sense_id, term_id))
            sense_ids.append(sense_id)
    keys = source_keys_by_sense(store, sense_ids)
    for sense_id, term_id in pairs:
        by_term.setdefault(term_id, {})[sense_id] = keys[sense_id]
    return by_term


def _member_set(rel: Mapping[str, Any]) -> frozenset[str]:
    """The member senses of an existing relation, as a set to compare with.

    Reads them through ``api``'s own package-private helper rather than
    re-parsing the ``evidence`` JSON here: "which senses is this row about"
    has exactly one answer, and a second implementation of it is how a scan
    starts disagreeing with the queue it is supposed to be idempotent
    against."""
    return frozenset(api._relation_member_sense_ids(rel))


#: The one SELECT both readings of "what already blocks this member set"
#: share. ``{where}`` is the only thing that differs between the per-term
#: and the grouped form, so the status list, the verb, the kind and the
#: ordering cannot drift apart between them.
_BLOCKING_SELECT = (
    "SELECT * FROM term_relation WHERE verb = 'conflicts_with' AND src_kind = 'term' "
    "AND status IN ({statuses}) AND {where} ORDER BY marked_ts, rel_id"
)


def _open_conflicts(store, term_id: str) -> list[dict[str, Any]]:
    sql = _BLOCKING_SELECT.format(
        statuses=",".join("?" for _ in _BLOCKING_STATUSES), where="src_id = ?"
    )
    rows = store.conn_for_table("term_relation").execute(
        sql, (*_BLOCKING_STATUSES, term_id)
    ).fetchall()
    return [dict(r) for r in rows]


def blocking_conflicts_by_term(
    store, term_ids: Sequence[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """:func:`_open_conflicts` for many terms at once -- ONE statement (or
    one per chunk of a named subset) instead of one per term.

    Each list is what ``_open_conflicts(store, term_id)`` returns for that
    term, in the same order: same WHERE (:data:`_BLOCKING_SELECT`), and
    grouping a stream ordered by ``marked_ts, rel_id`` preserves that order
    within each ``src_id``. A term named in ``term_ids`` with no blocking row
    maps to ``[]``; the whole-store form has no list to seed from and carries
    no key for such a term at all (verify V-6) -- read it with a default, as
    every caller here does.

    Safe to take ONCE for a whole scan even though the scan writes: a row
    :func:`conflicts_for_term` opens is always ``src_id = <its own term>``,
    so it can never belong in another term's blocking set, and each term
    reads its own set before opening anything of its own."""
    conn = store.conn_for_table("term_relation")
    out: dict[str, list[dict[str, Any]]] = {}
    statuses = ",".join("?" for _ in _BLOCKING_STATUSES)
    streams = []
    if term_ids is None:
        streams.append(
            conn.execute(
                _BLOCKING_SELECT.format(statuses=statuses, where="1=1"), _BLOCKING_STATUSES
            ).fetchall()
        )
    else:
        for tid in term_ids:
            out.setdefault(tid, [])
        for chunk in _chunked(list(term_ids)):
            marks = ",".join("?" for _ in chunk)
            streams.append(
                conn.execute(
                    _BLOCKING_SELECT.format(statuses=statuses, where=f"src_id IN ({marks})"),
                    (*_BLOCKING_STATUSES, *chunk),
                ).fetchall()
            )
    for stream in streams:
        for row in stream:
            rel = dict(row)
            out.setdefault(rel["src_id"], []).append(rel)
    return out


def terms_by_id(store, term_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """``{term_id: term row}`` for many terms -- ONE statement per chunk,
    standing in for one :func:`trialerror.lexicon.api.get_term` call per
    term. A term id nothing matches is simply absent, which is the ``None``
    that function returns for it."""
    conn = store.conn_for_table("term")
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunked(term_ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(f"SELECT * FROM term WHERE term_id IN ({marks})", chunk).fetchall():
            term = dict(row)
            out[term["term_id"]] = term
    return out


def conflicts_for_term(
    store,
    term_id: str,
    *,
    term: Mapping[str, Any] | None = None,
    source_sets: Mapping[str, Mapping[str, list[str]]] | None = None,
    blocking: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Scan one term and open its conflict item if it has one.

    Called by :func:`trialerror.lexicon.api.accept_sense` (a term acquiring
    a second current reading is exactly when a conflict can appear) and by
    :func:`scan_terms`. The return value is what lands in ``accept_sense``'s
    result under ``"conflicts"``.

    Idempotent: an item already open, confirmed or rejected for the same
    member set is reported under ``existing`` and nothing is written.

    ``term``, ``source_sets`` and ``blocking`` are the whole-store reads a
    scan over every term takes ONCE (:func:`scan_terms`) instead of asking
    the same three questions per term. Each is optional and each is the same
    rows the per-term read returns; passing none of them is the single-term
    path :func:`trialerror.lexicon.api.accept_sense` uses, unchanged.
    """
    if term is None:
        term = api.get_term(store, term_id)
    if term is None:
        return {"status": "unknown_term", "term_id": term_id, "opened": None}

    sets = source_sets_for_term(store, term_id, prefetched=source_sets)
    ungrounded = sorted(sid for sid, keys in sets.items() if not keys)
    grounded = {sid: set(keys) for sid, keys in sets.items() if keys}
    order = [s for s in sets if s in grounded]

    result: dict[str, Any] = {
        "status": "ok",
        "term_id": term_id,
        "lemma": term["lemma"],
        "sense_ids": list(sets),
        "source_sets": {sid: sorted(keys) for sid, keys in sets.items()},
        "ungrounded": ungrounded,
        "shared_sources": [],
        "member_sense_ids": [],
        "opened": None,
        "existing": None,
        "reason": None,
    }

    if len(order) < 2:
        result["reason"] = "fewer than two grounded current readings -- nothing to disagree about"
        return result

    disjoint_members: set[str] = set()
    shared: list[dict[str, Any]] = []
    for i, left in enumerate(order):
        for right in order[i + 1 :]:
            overlap = grounded[left] & grounded[right]
            if overlap:
                shared.append({"sense_ids": [left, right], "shared": sorted(overlap)})
            else:
                disjoint_members.update({left, right})
    result["shared_sources"] = shared

    if not disjoint_members:
        result["reason"] = (
            "every pair of readings shares a source -- the same material described twice, "
            "which is nuance and not a conflict"
        )
        return result

    members = [s for s in order if s in disjoint_members]
    result["member_sense_ids"] = members

    wanted = frozenset(members)
    already = list(blocking.get(term_id, ())) if blocking is not None else _open_conflicts(store, term_id)
    for rel in already:
        if _member_set(rel) == wanted:
            result["existing"] = {"rel_id": rel["rel_id"], "status": rel["status"], "decided_verb": rel["decided_verb"]}
            result["reason"] = f"already raised as {rel['rel_id']} ({rel['status']})"
            return result

    opened = api.open_relation(
        store,
        src_kind="term",
        src_id=term_id,
        dst_kind="term",
        dst_id=term_id,
        verb="conflicts_with",
        marked_by_kind="system",
        marked_by_model=policy.SYSTEM_SCAN_MODEL,
        reason=(
            f"{len(members)} current readings of {term['lemma']!r} stand on disjoint sources"
        ),
        evidence={
            "sense_ids": members,
            "source_sets": {sid: sorted(grounded[sid]) for sid in members},
            "shared_sources": shared,
            "scan": policy.SYSTEM_SCAN_MODEL,
        },
    )
    result["opened"] = opened["rel_id"]
    result["reason"] = "opened"
    return result


def scan_terms(
    store,
    *,
    term_ids: Iterable[str] | None = None,
    duplicates: bool = True,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The whole-store pass behind ``trialerror term scan`` -- conflicts,
    and (unless ``duplicates=False``) the duplicate half too.

    Idempotent in both halves, which is what makes re-running it after a
    backfill safe: :func:`conflicts_for_term` skips a member set that has
    already been raised, and
    :func:`trialerror.lexicon.candidates.surface_candidates` skips a pair
    that already has a relation of any status.

    Terms are scanned in id order and ``merged``/``retired`` ones are
    skipped: a folded term's readings belong to the term it was folded
    into, and re-raising them under the old id would produce an item whose
    lemma no longer resolves to it.
    """
    from trialerror.lexicon.candidates import surface_candidates, token_stats

    if term_ids is None:
        rows = store.conn_for_table("term").execute(
            "SELECT term_id FROM term WHERE status NOT IN ('merged','retired') ORDER BY term_id"
        ).fetchall()
        ids: Sequence[str] = [r[0] for r in rows]
    else:
        ids = [t for t in term_ids]

    # The duplicate gate's view of what is a common word (build step 1c) is
    # a whole-store measurement, so a whole-store pass takes it ONCE rather
    # than rebuilding it per term -- the same numbers for every term in one
    # scan, and one table read instead of thousands. The save-time call
    # deliberately does the opposite; see `candidates.token_stats`.
    stats = token_stats(store, config=config) if duplicates else None

    # Backlog item (c): the conflict half's three questions per term -- the
    # term row, its senses' source sets, the rows already blocking a member
    # set -- are taken ONCE here, in a bounded number of statements, for the
    # same reason `stats` is. They used to be 1 + (1 + senses) + 1 queries per
    # term, i.e. the shape the Lexicon panel was measured at 48.1 s for on the
    # live 7,260-term store. The prefetch is safe against the scan's own
    # writes: nothing here adds a sense or a term, and the only relation rows
    # it opens are `src_id = <that term>` (see `blocking_conflicts_by_term`).
    # The duplicate half stays per term on purpose -- an FTS/trigram search
    # against the rest of the store is not one question asked N times.
    terms = terms_by_id(store, ids)
    source_sets = source_sets_for_terms(store, ids)
    blocking = blocking_conflicts_by_term(store, ids)

    conflicts: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    scanned = 0
    for term_id in ids:
        term = terms.get(term_id)
        if term is None or term["status"] in ("merged", "retired"):
            continue
        scanned += 1
        outcome = conflicts_for_term(
            store, term_id, term=term, source_sets=source_sets, blocking=blocking
        )
        if outcome.get("opened"):
            conflicts.append({"term_id": term_id, "rel_id": outcome["opened"], "members": outcome["member_sense_ids"]})
        if duplicates:
            surfaced = surface_candidates(store, term_id, config=config, stats=stats)
            duplicate_rows.extend(surfaced.get("opened", []))

    return {
        "status": "ok",
        "terms_scanned": scanned,
        "conflicts_opened": len(conflicts),
        "conflicts": conflicts,
        "duplicates_opened": len(duplicate_rows),
        "duplicates": duplicate_rows,
    }
