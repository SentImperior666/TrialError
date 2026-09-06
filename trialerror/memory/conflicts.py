"""Mining adoption engram-F4: save-time conflict-candidate surfacing over
``memory_item``, with a locked judgment-verb taxonomy and actor/kind/model
provenance.

Source: ``docs/mining/G25-operator-2026-09__engram.md`` finding 3
(``internal/store/relations.go:354-395`` ``FindCandidates``, ``:32-59``
the six locked verbs). Disposition:
``docs/reviews/MINING_2026-09_OPERATOR_LINKS.md`` section 3, orchestrator
verdict **"adopt-now:memory WITH CONSTRAINT - advisory candidates only,
never auto-applied verdicts"**.

**Why this is not `trialerror.memory.merge`.** ``merge.py`` classifies only
at reconcile time, over two already-diverged stores. ``api.put_item``
resolves by exact ``(key, account_id)`` and nothing else. Neither notices
that a NEW item, filed under a different key, says the opposite of one
written six weeks ago in the same store. This module runs at save time,
inside one store, and answers exactly that question -- complementary, not
overlapping (the mining report's own "prior art is honestly partial").

**The constraint (review section 5.3).** The source ships a
``JudgeBySemantic`` path that lets an embedding pass pre-populate "obvious"
verdicts with ``marked_by_kind="system"``. That is a silent auto-merge by
another name, and this port does not have it. What lands here:

- the candidate surfacing (:func:`candidates_for`, :func:`record_candidates`),
  which only ever writes ``judgment_status='pending'`` rows with NO verb;
- the locked verb taxonomy with provenance (:func:`judge`);
- and a hard refusal: :func:`judge` raises on ``actor_kind="system"``.
  Machines may PROPOSE (a pending row, which is what an automated scan
  writes) and may judge as an ``agent`` under their own named identity --
  they may not write a verdict as an anonymous system act.

**Why BM25 in Python and not FTS5.** ``memory_item`` has no FTS index
today, and the orchestrator verdict ties the trigram/FTS half to lane e's
term store ("implement with lane e (term store) so the trigram/FTS half
reuses the new index"). Rather than create a second, competing index that
lane e would have to unpick, the ranking is a ~40-line Okapi BM25 over the
item rows themselves: the corpus is a few hundred short rows, so the cost
is irrelevant, and :func:`candidates_for` is a pure function of the rows
it is handed. When lane e's index lands, only :func:`_score_corpus` needs
replacing -- the taxonomy, the pending-row contract, and every caller stay
put.
"""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from trialerror.stores import insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "RELATION_VERBS",
    "JUDGMENT_STATUSES",
    "ACTOR_KINDS",
    "DEFAULT_CANDIDATE_LIMIT",
    "candidates_for",
    "record_candidates",
    "list_candidates",
    "judge",
    "render_note",
    "scan_and_record",
]

#: The locked judgment vocabulary, verbatim from the source
#: (``internal/store/relations.go:32-59``). Locked means locked: a verb
#: outside this tuple is refused by :func:`judge` AND by the DDL's own
#: CHECK constraint, so no caller can invent a seventh meaning that later
#: readers have to guess at.
RELATION_VERBS: tuple[str, ...] = (
    "related",
    "compatible",
    "scoped",
    "conflicts_with",
    "supersedes",
    "not_conflict",
)

#: ``pending`` = surfaced by a scan, nobody has ruled. ``judged`` = a human
#: or a named agent recorded a verb. ``superseded`` = an later judgment
#: replaced this one (the chain is ``superseded_by_relation_id``).
JUDGMENT_STATUSES: tuple[str, ...] = ("pending", "judged", "superseded")

#: Who recorded a verdict. ``system`` exists in the schema because the
#: source has it and a future import might carry such rows in -- but
#: :func:`judge` refuses to MINT one (review section 5.3).
ACTOR_KINDS: tuple[str, ...] = ("human", "agent", "system")

#: How many candidates one save surfaces. Small on purpose: an advisory
#: that lists twelve maybe-related items is an advisory nobody reads.
DEFAULT_CANDIDATE_LIMIT = 5

# Okapi BM25's usual constants; no tuning was performed and none is
# claimed -- the ranking only has to order a handful of short rows.
_BM25_K1 = 1.2
_BM25_B = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _ops(handle: Store | sqlite3.Connection) -> sqlite3.Connection:
    """Accept either a :class:`~trialerror.stores.store.Store` or a bare
    ops connection -- the doctor checks and the dashboard's read-only
    store both hold the latter (same convention as
    ``trialerror.memory.merge.list_conflicts``' ``store.ops`` reads)."""
    return getattr(handle, "ops", handle)


def _tokens(*parts: str | None) -> list[str]:
    """Lowercase alphanumeric tokens, singles dropped. No stemming and no
    stopword list: with a corpus this small, a stopword list costs more in
    surprise ("why didn't 'not' match?") than it buys in precision."""
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        out.extend(t for t in _TOKEN_RE.findall(part.lower()) if len(t) > 1)
    return out


def _score_corpus(query_tokens: Sequence[str], docs: Sequence[tuple[str, list[str]]]) -> dict[str, float]:
    """Okapi BM25 of ``query_tokens`` against ``docs`` (``(doc_id,
    tokens)`` pairs). The IDF is the ``ln(1 + (N-n+0.5)/(n+0.5))``
    smoothed form, which is strictly positive -- the unsmoothed classic
    goes NEGATIVE for a term present in more than half the corpus, which
    on a corpus of eight memory items is the common case, not the corner
    case."""
    n_docs = len(docs)
    if not n_docs or not query_tokens:
        return {}
    avg_len = sum(len(toks) for _, toks in docs) / n_docs or 1.0
    df: dict[str, int] = {}
    for _, toks in docs:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    scores: dict[str, float] = {}
    for doc_id, toks in docs:
        if not toks:
            continue
        freqs: dict[str, int] = {}
        for t in toks:
            freqs[t] = freqs.get(t, 0) + 1
        length = len(toks)
        total = 0.0
        for term in set(query_tokens):
            f = freqs.get(term, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            total += idf * (f * (_BM25_K1 + 1)) / (f + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / avg_len))
        if total > 0:
            scores[doc_id] = total
    return scores


def candidates_for(
    handle: Store | sqlite3.Connection,
    *,
    key: str,
    body: str,
    l0_abstract: str | None = None,
    account_id: str | None = None,
    exclude_id: str | None = None,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """BM25-rank the existing ACTIVE items against the item being saved and
    return the top ``limit`` as advisory candidates, best first.

    Query side is the new item's ``key`` + ``l0_abstract`` -- the source
    scopes its own FTS query to the observation's TITLE, and the same
    instinct holds here: an item's key and abstract say what it is ABOUT,
    while its body says what it says, and a body-vs-body match mostly
    finds shared vocabulary rather than shared subject. Document side is
    key + abstract + body, so an existing item still matches on its
    contents.

    **With one fallback the source does not need.** Engram's titles are
    sentences; this schema's ``key`` is a slug and ``l0_abstract`` is
    optional, so a terse-keyed item with no abstract would yield an EMPTY
    query and therefore -- silently -- never surface anything at all. When
    key + abstract produce no usable tokens the body stands in, on the
    principle that a weaker signal beats a mechanism that quietly does
    nothing.

    Reads only. Nothing is written and nothing is decided; see
    :func:`record_candidates` for the (also advisory) persistence step.
    """
    conn = _ops(handle)
    sql = "SELECT memory_item_id, key, tier, kind, l0_abstract, body, account_id FROM memory_item WHERE status = 'active'"
    params: list[Any] = []
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    docs: list[tuple[str, list[str]]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if exclude_id is not None and row["memory_item_id"] == exclude_id:
            continue
        if row["key"] == key:
            # Same key = the upsert's own business (api.put_item edits it
            # in place); surfacing it as a "conflict candidate" would
            # report every ordinary edit as a possible contradiction.
            continue
        by_id[row["memory_item_id"]] = row
        docs.append((row["memory_item_id"], _tokens(row["key"], row.get("l0_abstract"), row.get("body"))))

    query_tokens = _tokens(key, l0_abstract) or _tokens(body)
    scores = _score_corpus(query_tokens, docs)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        {
            "target_id": doc_id,
            "key": by_id[doc_id]["key"],
            "tier": by_id[doc_id]["tier"],
            "kind": by_id[doc_id]["kind"],
            "l0_abstract": by_id[doc_id].get("l0_abstract"),
            "score": round(score, 6),
        }
        for doc_id, score in ranked
    ]


def record_candidates(
    store: Store,
    *,
    source_id: str,
    candidates: Iterable[Mapping[str, Any]],
    ts: str | None = None,
) -> list[dict[str, Any]]:
    """Persist ``candidates`` as ``pending`` :data:`memory_relation` rows.

    Pending means pending: ``relation`` is left NULL and no
    ``marked_by_*`` provenance is written, because nothing has been
    judged. A pair that is ALREADY pending or already judged is skipped --
    re-saving an item must not grow a pile of duplicate advisories, and it
    must never resurrect a pair a human already ruled ``not_conflict``.
    """
    ts = ts or now()
    existing = {
        r["target_id"]
        for r in store.ops.execute(
            "SELECT target_id FROM memory_relation WHERE source_id = ? AND judgment_status IN ('pending','judged')",
            (source_id,),
        ).fetchall()
    }
    written: list[dict[str, Any]] = []
    for cand in candidates:
        target_id = cand["target_id"]
        if target_id == source_id or target_id in existing:
            continue
        row = {
            "relation_id": new_id("MREL"),
            "source_id": source_id,
            "target_id": target_id,
            "relation": None,
            "judgment_status": "pending",
            "score": cand.get("score"),
            "reason": cand.get("reason") or "save-time BM25 similarity (advisory, unjudged)",
            "evidence": cand.get("evidence"),
            "confidence": None,
            "marked_by_actor": None,
            "marked_by_kind": None,
            "marked_by_model": None,
            "created_ts": ts,
            "judged_ts": None,
            "superseded_by_relation_id": None,
        }
        written.append(insert(store, "memory_relation", row))
        existing.add(target_id)
    return written


def list_candidates(
    handle: Store | sqlite3.Connection,
    *,
    source_id: str | None = None,
    status: str | None = "pending",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Relation rows, newest first, joined to both sides' keys so a caller
    can render "X may contradict Y" without a second query.
    ``status=None`` removes the status filter."""
    conn = _ops(handle)
    clauses: list[str] = []
    params: list[Any] = []
    if source_id is not None:
        clauses.append("r.source_id = ?")
        params.append(source_id)
    if status is not None:
        clauses.append("r.judgment_status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        "SELECT r.*, s.key AS source_key, t.key AS target_key, t.l0_abstract AS target_abstract "
        "FROM memory_relation r "
        "JOIN memory_item s ON s.memory_item_id = r.source_id "
        "JOIN memory_item t ON t.memory_item_id = r.target_id "
        f"{where} ORDER BY r.created_ts DESC, r.rowid DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def judge(
    store: Store,
    *,
    relation_id: str,
    relation: str,
    actor: str,
    actor_kind: str,
    model: str | None = None,
    reason: str | None = None,
    confidence: float | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Record ONE verdict from the locked vocabulary against a pending
    relation, with provenance.

    Refuses, loudly:

    - a verb outside :data:`RELATION_VERBS`;
    - ``actor_kind="system"`` -- **review section 5.3**: an automated pass
      may surface a candidate, never write the verdict. A model that wants
      to rule does it as a named ``agent`` (its own identity in ``actor``,
      its model string in ``model``), which leaves an auditable "this
      verdict came from a machine" trail instead of an anonymous one;
    - an unknown or already-judged relation id. A change of mind is a NEW
      judgment that supersedes the old row (``superseded_by_relation_id``),
      matching the source's deliberate "multi-actor disagreement allowed"
      row model -- never an in-place edit that erases what the first judge
      thought.
    """
    if relation not in RELATION_VERBS:
        raise ValueError(f"judge: relation must be one of {RELATION_VERBS!r}, got {relation!r}")
    if actor_kind not in ACTOR_KINDS:
        raise ValueError(f"judge: actor_kind must be one of {ACTOR_KINDS!r}, got {actor_kind!r}")
    if actor_kind == "system":
        raise ValueError(
            "judge: actor_kind='system' is refused -- an automated pass surfaces PENDING candidates, "
            "it does not write verdicts (MINING_2026-09_OPERATOR_LINKS.md section 5.3: machine verdicts "
            "stay advisory and pending). Judge as actor_kind='agent' under a named actor instead."
        )
    if not actor or not actor.strip():
        raise ValueError("judge: actor is required (provenance is the point of the taxonomy)")

    row = store.ops.execute(
        "SELECT * FROM memory_relation WHERE relation_id = ?", (relation_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"judge: no memory_relation {relation_id!r}")
    if row["judgment_status"] != "pending":
        raise ValueError(
            f"judge: relation {relation_id!r} is already {row['judgment_status']!r}; "
            "record a NEW judgment (which supersedes this one) rather than editing a settled verdict"
        )

    ts = ts or now()
    changes = {
        "relation": relation,
        "judgment_status": "judged",
        "reason": reason if reason is not None else row["reason"],
        "confidence": confidence,
        "marked_by_actor": actor,
        "marked_by_kind": actor_kind,
        "marked_by_model": model,
        "judged_ts": ts,
    }
    update(store, "memory_relation", pk_column="relation_id", pk_value=relation_id, changes=changes)
    merged = dict(row)
    merged.update(changes)
    return merged


def render_note(source_key: str, candidates: Sequence[Mapping[str, Any]]) -> str | None:
    """The human-facing advisory text -- what the CLI prints and what a
    ``trialerror memory put --feed-thread`` posts. ``None`` when there is
    nothing to say, so a caller can test the note itself for "was there an
    advisory" rather than checking a separate flag."""
    if not candidates:
        return None
    lines = [
        f"ADVISORY (unjudged): saving memory item {source_key!r} surfaced "
        f"{len(candidates)} existing item(s) that may relate to or contradict it.",
        "Nothing was blocked, changed, or decided -- these are pending candidates awaiting a human "
        "or a named agent's verdict (`trialerror memory judge`).",
        "",
    ]
    for cand in candidates:
        abstract = cand.get("l0_abstract") or "(no abstract)"
        lines.append(f"- {cand['key']} [{cand.get('tier')}/{cand.get('kind')}] score={cand.get('score')} — {abstract}")
    return "\n".join(lines)


def scan_and_record(
    store: Store,
    *,
    item: Mapping[str, Any],
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    ts: str | None = None,
) -> list[dict[str, Any]]:
    """Scan for candidates against ``item`` and persist them as pending
    rows, returning the candidate dicts (each carrying the
    ``relation_id`` it was recorded under). The one call
    :func:`trialerror.memory.api.put_item` makes."""
    found = candidates_for(
        store,
        key=item["key"],
        body=item.get("body") or "",
        l0_abstract=item.get("l0_abstract"),
        account_id=item.get("account_id"),
        exclude_id=item.get("memory_item_id"),
        limit=limit,
    )
    written = record_candidates(store, source_id=item["memory_item_id"], candidates=found, ts=ts)
    by_target = {w["target_id"]: w["relation_id"] for w in written}
    out: list[dict[str, Any]] = []
    for cand in found:
        rel_id = by_target.get(cand["target_id"])
        if rel_id is None:
            continue  # already pending or already judged -- not a new advisory
        out.append({**cand, "relation_id": rel_id})
    return out
