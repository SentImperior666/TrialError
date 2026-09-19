"""The term store's write lifecycle and the reads its consumers need.
Design of record: ``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` §3.

Every mutating function here takes a ``Store``, requires a ``by_launch``
for anything that decides, writes through
``trialerror.stores.writer``/``trialerror.stores.bitemporal`` (so XID
refuse-on-missing and every CHECK constraint apply), appends one type-keyed
``event``, and returns a plain dict -- the
``trialerror.ingest.extract.accept_candidate`` shape, deliberately, since
the two are the same kind of thing: a reviewed promotion out of a queue.

Reading order, if this is the first time:

    propose            a lemma, a reading of it, and the evidence for that
                       reading arrive together, or not at all
    accept_sense       that reading becomes the program's current one
    supersede_sense    a corrected reading; the old one stays readable
    retire_sense       the reading stopped being used, and we say so
    open_relation      one judgment about two terms or two senses
    decide_relation    a launch resolves that judgment; three of the six
                       decisions move other rows, and this is where they do
    merge_terms        two lemmas turn out to name one thing

**The two hooks.** Save-time duplicate surfacing
(:mod:`trialerror.lexicon.candidates`) and the disjoint-source conflict
scan (:mod:`trialerror.lexicon.scan`) are reached through import-guarded
calls that report their own absence in the returned dict
(``{"status": "unavailable", "reason": ...}``) rather than silently doing
nothing: a caller reading the result can tell "no candidates were found"
apart from "nothing looked", which is exactly the distinction the
never-silent-auto-merge posture is about. Both modules landed with step E2
and the hooks return real answers now; the guards stay, because a partial
install is a state a consumer of this API should be able to read rather
than crash on.

**On ``source_key``.** The disjoint-source conflict rule counts source
identities, so every evidence row must be able to name one. Three of the
four evidence kinds derive it from a real row (an anchor and a claim reach
a ``source.source_id`` through ``quote_anchor.doc_id -> document``; a
record carries its ``register_key``). The fourth, ``idea``, has no source
behind it -- so an idea's ``source_key`` is the idea's own id. That is a
choice with a consequence worth stating: two senses coined in two separate
ideation rounds count as disjoint and will open a conflict, which is
correct -- they are two independent readings that nobody has reconciled --
whereas folding all ideation into one shared key would have made them read
as nuance.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from trialerror.events.api import append_event
from trialerror.lexicon import policy
from trialerror.lexicon.errors import (
    GlossTooLongError,
    InvalidDecisionError,
    InvalidEvidenceError,
    InvalidMergeError,
    InvalidTermInputError,
    LaunchRequiredError,
    MissingDisambiguatorError,
    RelationNotFoundError,
    RelationNotPendingError,
    SenseNotDecidableError,
    SenseNotFoundError,
    SenseWithoutEvidenceError,
    TermNotFoundError,
)
from trialerror.lexicon.normalize import fts_text, norm_lemma, norm_with_offsets, word_count
from trialerror.stores.bitemporal import assert_fact, end_fact_validity, expire_fact, supersede_fact
from trialerror.stores.store import Store
from trialerror.stores.writer import get, insert, require_xid_targets, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    # reads
    "get_term",
    "find_term",
    "get_sense",
    "get_relation",
    "senses_for_term",
    "evidence_for_sense",
    "source_keys_for_sense",
    "relations_for_term",
    "needs_review",
    "conflicts_for_claim",
    # text-facing reads (design §5's consumers)
    "match_terms_in_text",
    "lookup_terms",
    "covered_region_labels",
    # writes
    "propose",
    "accept_sense",
    "reject_sense",
    "supersede_sense",
    "retire_sense",
    "mark_reviewed",
    "retract_evidence",
    "open_relation",
    "decide_relation",
    "merge_terms",
    "reindex_term",
    "reindex_all",
]

#: The evidence-kind token the CLI and the extraction hook spell ``anchor``.
_EVIDENCE_KIND_ALIASES = {"anchor": "quote_anchor"}


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------


def _conn(store: Store) -> sqlite3.Connection:
    """The knowledge.db connection, resolved through the store's own routing
    map so an ``RoStore`` (read-only, dashboard side) works unchanged."""
    return store.conn_for_table("term")


def _require_launch(store: Store, by_launch: str | None, *, what: str) -> str:
    """Refuse a decision with no launch, then refuse one whose launch names
    no row -- in that order, before anything is written.

    Ruling L-E4 in two lines. The second half is a pre-flight of the write
    the caller is about to make (``trialerror.stores.writer.
    require_xid_targets``, the same guard ``ingest.extract`` uses for the
    identical reason): a multi-step decision whose audit ``event`` lands
    last must not mutate anything and *then* discover the launch is
    fictional.

    Every deciding verb calls this FIRST, before it even loads the row it
    was asked about. That ordering is deliberate and uniform: attribution
    is the one precondition none of them can proceed without, so a caller
    can rely on "no ``by_launch``" always raising this rather than
    whichever state check the target row happened to fail first."""
    if not by_launch:
        raise LaunchRequiredError(f"{what} requires by_launch (ruling L-E4: an existing platform.launch row)")
    require_xid_targets(store, "event", {"launch_id": by_launch})
    return by_launch


def _require_vocab(value: Any, allowed: Sequence[str], *, field: str, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if value not in allowed:
        raise InvalidTermInputError(f"{field}={value!r} is not one of {list(allowed)!r}")
    return value


def _require_gloss(
    gloss: str | None,
    *,
    origin_kind: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """The gloss, stripped, or a named refusal.

    ``origin_kind`` selects which cap applies (build step 1c, decision D5).
    The register-import route keeps an imported reading whole, up to
    :data:`trialerror.lexicon.policy.GLOSS_MAX_WORDS_IMPORT`; every other
    route -- including a hand-written correction TO an imported sense --
    is the program writing its own words and gets the strict cap. Omitting
    the argument is the strict cap, which is the direction a default here
    should fail in.
    """
    text = (gloss or "").strip()
    if not text:
        raise InvalidTermInputError("gloss must be a non-empty own-words reading of the term")
    cap = policy.gloss_cap_for(origin_kind, policy=policy.load_policy(config))
    count = word_count(text)
    if count > cap:
        raise GlossTooLongError(
            f"gloss is {count} words, cap is {cap} -- a gloss is the program's own reading; "
            "the source's wording belongs in an evidence row, anchored and fenced"
        )
    return text


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _live_evidence(store: Store, sense_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in _conn(store)
        .execute(
            "SELECT * FROM term_sense_evidence WHERE sense_id = ? AND retracted_ts IS NULL "
            "ORDER BY created_ts, evidence_id",
            (sense_id,),
        )
        .fetchall()
    ]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_term(store: Store, term_id: str) -> dict[str, Any] | None:
    return get(store, "term", pk_column="term_id", pk_value=term_id)


def get_sense(store: Store, sense_id: str) -> dict[str, Any] | None:
    return get(store, "term_sense", pk_column="sense_id", pk_value=sense_id)


def get_relation(store: Store, rel_id: str) -> dict[str, Any] | None:
    return get(store, "term_relation", pk_column="rel_id", pk_value=rel_id)


def find_term(store: Store, lemma: str, *, follow_merges: bool = True) -> dict[str, Any] | None:
    """Resolve a written form to the term it names now.

    The lookup surface is ``term.lemma_norm`` first, then
    ``term_alias.alias_norm`` -- that union is the whole of it (design §2),
    and it is why a merge loses no key: the folded term's lemma survives as
    an alias of the canonical one.

    ``follow_merges`` (default on) then walks ``merged_into`` to the live
    term. This matters because a merge deletes nothing: the folded row
    keeps its own ``lemma_norm``, so a raw lookup would keep returning the
    term that was folded away -- technically a real row, and the wrong
    answer to "what does this name mean". Pass ``follow_merges=False`` to
    see the row as stored, which is what an audit of the merge itself
    wants. The walk is bounded by the number of terms, so a corrupted
    ``merged_into`` cycle terminates rather than hanging.
    """
    key = norm_lemma(lemma)
    if not key:
        return None
    conn = _conn(store)
    row = conn.execute("SELECT * FROM term WHERE lemma_norm = ?", (key,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT t.* FROM term_alias a JOIN term t ON t.term_id = a.term_id "
            "WHERE a.alias_norm = ? ORDER BY a.created_ts, a.alias_id LIMIT 1",
            (key,),
        ).fetchone()
    if row is None:
        return None

    term = dict(row)
    return term if not follow_merges else _follow_merges(store, term)


def _follow_merges(store: Store, term: Mapping[str, Any]) -> dict[str, Any]:
    """Walk ``merged_into`` from ``term`` to the live term it was folded
    into, and return that row.

    Extracted from :func:`find_term` because three other reads need the
    identical walk and "the way ``find_term(follow_merges=True)`` does it"
    is a contract other lanes are written against -- two copies of a
    graph walk are two chances to disagree about a cycle. The bound is the
    set of ids already seen, so a corrupted ``merged_into`` cycle
    terminates rather than hanging, and a pointer to a row that is gone
    stops at the last row that exists rather than returning nothing.
    """
    current = dict(term)
    seen = {current["term_id"]}
    while current.get("merged_into") and current["merged_into"] not in seen:
        seen.add(current["merged_into"])
        nxt = get_term(store, current["merged_into"])
        if nxt is None:
            break
        current = nxt
    return current


def senses_for_term(
    store: Store, term_id: str, *, statuses: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM term_sense WHERE term_id = ?"
    params: list[Any] = [term_id]
    if statuses:
        sql += f" AND status IN ({','.join('?' for _ in statuses)})"
        params += list(statuses)
    sql += " ORDER BY created_at, sense_id"
    return [dict(r) for r in _conn(store).execute(sql, params).fetchall()]


def evidence_for_sense(
    store: Store, sense_id: str, *, include_retracted: bool = False
) -> list[dict[str, Any]]:
    if include_retracted:
        rows = _conn(store).execute(
            "SELECT * FROM term_sense_evidence WHERE sense_id = ? ORDER BY created_ts, evidence_id",
            (sense_id,),
        )
        return [dict(r) for r in rows.fetchall()]
    return _live_evidence(store, sense_id)


def source_keys_for_sense(store: Store, sense_id: str) -> list[str]:
    """The non-retracted source identities behind one sense, sorted.

    This is the set the disjointness rule intersects. Retracted rows are
    excluded because a retraction is the program saying "this never should
    have counted" -- but the row itself stays, so the exclusion is a read
    rule and the audit trail is intact."""
    rows = _conn(store).execute(
        "SELECT DISTINCT source_key FROM term_sense_evidence "
        "WHERE sense_id = ? AND retracted_ts IS NULL ORDER BY source_key",
        (sense_id,),
    )
    return [r[0] for r in rows.fetchall()]


def relations_for_term(
    store: Store, term_id: str, *, statuses: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Every relation naming this term, or any of its senses, on either
    side. A conflict item is term-scoped (one queue entry for a nine-way
    polysemous lemma, not thirty-six pairwise ones) while a duplicate
    candidate is term-to-term, so both shapes are reachable from here."""
    conn = _conn(store)
    sense_ids = [r[0] for r in conn.execute("SELECT sense_id FROM term_sense WHERE term_id = ?", (term_id,))]
    ids = [term_id, *sense_ids]
    marks = ",".join("?" for _ in ids)
    # The disjunction is parenthesized: `A OR B AND C` binds as `A OR (B AND
    # C)` in SQL, which would have made the status filter apply to only one
    # side of the relation.
    sql = f"SELECT * FROM term_relation WHERE (src_id IN ({marks}) OR dst_id IN ({marks}))"
    params: list[Any] = ids + ids
    if statuses:
        sql += f" AND status IN ({','.join('?' for _ in statuses)})"
        params += list(statuses)
    sql += " ORDER BY marked_ts, rel_id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def needs_review(sense: Mapping[str, Any], *, at: str | None = None) -> bool:
    """engram-F5 as a COMPUTED flag (MINING §5.7).

    Read from ``review_after`` at read time; never stored, never a status.
    A sense that is not ``current`` is never stale -- there is nothing to
    re-confirm about a rejected or superseded reading."""
    if sense.get("status") != "current":
        return False
    stamp = sense.get("review_after")
    if not stamp:
        return False
    return str(stamp) < (at or now())


def conflicts_for_claim(store: Store, claim_id: str) -> list[dict[str, Any]]:
    """Ruling L-C5's read: the open sense conflicts on any term this claim
    is evidence for. Pure -- no writes, no events, safe on an ``RoStore``.

    Wired into the Evidence panel's WHAT ARGUES WITH IT region
    (``trialerror.dashboard.data._evidence_lexicon_conflicts``, which
    already exists and already guards on ``ImportError`` /
    ``sqlite3.OperationalError``). This function deliberately does NOT
    swallow ``OperationalError`` itself: on a store that has not run the v5
    migration the tables are genuinely absent, and the caller's guard is
    what turns that into "the region is omitted with a stated reason".
    Catching it here would return ``[]``, which renders as "nothing argues
    with this claim" -- a different and false statement.

    Returns one entry per (term, open conflict relation), each carrying the
    member senses and their source sets, so the panel can show the
    disjointness rather than assert it.
    """
    conn = _conn(store)
    term_rows = conn.execute(
        "SELECT DISTINCT s.term_id FROM term_sense_evidence e "
        "JOIN term_sense s ON s.sense_id = e.sense_id "
        "WHERE e.evidence_kind = 'claim' AND e.ref_id = ? AND e.retracted_ts IS NULL "
        "ORDER BY s.term_id",
        (claim_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in term_rows:
        term_id = row[0]
        term = get_term(store, term_id)
        if term is None:  # pragma: no cover - FK makes this unreachable
            continue
        for rel in conn.execute(
            "SELECT * FROM term_relation WHERE verb = 'conflicts_with' "
            "AND status IN ('pending','confirmed') AND src_kind = 'term' AND src_id = ? "
            "ORDER BY marked_ts, rel_id",
            (term_id,),
        ).fetchall():
            rel = dict(rel)
            members = _relation_member_sense_ids(rel)
            out.append(
                {
                    "term_id": term_id,
                    "lemma": term["lemma"],
                    "term_status": term["status"],
                    "rel_id": rel["rel_id"],
                    "status": rel["status"],
                    "decided_verb": rel["decided_verb"],
                    "opened_ts": rel["marked_ts"],
                    "marked_by_kind": rel["marked_by_kind"],
                    "member_sense_ids": members,
                    "senses": [
                        {
                            "sense_id": sid,
                            "gloss": (get_sense(store, sid) or {}).get("gloss"),
                            "source_keys": source_keys_for_sense(store, sid),
                        }
                        for sid in members
                    ],
                }
            )
    return out


# ---------------------------------------------------------------------------
# text-facing reads -- design §5's two consumers outside this package
# ---------------------------------------------------------------------------
#
# Three functions, one shared lookup surface, and one property worth stating
# once for all of them: they are PURE. No writes, no events, nothing that
# needs a ``by_launch`` -- so every one of them is safe on an ``RoStore``
# and safe to call from a renderer. They are also the only reads in this
# module that answer a question about text a *generator* wrote or will
# read, which is why the label vocabulary each one returns is drawn
# deliberately rather than by handing back whatever the row had.


def _lookup_index(store: Store) -> tuple[dict[str, tuple[str, str]], int]:
    """``lemma_norm ∪ alias_norm`` → ``(term_id, "lemma"|"alias")``, with
    the longest key's length in characters.

    The union is the store's whole lookup surface (design §2), and the
    precedence inside it is :func:`find_term`'s, restated as a build order
    rather than as a second query: a key that is some term's ``lemma_norm``
    resolves to that term (the column is UNIQUE, so that half cannot
    collide with itself), and a key that is only ever an alias resolves to
    the term whose alias row was created first -- exactly what
    ``ORDER BY created_ts, alias_id LIMIT 1`` gives a single lookup.

    An alias key colliding *across* terms is a real state, not a corrupt
    one: ``term_alias`` is UNIQUE per ``(term_id, alias_norm)``, not
    globally, and two terms sharing a spelling is precisely the question
    the duplicate-candidate queue exists to put in front of a human.
    Reporting it here as two matches would turn one open question into two
    confident findings, so the first-created row wins and the queue keeps
    the question.

    Two queries and a dict, rebuilt per call. At the sizes this lane plans
    for (thousands of terms) that is milliseconds; the alternative is a
    cache keyed on a store that any write invalidates, which is a
    stale-glossary bug waiting for the first background writer. Nothing in
    the returned index refers back to the connection, so a batch caller
    that profiles this can hoist it.
    """
    conn = _conn(store)
    index: dict[str, tuple[str, str]] = {}
    longest = 0
    for row in conn.execute("SELECT term_id, lemma_norm FROM term").fetchall():
        key = row[1]
        if not key:
            continue
        index[key] = (row[0], "lemma")
        longest = max(longest, len(key))
    for row in conn.execute(
        "SELECT term_id, alias_norm FROM term_alias ORDER BY created_ts, alias_id"
    ).fetchall():
        key = row[1]
        if not key or key in index:
            continue
        index[key] = (row[0], "alias")
        longest = max(longest, len(key))
    return index, longest


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _boundary_at(normalized: str, pos: int) -> bool:
    """True when ``pos`` is a position a match may start or end at.

    The rule is ``\\b``'s, spelled out because it is the whole of "word
    boundary" for this module: a position is a boundary unless a word
    character sits on both sides of it. That admits a match that starts or
    ends on punctuation the term itself carries (a hyphenated lemma keeps
    its hyphen through :func:`~trialerror.lexicon.normalize.norm_lemma`)
    while refusing one that cuts a longer word in half.
    """
    if pos <= 0 or pos >= len(normalized):
        return True
    return not (_is_word_char(normalized[pos - 1]) and _is_word_char(normalized[pos]))


def match_terms_in_text(store: Store, text: str) -> list[dict[str, Any]]:
    """Every term this store knows, found in ``text``, with the span of the
    ORIGINAL text each one occupies.

    Returns ``[{"term_id", "lemma", "span_start", "span_end", "sense_id"},
    ...]`` in text order. ``lemma`` is the term's own lemma, not the
    written form that matched it -- the written form is
    ``text[span_start:span_end]``, which is the half a caller cannot
    reconstruct. This is the seam ``AISPEAK_TRANSLATOR_DESIGN.md`` §4.3's
    ``glossary_hint_terms`` and ``feed_post_translation.glossary_links``
    were designed against (that table's column comment names the same four
    fields plus a definition id). **This lane ships the function; lane b
    wires it.**

    **Offsets are into the original text, and that is the whole difficulty
    of this function.** The lookup keys are normalized
    (:func:`~trialerror.lexicon.normalize.norm_lemma`) and normalization is
    not length-preserving -- a soft hyphen or a BOM disappears, ``ß``
    becomes two characters, a run of newlines becomes one space, a
    full-width digit narrows -- so an offset measured against a normalized
    copy is wrong against the original by an amount that grows with the
    document and cannot be recovered downstream. Of the two routes the
    design allows, this takes the first:
    :func:`~trialerror.lexicon.normalize.norm_with_offsets` folds the text
    with the identical pipeline and returns, per normalized character, the
    slice of the original that produced it. The second route -- tokenize
    first, normalize per token -- was rejected for a concrete reason: a
    tokenizer has to decide what a token is *before* normalization has
    folded anything, so a soft-hyphenated word splits in two and a
    hyphenated lemma either splits or swallows its neighbours, and both
    failures are invisible in the output.

    **No overlaps: longest wins, then leftmost.** Every boundary-legal
    match is collected first and the set is then taken longest-first,
    ties broken leftmost, skipping anything that overlaps a match already
    taken. That is the design's rule read literally, and it differs from
    the cheaper leftmost-greedy scan in the case that matters: with terms
    ``a b`` and ``b c d`` over the text ``a b c d``, greedy takes the short
    one because it starts first and loses the longer term entirely; this
    takes ``b c d``. A term store's whole value in this seam is that the
    longest name that fits is the one a reader means.

    **Word boundary**, per :func:`_boundary_at`: a term never matches
    inside a longer word.

    **Merged and retired terms are followed exactly as
    ``find_term(follow_merges=True)`` follows them** -- through the shared
    :func:`_follow_merges` walk, so a folded term's old lemma (kept as an
    alias of the canonical term, and kept as its own ``lemma_norm`` on the
    folded row) reports the canonical term's id and lemma. A retired term
    still matches: retirement says the program stopped using the name, not
    that a reader of an older document will stop meeting it, and a
    glossary link is exactly how that reader finds out.

    ``sense_id`` is the term's ``preferred_sense_id``, or ``None``. It is
    not backfilled from "some current sense" the way the dashboard's index
    fills its gloss column: the dashboard is showing a human something
    approximate, while this id is what a link would resolve to, and a link
    to an arbitrary one of several current readings is worse than a link
    to the term itself.

    Cost is one index build (see :func:`_lookup_index`) plus O(len(text) ×
    longest key) dict lookups.
    """
    normalized, spans = norm_with_offsets(text or "")
    if not normalized:
        return []
    index, longest = _lookup_index(store)
    if not index:
        return []

    hits: list[tuple[int, int, int, str]] = []
    size = len(normalized)
    for start in range(size):
        if normalized[start] == " " or not _boundary_at(normalized, start):
            continue
        for end in range(min(size, start + longest), start, -1):
            if not _boundary_at(normalized, end):
                continue
            hit = index.get(normalized[start:end])
            if hit is not None:
                hits.append((end - start, start, end, hit[0]))

    hits.sort(key=lambda h: (-h[0], h[1]))
    accepted: list[tuple[int, int, str]] = []
    for _length, start, end, term_id in hits:
        if any(start < taken_end and taken_start < end for taken_start, taken_end, _ in accepted):
            continue
        accepted.append((start, end, term_id))
    accepted.sort()

    resolved: dict[str, dict[str, Any] | None] = {}
    out: list[dict[str, Any]] = []
    for start, end, term_id in accepted:
        if term_id not in resolved:
            row = get_term(store, term_id)
            resolved[term_id] = _follow_merges(store, row) if row is not None else None
        term = resolved[term_id]
        if term is None:  # pragma: no cover - the index is built from term rows
            continue
        out.append(
            {
                "term_id": term["term_id"],
                "lemma": term["lemma"],
                "span_start": spans[start][0],
                "span_end": spans[end - 1][1],
                "sense_id": term.get("preferred_sense_id"),
            }
        )
    return out


def lookup_terms(store: Store, texts: Sequence[str]) -> list[dict[str, Any]]:
    """Which of ``texts`` are, as a whole, the name of an ``instance`` term.

    Returns ``[{"index", "text", "term_id", "lemma", "matched_by", "sense_id"},
    ...]`` -- one entry per input that resolves, none for the inputs that
    do not, which is why ``index`` (the position in ``texts``) is carried
    rather than left to the caller to line up. ``matched_by`` is
    ``"lemma"`` or ``"alias"``.

    This is AIIF v2's zero-LLM pre-pass for the KNOWN-MECHANIC check
    (design §5): a name the store already holds is recorded in the dossier
    as ``known_by_name``, **alongside the cosine rule and never replacing
    it**. The two answer different questions -- one asks whether this store
    has already named the thing, the other whether some described thing is
    the same thing -- and a name match is the cheap, deterministic, free
    half.

    **Whole-text exact match after ``norm_lemma``, never a substring.** An
    input is a candidate name, not prose: ``match_terms_in_text`` is the
    function for prose, and running substring logic here would let a
    six-word description of a novel mechanic count as "already known"
    because four of its words name something else.

    **``granularity='instance'`` only** (the vocabulary is
    :data:`trialerror.lexicon.policy.GRANULARITIES`). The filter is applied
    to the term the name resolves to *after* merges are followed, not to
    the row the key was found on, so a folded name reports the live term's
    level rather than the level of a row nothing reads any more. A
    ``family`` term -- the level-1 rows the register backfill's family map
    mints -- is deliberately not an answer here: "this is one of the
    stability behaviours" is not the claim that this mechanic is already
    known by name, and letting the family level answer would mark every
    member of a mapped family as known.

    At most one entry per input, for the reason :func:`_lookup_index`
    states: an alias spelling shared by two terms is an open duplicate
    candidate, and this pre-pass is not the place that decides it.
    """
    if isinstance(texts, (str, bytes)):
        raise InvalidTermInputError(
            "lookup_terms takes a sequence of texts, not one text -- a bare string would be "
            "iterated character by character and match nothing"
        )
    index, _longest = _lookup_index(store)
    out: list[dict[str, Any]] = []
    for position, text in enumerate(texts):
        key = norm_lemma(text if text is not None else "")
        if not key:
            continue
        hit = index.get(key)
        if hit is None:
            continue
        row = get_term(store, hit[0])
        if row is None:  # pragma: no cover - the index is built from term rows
            continue
        term = _follow_merges(store, row)
        if term.get("granularity") != "instance":
            continue
        out.append(
            {
                "index": position,
                "text": text,
                "term_id": term["term_id"],
                "lemma": term["lemma"],
                "matched_by": hit[1],
                "sense_id": term.get("preferred_sense_id"),
            }
        )
    return out


def _decode_tags(value: Any) -> list[str]:
    """``term.tags`` (JSON text) as a list of tag codes, tolerating
    anything else the column might hold.

    The column is written by :func:`propose` through ``json.dumps`` of
    whatever the caller passed, and the register backfill passes a list of
    codes. A row written around the API could hold a bare string or
    unparseable text; both come back as an empty list rather than as an
    exception, because a label read exists to say what is known about a
    term and "the tags column is malformed" is the doctor's finding to
    make, not this function's.
    """
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, (list, tuple)):
            return [str(v) for v in parsed if str(v).strip()]
    return []


def covered_region_labels(store: Store, term_ids: Sequence[str]) -> dict[str, Any]:
    """The label vocabulary -- and only the label vocabulary -- for the
    terms named by ``term_ids``.

    Returns ``{"labels": [{"term_id", "granularity", "family_tags"}, ...],
    "missing": [...]}``. **Every label row carries those three keys and
    nothing else**: no gloss, no evidence, no excerpt, no idea body. That
    is C-0082(3)'s line, and it is the entire point of this function
    existing separately from :func:`get_term` -- a generator being told
    which regions of the space are already covered may see the coverage
    labels and must not see the readings, because a reading is the
    program's own words about a source and handing it to a generator that
    is about to produce a novelty verdict contaminates the verdict.

    ``family_tags`` is the term's ``tags`` list as the schema stores it.
    That is where a family membership lives: the register backfill files
    every instance term under its tag codes, and ruling L-E3's level-1
    family map mints one ``family`` term per tag carrying that same code,
    so the tag is the join between the two levels. There is no relation
    verb for membership -- :data:`trialerror.lexicon.policy.RELATION_VERBS`
    is about sameness, conflict and scope -- so reading tags is not a
    shortcut past a graph walk; it is the graph.

    **Unknown ids are reported, never raised.** They come back in
    ``missing``, in the order given, because the caller is a batch pass
    over ids it collected elsewhere and one stale id should cost that pass
    one label, not the whole run. Ids repeated in the input are answered
    once.

    Merges are deliberately **not** followed here, unlike in the two
    lookups above. Those two are asked "what does this name mean now" and
    must answer with the live term; this one is asked "what are the labels
    on this row" about an id the caller already resolved (through
    :func:`lookup_terms` or :func:`match_terms_in_text`, both of which
    follow merges). Answering with a different term's labels under the id
    that was asked about would be the one shape of wrong a label read
    cannot afford.
    """
    if isinstance(term_ids, (str, bytes)):
        raise InvalidTermInputError(
            "covered_region_labels takes a sequence of term ids, not one id -- a bare string "
            "would be iterated character by character"
        )
    labels: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for term_id in term_ids:
        if not term_id or term_id in seen:
            continue
        seen.add(term_id)
        term = get_term(store, term_id)
        if term is None:
            missing.append(term_id)
            continue
        labels.append(
            {
                "term_id": term["term_id"],
                "granularity": term.get("granularity"),
                "family_tags": _decode_tags(term.get("tags")),
            }
        )
    return {"labels": labels, "missing": missing}


# ---------------------------------------------------------------------------
# FTS maintenance
# ---------------------------------------------------------------------------


def reindex_term(store: Store, term_id: str) -> int:
    """Rebuild ``term_fts`` for one term. Returns the row count written.

    The index carries **one row per term** (its lemma plus every current
    gloss, so a trigram query hits a term through its definition as well as
    its name) **plus one row per alias**. That shape is what makes the
    ``term_fts_in_sync`` doctor check a plain count comparison against
    ``term`` + ``term_alias`` rather than a text diff -- drift in an
    API-maintained index is a bug, and the cheapest true check is the one
    that will actually be run.

    Written with a direct ``execute`` rather than through
    ``trialerror.stores.writer.insert``: ``term_fts`` is a virtual table and
    therefore deliberately absent from the schema module's ``TABLES``, so
    the write API cannot (and should not) route to it -- the same treatment
    ``chunk_fts`` gets from ``trialerror.retrieve``."""
    conn = _conn(store)
    term = get_term(store, term_id)
    with conn:
        conn.execute("DELETE FROM term_fts WHERE term_id = ?", (term_id,))
        if term is None:
            return 0
        glosses = [
            r[0]
            for r in conn.execute(
                "SELECT gloss FROM term_sense WHERE term_id = ? AND status = 'current' "
                "ORDER BY created_at, sense_id",
                (term_id,),
            )
        ]
        conn.execute(
            "INSERT INTO term_fts(term_id, text) VALUES (?, ?)",
            (term_id, fts_text(term["lemma"], *glosses)),
        )
        written = 1
        for (alias,) in conn.execute(
            "SELECT alias FROM term_alias WHERE term_id = ? ORDER BY created_ts, alias_id",
            (term_id,),
        ).fetchall():
            conn.execute("INSERT INTO term_fts(term_id, text) VALUES (?, ?)", (term_id, fts_text(alias)))
            written += 1
    return written


def reindex_all(store: Store) -> dict[str, int]:
    """Rebuild the whole index (the ``trialerror term reindex`` verb, E3).
    Returns ``{"terms": n, "rows": m}``."""
    conn = _conn(store)
    term_ids = [r[0] for r in conn.execute("SELECT term_id FROM term ORDER BY term_id").fetchall()]
    with conn:
        conn.execute("DELETE FROM term_fts")
    rows = sum(reindex_term(store, tid) for tid in term_ids)
    return {"terms": len(term_ids), "rows": rows}


# ---------------------------------------------------------------------------
# the two scans -- import-guarded, and honest about their own absence
# ---------------------------------------------------------------------------


def _run_candidate_surfacing(
    store: Store,
    term_id: str,
    *,
    sense_id: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from trialerror.lexicon.candidates import surface_candidates
    except ImportError:  # pragma: no cover - the module ships with the package
        return {
            "status": "unavailable",
            "reason": "trialerror.lexicon.candidates is not importable",
        }
    # `config` reaches the scan from here on (build step 1c): the duplicate
    # gate's floors are program-overridable knobs like the gloss cap, and a
    # save-time scan that read the defaults while the program's own
    # `[lexicon]` table said otherwise would open rows the same store's
    # `term scan` would not.
    return surface_candidates(store, term_id, sense_id=sense_id, config=config)


def _run_conflict_scan(store: Store, term_id: str) -> dict[str, Any]:
    try:
        from trialerror.lexicon.scan import conflicts_for_term
    except ImportError:  # pragma: no cover - the module ships with the package
        return {
            "status": "unavailable",
            "reason": "trialerror.lexicon.scan is not importable",
        }
    return conflicts_for_term(store, term_id)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _coerce_evidence_spec(spec: Any) -> dict[str, Any]:
    """Accept either ``"kind:id"`` (the CLI's ``--evidence`` token) or a
    mapping, and return the normalized field set."""
    if isinstance(spec, str):
        kind, sep, ref = spec.partition(":")
        if not sep or not ref.strip():
            raise InvalidEvidenceError(
                f"evidence token {spec!r} must be '<kind>:<id>' "
                f"(kinds: anchor/quote_anchor, record, claim, idea)"
            )
        spec = {"kind": kind.strip(), "ref": ref.strip()}
    if not isinstance(spec, Mapping):
        raise InvalidEvidenceError(f"evidence entry must be a 'kind:id' string or a mapping, got {type(spec).__name__}")

    kind = str(spec.get("kind") or spec.get("evidence_kind") or "").strip()
    kind = _EVIDENCE_KIND_ALIASES.get(kind, kind)
    if kind not in policy.EVIDENCE_KINDS:
        raise InvalidEvidenceError(
            f"evidence_kind={kind!r} is not one of {list(policy.EVIDENCE_KINDS)!r} "
            "('anchor' is accepted as a spelling of 'quote_anchor')"
        )

    ref = spec.get("ref")
    anchor_id = spec.get("anchor_id")
    ref_id = spec.get("ref_id")
    if kind == "quote_anchor":
        anchor_id = anchor_id or ref
        ref_id = None
    else:
        ref_id = ref_id or ref
        anchor_id = None
    identity = anchor_id if kind == "quote_anchor" else ref_id
    if not identity:
        raise InvalidEvidenceError(f"{kind} evidence needs an id ({'anchor_id' if kind == 'quote_anchor' else 'ref_id'})")

    return {
        "evidence_kind": kind,
        "anchor_id": anchor_id,
        "ref_id": ref_id,
        "source_key": spec.get("source_key"),
        "cite_raw": spec.get("cite_raw"),
        "excerpt": spec.get("excerpt"),
    }


def _evidence_identity(spec: Mapping[str, Any]) -> tuple[str, Any]:
    """The identity ``UNIQUE(sense_id, evidence_kind, COALESCE(anchor_id,
    ref_id))`` counts -- the same pair ``decide_relation``'s evidence-union
    arm uses to decide whether a row it is copying is already there."""
    return (str(spec["evidence_kind"]), spec.get("anchor_id") or spec.get("ref_id"))


def _dedupe_evidence_specs(specs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeats of one evidence identity, keeping the first.

    Naming the same anchor twice in one ``evidence=[...]`` list is a caller
    saying one thing twice, not two grounds -- and the partial unique index
    would refuse the second INSERT *after* the term and the sense had already
    been committed by their own transactions, leaving exactly the
    half-written state finding F2 is about. Collapsing here rather than
    raising keeps a duplicated CLI ``--evidence`` token from being a refusal:
    the store ends up with what the caller meant either way.
    """
    seen: set[tuple[str, Any]] = set()
    out: list[dict[str, Any]] = []
    for spec in specs:
        identity = _evidence_identity(spec)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(dict(spec))
    return out


def _derive_source_key(store: Store, spec: Mapping[str, Any]) -> str:
    """The source identity for one evidence row -- supplied, or derived from
    the row it points at. Refuses rather than inventing one: see the module
    docstring for why an unnamed source would corrupt every later conflict
    verdict."""
    supplied = spec.get("source_key")
    if supplied:
        return str(supplied)

    conn = _conn(store)
    kind = spec["evidence_kind"]
    if kind == "quote_anchor":
        row = conn.execute(
            "SELECT d.source_id FROM quote_anchor a JOIN document d ON d.doc_id = a.doc_id "
            "WHERE a.anchor_id = ?",
            (spec["anchor_id"],),
        ).fetchone()
        if row is None:
            raise InvalidEvidenceError(
                f"quote_anchor {spec['anchor_id']!r} has no row (or no document behind it); "
                "cannot derive a source_key"
            )
        return str(row[0])
    if kind == "claim":
        row = conn.execute(
            "SELECT d.source_id FROM claim c "
            "JOIN quote_anchor a ON a.anchor_id = c.anchor_id "
            "JOIN document d ON d.doc_id = a.doc_id WHERE c.claim_id = ?",
            (spec["ref_id"],),
        ).fetchone()
        if row is None:
            raise InvalidEvidenceError(
                f"claim {spec['ref_id']!r} has no row (or no anchored document behind it); "
                "cannot derive a source_key"
            )
        return str(row[0])
    if kind == "record":
        row = conn.execute("SELECT register_key FROM record WHERE record_id = ?", (spec["ref_id"],)).fetchone()
        if row is None:
            raise InvalidEvidenceError(f"record {spec['ref_id']!r} has no row; cannot derive a source_key")
        return str(row[0])
    # idea: no source behind it -- the coinage is its own source. See the
    # module docstring for why this is not folded into one shared key.
    row = conn.execute("SELECT idea_id FROM idea WHERE idea_id = ?", (spec["ref_id"],)).fetchone()
    if row is None:
        raise InvalidEvidenceError(f"idea {spec['ref_id']!r} has no row; cannot derive a source_key")
    return str(row[0])


def _insert_evidence(
    store: Store, sense_id: str, spec: Mapping[str, Any], *, by_launch: str, ts: str
) -> dict[str, Any]:
    row = {
        "evidence_id": new_id("TSE"),
        "sense_id": sense_id,
        "evidence_kind": spec["evidence_kind"],
        "anchor_id": spec.get("anchor_id"),
        "ref_id": spec.get("ref_id"),
        "source_key": _derive_source_key(store, spec),
        "cite_raw": spec.get("cite_raw"),
        "excerpt": spec.get("excerpt"),
        "created_by_launch": by_launch,
        "created_ts": ts,
    }
    return insert(store, "term_sense_evidence", row)


def retract_evidence(store: Store, evidence_id: str, *, by_launch: str, reason: str) -> dict[str, Any]:
    """The raw layer's ONLY mutation, and it is not a delete.

    An evidence row that should not have counted gets a ``retracted_ts`` and
    a reason; the row stays, readable, forever. Every read that feeds a
    judgment (``source_keys_for_sense``, the conflict rule, the accept-time
    grounding check) excludes retracted rows, so the effect is complete --
    but the record of what the program once believed, and when it stopped,
    is not erased along with it.

    **It refuses to leave a live reading standing on nothing** (fix pass,
    finding F1). Retracting the LAST non-retracted row under a ``proposed``
    or ``current`` sense used to succeed silently: the sense stayed
    ``current``, the term stayed ``active`` with ``preferred_sense_id``
    pointing at it, and the Lexicon index went on rendering a gloss with
    nothing underneath -- one public-API call taking the fail-level
    ``term_sense_without_evidence`` check from 0 to 1, on an invariant the
    package docstring calls structurally unreachable. It is a refusal now,
    naming the sense and the verb that does mean what the caller wanted:
    ``reject_sense`` for a proposal, ``retire_sense`` or ``supersede_sense``
    for a current reading. Retracting is a statement about one row; ending a
    reading is a decision about the term, and this API does not make the
    second one on the strength of the first.

    The scope is :data:`trialerror.lexicon.policy.LIVE_SENSE_STATUSES` and
    not "every non-rejected sense": a superseded or retired reading is
    history, its successor already carries the evidence forward, and
    refusing there would be a dead end with no verb to escape through.
    """
    if not reason or not str(reason).strip():
        raise InvalidEvidenceError("retracting evidence requires a reason -- the row survives, so the why must too")
    _require_launch(store, by_launch, what="retract_evidence")
    row = get(store, "term_sense_evidence", pk_column="evidence_id", pk_value=evidence_id)
    if row is None:
        raise InvalidEvidenceError(f"no such evidence row: {evidence_id!r}")
    if row["retracted_ts"]:
        return {"evidence_id": evidence_id, "retracted": False, "reason": row["retracted_reason"]}

    sense = get_sense(store, row["sense_id"]) or {}
    live = _live_evidence(store, row["sense_id"])
    if (
        sense.get("status") in policy.LIVE_SENSE_STATUSES
        and len(live) == 1
        and live[0]["evidence_id"] == evidence_id
    ):
        verb = "reject_sense" if sense["status"] == "proposed" else "retire_sense / supersede_sense"
        raise SenseWithoutEvidenceError(
            f"retracting {evidence_id!r} would leave {sense['status']} sense {row['sense_id']!r} with no "
            f"live evidence, and a live reading always stands on something -- end the reading first "
            f"({verb}), then the retraction is recorded against a reading that no longer claims to be "
            "grounded"
        )

    ts = now()
    update(
        store,
        "term_sense_evidence",
        pk_column="evidence_id",
        pk_value=evidence_id,
        changes={"retracted_ts": ts, "retracted_reason": str(reason)},
    )
    append_event(
        store,
        event_type="term_evidence_retracted",
        payload={"evidence_id": evidence_id, "sense_id": row["sense_id"], "reason": str(reason)},
        launch_id=by_launch,
        ts=ts,
    )
    return {"evidence_id": evidence_id, "sense_id": row["sense_id"], "retracted": True, "retracted_ts": ts}


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------


def _coerce_alias_spec(spec: Any) -> tuple[str, str]:
    if isinstance(spec, str):
        return spec, "variant"
    if isinstance(spec, Mapping):
        return str(spec.get("alias") or ""), str(spec.get("kind") or "variant")
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        return str(spec[0]), str(spec[1])
    raise InvalidTermInputError(f"alias entry must be a string, a (alias, kind) pair, or a mapping, got {spec!r}")


def _add_alias(store: Store, term_id: str, alias: str, kind: str, *, by_launch: str, ts: str) -> str | None:
    """Attach one alias, or return ``None`` if the key is already taken.

    A collision is not an error: two routes proposing the same spelling of
    the same term is the normal case, and ``UNIQUE(term_id, alias_norm)``
    already says the second one adds nothing."""
    _require_vocab(kind, policy.ALIAS_KINDS, field="alias kind")
    alias_norm = norm_lemma(alias)
    if not alias_norm:
        return None
    conn = _conn(store)
    term = get_term(store, term_id)
    if term is not None and term["lemma_norm"] == alias_norm:
        return None
    exists = conn.execute(
        "SELECT 1 FROM term_alias WHERE term_id = ? AND alias_norm = ? LIMIT 1", (term_id, alias_norm)
    ).fetchone()
    if exists is not None:
        return None
    alias_id = new_id("ALIAS")
    insert(
        store,
        "term_alias",
        {
            "alias_id": alias_id,
            "term_id": term_id,
            "alias": str(alias).strip(),
            "alias_norm": alias_norm,
            "kind": kind,
            "created_by_launch": by_launch,
            "created_ts": ts,
        },
    )
    return alias_id


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


def propose(
    store: Store,
    *,
    lemma: str,
    gloss: str,
    origin_kind: str,
    origin_ref: str | None,
    evidence: Iterable[Any],
    by_launch: str,
    procedure_version: str,
    granularity: str | None = None,
    tags: Any = None,
    aliases: Iterable[Any] = (),
    confidence: float | None = None,
    disambiguator: str | None = None,
    entity_id: str | None = None,
    status: str = "proposed",
    valid_at: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose one reading of one lemma, with its evidence.

    The lemma is resolved through the full lookup surface (``lemma_norm``
    then ``alias_norm``), so proposing against an existing term's alias
    attaches to that term rather than creating a near-duplicate.

    ``status`` may be ``'proposed'`` (the default; a human or a later call
    decides) or ``'current'`` -- the two routes design §4 lets land already
    accepted, because their gate has already happened elsewhere: an
    extraction claim the merge-review queue just accepted, and a register
    import whose denominator was gated once (ruling L-E2). ``'current'``
    here does **not** write a different status; it runs
    :func:`accept_sense` on the row it just wrote, so the evidence check,
    the ``review_after`` stamp, the ``prov_edge`` and the events are
    identical either way. That is invariant 1 of the package docstring
    implemented as one code path rather than as two that agree today.

    **Idempotent on ``(origin_kind, origin_ref)``** -- guaranteed by the
    partial unique index, not by this check: re-running a backfill returns
    the existing sense with ``created_sense=False`` and writes nothing.
    Proposals with no ``origin_ref`` (the manual route) are outside that
    guarantee by construction, since there is no origin to be idempotent
    about.

    ``evidence`` is a non-empty iterable of ``"kind:id"`` tokens or
    mappings; an empty one raises
    :class:`~trialerror.lexicon.errors.SenseWithoutEvidenceError` before
    any row is written.
    """
    _require_launch(store, by_launch, what="propose")
    _require_vocab(origin_kind, policy.ORIGIN_KINDS, field="origin_kind")
    _require_vocab(granularity, policy.GRANULARITIES, field="granularity", nullable=True)
    if status not in ("proposed", "current"):
        raise InvalidTermInputError(
            f"status={status!r}: propose may land 'proposed' or 'current' only "
            "(the other three sense statuses are reached by deciding, superseding or retiring)"
        )
    gloss_text = _require_gloss(gloss, origin_kind=origin_kind, config=config)
    # The emptiness test is made against the POST-normalization key, not the
    # written form: a lemma of nothing but zero-width format characters reads
    # as a name on the page and normalizes to "" (fix pass, finding F4), and
    # a term whose every cell renders empty is not a lemma the store can be
    # asked about later.
    lemma_norm = norm_lemma(lemma)
    if not lemma_norm:
        raise InvalidTermInputError(
            f"lemma {lemma!r} normalizes to an empty key -- a lemma must be a non-empty name "
            "(whitespace and zero-width format characters are not one)"
        )

    specs = [_coerce_evidence_spec(e) for e in evidence]
    if not specs:
        raise SenseWithoutEvidenceError(
            f"a sense of {lemma!r} needs at least one evidence row -- the quote-grounding law, "
            "applied to the lexicon"
        )
    # Resolve every source_key BEFORE the term is created, not lazily inside
    # the evidence insert. A reference that names no row is a refusal, and a
    # refusal that arrives three writes in leaves an orphan term with an
    # orphan sense and no evidence -- exactly the state the grounding law
    # exists to make impossible.
    for spec in specs:
        spec["source_key"] = _derive_source_key(store, spec)
    specs = _dedupe_evidence_specs(specs)

    # Same reason, applied to the aliases (fix pass, finding F2). The alias
    # vocabulary used to be checked inside _add_alias, which runs after the
    # term, the sense and every evidence row are committed and before the
    # reindex and both events -- so a one-character typo in an alias kind
    # raised a refusal meaning "nothing happened" over a store that had
    # gained three rows no event recorded and no term_fts row covered. Pure
    # validation belongs here, beside the two checks above that already run
    # up front for exactly this reason.
    alias_specs: list[tuple[str, str]] = []
    for alias_spec in aliases:
        alias, alias_kind = _coerce_alias_spec(alias_spec)
        _require_vocab(alias_kind, policy.ALIAS_KINDS, field="alias kind")
        alias_specs.append((alias, alias_kind))

    # Idempotency, checked before anything is created so a re-run leaves no
    # orphan term behind it.
    if origin_ref:
        existing = _conn(store).execute(
            "SELECT * FROM term_sense WHERE origin_kind = ? AND origin_ref = ?", (origin_kind, origin_ref)
        ).fetchone()
        if existing is not None:
            existing = dict(existing)
            term = get_term(store, existing["term_id"]) or {}
            return {
                "term_id": existing["term_id"],
                "sense_id": existing["sense_id"],
                "lemma": term.get("lemma"),
                "lemma_norm": term.get("lemma_norm"),
                "status": existing["status"],
                "term_status": term.get("status"),
                "created_term": False,
                "created_sense": False,
                "evidence_ids": [e["evidence_id"] for e in _live_evidence(store, existing["sense_id"])],
                "alias_ids": [],
                "candidates": {"status": "skipped", "reason": "idempotent re-propose; nothing was written"},
                "conflicts": None,
            }

    ts = now()
    term = find_term(store, lemma)
    created_term = term is None
    if term is None:
        term_id = new_id("TERM")
        insert(
            store,
            "term",
            {
                "term_id": term_id,
                "lemma": str(lemma).strip(),
                "lemma_norm": lemma_norm,
                "granularity": granularity,
                "tags": _json_or_none(tags),
                "entity_id": entity_id,
                "status": "proposed",
                "preferred_sense_id": None,
                "merged_into": None,
                "created_by_launch": by_launch,
                "created_at": ts,
                "updated_ts": ts,
            },
        )
        term = get_term(store, term_id) or {}
    else:
        term_id = term["term_id"]
        # An existing term learns what this proposal knows and it did not,
        # and nothing else: a later proposal never overwrites a decided
        # granularity, a tag set or an entity link.
        learned: dict[str, Any] = {}
        if granularity and not term.get("granularity"):
            learned["granularity"] = granularity
        if tags is not None and not term.get("tags"):
            learned["tags"] = _json_or_none(tags)
        if entity_id and not term.get("entity_id"):
            learned["entity_id"] = entity_id
        if learned:
            learned["updated_ts"] = ts
            update(store, "term", pk_column="term_id", pk_value=term_id, changes=learned)
            term = get_term(store, term_id) or term

    sense_id = new_id("SENSE")
    assert_fact(
        store,
        "term_sense",
        {
            "sense_id": sense_id,
            "term_id": term_id,
            "gloss": gloss_text,
            "disambiguator": disambiguator,
            "origin_kind": origin_kind,
            "origin_ref": origin_ref,
            "confidence": confidence,
            "procedure_version": procedure_version,
            "status": "proposed",
            "created_at": ts,
            "proposed_by_launch": by_launch,
        },
        valid_at=valid_at,
    )

    evidence_ids = [_insert_evidence(store, sense_id, s, by_launch=by_launch, ts=ts)["evidence_id"] for s in specs]
    alias_ids: list[str] = []
    for alias, alias_kind in alias_specs:
        alias_id = _add_alias(store, term_id, alias, alias_kind, by_launch=by_launch, ts=ts)
        if alias_id:
            alias_ids.append(alias_id)

    reindex_term(store, term_id)

    if created_term:
        append_event(
            store,
            event_type="term_proposed",
            payload={"term_id": term_id, "lemma": term.get("lemma"), "granularity": granularity},
            launch_id=by_launch,
            ts=ts,
        )
    append_event(
        store,
        event_type="term_sense_proposed",
        payload={
            "term_id": term_id,
            "sense_id": sense_id,
            "origin_kind": origin_kind,
            "origin_ref": origin_ref,
            "procedure_version": procedure_version,
            "evidence_count": len(evidence_ids),
        },
        launch_id=by_launch,
        ts=ts,
    )

    result: dict[str, Any] = {
        "term_id": term_id,
        "sense_id": sense_id,
        "lemma": term.get("lemma"),
        "lemma_norm": lemma_norm,
        "status": "proposed",
        "term_status": (get_term(store, term_id) or {}).get("status"),
        "created_term": created_term,
        "created_sense": True,
        "evidence_ids": evidence_ids,
        "alias_ids": alias_ids,
        "conflicts": None,
    }
    result["candidates"] = _run_candidate_surfacing(store, term_id, sense_id=sense_id, config=config)

    if status == "current":
        accepted = accept_sense(store, sense_id, by_launch=by_launch, config=config)
        result["status"] = accepted["status"]
        result["term_status"] = accepted["term_status"]
        result["review_after"] = accepted["review_after"]
        result["conflicts"] = accepted["conflicts"]
    return result


# ---------------------------------------------------------------------------
# sense lifecycle
# ---------------------------------------------------------------------------


def _load_sense(store: Store, sense_id: str) -> dict[str, Any]:
    sense = get_sense(store, sense_id)
    if sense is None:
        raise SenseNotFoundError(f"no such sense: {sense_id!r}")
    return sense


def _prov_edge(
    store: Store, *, role: str, src_kind: str, src_id: str, dst_kind: str, dst_id: str, launch_id: str, ts: str
) -> str:
    """Ruling L-E5: the lexicon is the provenance graph's first writer, for
    lexicon lineage only (``derived_from`` from a sense to the row it was
    read out of, ``supersedes`` between two senses, ``contradicts`` between
    the members of a decided split). No other subsystem gains a
    ``prov_edge`` writer by that ruling, and nothing here writes an edge
    whose two endpoints are not both lexicon rows or a lexicon row and its
    own origin."""
    edge_id = new_id("EDGE")
    insert(
        store,
        "prov_edge",
        {
            "edge_id": edge_id,
            "src_kind": src_kind,
            "src_id": src_id,
            "dst_kind": dst_kind,
            "dst_id": dst_id,
            "role": role,
            "launch_id": launch_id,
            "ts": ts,
        },
    )
    return edge_id


def accept_sense(
    store: Store, sense_id: str, *, by_launch: str, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """``proposed -> current``: the evidenced route, and the only one.

    Stamps the decision, sets the ``review_after`` decay window for the
    sense's origin kind, promotes a still-``proposed`` term to ``active``,
    adopts the sense as the term's preferred one if it has none, writes the
    ``derived_from`` provenance edge back to the row this reading came out
    of, and then re-runs the conflict scan for the term -- because a term
    with a second current sense is exactly when a conflict can appear.
    """
    _require_launch(store, by_launch, what="accept_sense")
    sense = _load_sense(store, sense_id)
    if sense["status"] != "proposed":
        raise SenseNotDecidableError(
            f"sense {sense_id!r} is {sense['status']!r}, not 'proposed' -- a decided sense is "
            "superseded or retired in the open, never re-decided"
        )
    if not _live_evidence(store, sense_id):
        raise SenseWithoutEvidenceError(
            f"sense {sense_id!r} has no live evidence (all of it retracted?); it cannot become current"
        )

    ts = now()
    review_after = policy.review_after_for(sense["origin_kind"], at=ts, policy=policy.load_policy(config))
    update(
        store,
        "term_sense",
        pk_column="sense_id",
        pk_value=sense_id,
        changes={
            "status": "current",
            "decided_by_launch": by_launch,
            "decided_ts": ts,
            "review_after": review_after,
        },
    )

    term_id = sense["term_id"]
    term = get_term(store, term_id) or {}
    term_changes: dict[str, Any] = {"updated_ts": ts}
    if term.get("status") == "proposed":
        term_changes["status"] = "active"
    if not term.get("preferred_sense_id"):
        term_changes["preferred_sense_id"] = sense_id
    update(store, "term", pk_column="term_id", pk_value=term_id, changes=term_changes)

    edge_id = None
    origin_table = policy.ORIGIN_REF_TABLE.get(sense["origin_kind"])
    if origin_table and sense["origin_ref"]:
        edge_id = _prov_edge(
            store,
            role="derived_from",
            src_kind="term_sense",
            src_id=sense_id,
            dst_kind=origin_table,
            dst_id=sense["origin_ref"],
            launch_id=by_launch,
            ts=ts,
        )

    reindex_term(store, term_id)
    append_event(
        store,
        event_type="term_sense_accepted",
        payload={
            "term_id": term_id,
            "sense_id": sense_id,
            "origin_kind": sense["origin_kind"],
            "review_after": review_after,
            "prov_edge": edge_id,
        },
        launch_id=by_launch,
        ts=ts,
    )
    return {
        "term_id": term_id,
        "sense_id": sense_id,
        "status": "current",
        "term_status": (get_term(store, term_id) or {}).get("status"),
        "review_after": review_after,
        "prov_edge": edge_id,
        "conflicts": _run_conflict_scan(store, term_id),
    }


def reject_sense(
    store: Store, sense_id: str, *, by_launch: str, reason: str | None = None
) -> dict[str, Any]:
    """``proposed -> rejected``. The evidence rows are KEPT: the program
    looked at this reading and said no, and the material it said no to is
    part of that record."""
    _require_launch(store, by_launch, what="reject_sense")
    sense = _load_sense(store, sense_id)
    if sense["status"] != "proposed":
        raise SenseNotDecidableError(f"sense {sense_id!r} is {sense['status']!r}, not 'proposed'")
    ts = now()
    update(
        store,
        "term_sense",
        pk_column="sense_id",
        pk_value=sense_id,
        changes={"status": "rejected", "decided_by_launch": by_launch, "decided_ts": ts},
    )
    reindex_term(store, sense["term_id"])
    append_event(
        store,
        event_type="term_sense_rejected",
        payload={"term_id": sense["term_id"], "sense_id": sense_id, "reason": reason},
        launch_id=by_launch,
        ts=ts,
    )
    return {"term_id": sense["term_id"], "sense_id": sense_id, "status": "rejected", "reason": reason}


def supersede_sense(
    store: Store,
    sense_id: str,
    *,
    gloss: str,
    by_launch: str,
    disambiguator: str | None = None,
    procedure_version: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A corrected reading: assert the replacement, expire the old one, copy
    the evidence forward.

    Two details that are consequences of the schema rather than choices
    made here.

    *The replacement carries no ``origin_ref``.* The partial unique index
    on ``(origin_kind, origin_ref)`` is what makes every proposal route
    idempotent, and it means an origin belongs to the one row that was
    actually derived from it. The correction's lineage runs through
    ``superseded_by`` and a ``prov_edge(role='supersedes')`` instead, which
    is the more honest statement anyway: this row was derived from the
    previous reading plus a judgment, not from the source a second time.

    *The evidence is copied, not moved.* The raw layer is append-only, so
    the old sense keeps its rows and the new sense gets new rows pointing
    at the same anchors. Reading the old sense a year from now still shows
    what it stood on.
    """
    _require_launch(store, by_launch, what="supersede_sense")
    old = _load_sense(store, sense_id)
    if old["status"] != "current":
        raise SenseNotDecidableError(
            f"sense {sense_id!r} is {old['status']!r}; only a 'current' reading can be superseded"
        )
    gloss_text = _require_gloss(gloss, config=config)

    live = _live_evidence(store, sense_id)
    if not live:
        raise SenseWithoutEvidenceError(
            f"sense {sense_id!r} has no live evidence to carry forward; retract-and-repropose instead"
        )

    ts = now()
    new_sense_id = new_id("SENSE")
    review_after = policy.review_after_for(old["origin_kind"], at=ts, policy=policy.load_policy(config))
    supersede_fact(
        store,
        "term_sense",
        sense_id,
        {
            "term_id": old["term_id"],
            "gloss": gloss_text,
            "disambiguator": disambiguator if disambiguator is not None else old["disambiguator"],
            "origin_kind": old["origin_kind"],
            "origin_ref": None,
            "confidence": confidence if confidence is not None else old["confidence"],
            "procedure_version": procedure_version or old["procedure_version"],
            "status": "current",
            "proposed_by_launch": by_launch,
            "decided_by_launch": by_launch,
            "decided_ts": ts,
            "review_after": review_after,
        },
        new_id_column="sense_id",
        new_id_value=new_sense_id,
        tx_at=ts,
    )
    update(
        store,
        "term_sense",
        pk_column="sense_id",
        pk_value=sense_id,
        changes={"status": "superseded", "decided_by_launch": by_launch, "decided_ts": ts},
    )
    carried = [
        _insert_evidence(store, new_sense_id, dict(row), by_launch=by_launch, ts=ts)["evidence_id"]
        for row in live
    ]

    term = get_term(store, old["term_id"]) or {}
    term_changes: dict[str, Any] = {"updated_ts": ts}
    if term.get("preferred_sense_id") == sense_id:
        term_changes["preferred_sense_id"] = new_sense_id
    update(store, "term", pk_column="term_id", pk_value=old["term_id"], changes=term_changes)

    edge_id = _prov_edge(
        store,
        role="supersedes",
        src_kind="term_sense",
        src_id=new_sense_id,
        dst_kind="term_sense",
        dst_id=sense_id,
        launch_id=by_launch,
        ts=ts,
    )
    reindex_term(store, old["term_id"])
    append_event(
        store,
        event_type="term_sense_superseded",
        payload={
            "term_id": old["term_id"],
            "old_sense_id": sense_id,
            "sense_id": new_sense_id,
            "evidence_carried": len(carried),
            "reason": reason,
        },
        launch_id=by_launch,
        ts=ts,
    )
    return {
        "term_id": old["term_id"],
        "sense_id": new_sense_id,
        "superseded_sense_id": sense_id,
        "status": "current",
        "evidence_ids": carried,
        "review_after": review_after,
        "prov_edge": edge_id,
    }


def retire_sense(
    store: Store, sense_id: str, *, by_launch: str, reason: str | None = None, event_at: str | None = None
) -> dict[str, Any]:
    """The reading stopped being used, and the store says so instead of
    deleting it: ``invalid_at`` closes the EVENT-time window (the fact
    itself ended) while the row stays the DB's belief about what was once
    true, and ``status`` becomes ``retired``.

    The event type ``term_sense_retired`` is one more than the design's §3
    list names. Retiring is a head-layer change like the other five, and
    the head has to be rebuildable from the event log; an omitted event
    would be the one hole in that.
    """
    _require_launch(store, by_launch, what="retire_sense")
    sense = _load_sense(store, sense_id)
    if sense["status"] not in ("current", "proposed"):
        raise SenseNotDecidableError(
            f"sense {sense_id!r} is {sense['status']!r}; only a live reading can be retired"
        )
    ts = now()
    end_fact_validity(store, "term_sense", sense_id, event_at=event_at or ts)
    update(
        store,
        "term_sense",
        pk_column="sense_id",
        pk_value=sense_id,
        changes={"status": "retired", "decided_by_launch": by_launch, "decided_ts": ts},
    )

    term_id = sense["term_id"]
    term = get_term(store, term_id) or {}
    term_changes: dict[str, Any] = {"updated_ts": ts}
    if term.get("preferred_sense_id") == sense_id:
        remaining = senses_for_term(store, term_id, statuses=("current",))
        term_changes["preferred_sense_id"] = remaining[0]["sense_id"] if remaining else None
    update(store, "term", pk_column="term_id", pk_value=term_id, changes=term_changes)

    reindex_term(store, term_id)
    append_event(
        store,
        event_type="term_sense_retired",
        payload={"term_id": term_id, "sense_id": sense_id, "reason": reason},
        launch_id=by_launch,
        ts=ts,
    )
    return {"term_id": term_id, "sense_id": sense_id, "status": "retired", "reason": reason}


def mark_reviewed(
    store: Store, sense_id: str, *, by_launch: str, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """"I looked at this and it is still right": stamp ``reviewed_ts`` and
    push ``review_after`` out by the origin kind's window again. Nothing
    else moves -- the point of the decay flag is that reviewing is cheap and
    changes no status."""
    _require_launch(store, by_launch, what="mark_reviewed")
    sense = _load_sense(store, sense_id)
    if sense["status"] != "current":
        raise SenseNotDecidableError(
            f"sense {sense_id!r} is {sense['status']!r}; only a current reading carries a review window"
        )
    ts = now()
    review_after = policy.review_after_for(sense["origin_kind"], at=ts, policy=policy.load_policy(config))
    update(
        store,
        "term_sense",
        pk_column="sense_id",
        pk_value=sense_id,
        changes={"reviewed_ts": ts, "review_after": review_after},
    )
    append_event(
        store,
        event_type="term_reviewed",
        payload={"term_id": sense["term_id"], "sense_id": sense_id, "review_after": review_after},
        launch_id=by_launch,
        ts=ts,
    )
    return {
        "term_id": sense["term_id"],
        "sense_id": sense_id,
        "reviewed_ts": ts,
        "review_after": review_after,
    }


# ---------------------------------------------------------------------------
# relations
# ---------------------------------------------------------------------------


def _relation_member_sense_ids(rel: Mapping[str, Any]) -> list[str]:
    """The senses a relation is about.

    A term-scoped conflict carries them in its ``evidence`` JSON (one queue
    item for a nine-way polysemous lemma, not thirty-six pairwise ones); a
    sense-scoped relation IS its two endpoints. Returns ``[]`` when neither
    applies, and the caller decides whether that is fatal."""
    raw = rel.get("evidence")
    if raw:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            ids = payload.get("sense_ids")
            if isinstance(ids, (list, tuple)) and ids:
                return [str(i) for i in ids]
    if rel.get("src_kind") == "sense" and rel.get("dst_kind") == "sense":
        return [str(rel["src_id"]), str(rel["dst_id"])]
    return []


def _load_decidable_members(
    store: Store, rel_id: str, members: Sequence[str], *, decision: str
) -> dict[str, dict[str, Any]]:
    """Re-read a relation's member senses and refuse a decision built on a
    stale snapshot (fix pass, finding F5).

    A conflict item's member ids are a snapshot the scan took when it opened
    the relation, carried in the row's ``evidence`` JSON. Nothing used to
    re-validate them at decision time, so a member retired or rejected in
    between could still be decided on: ``not_conflict --into`` a retired
    sense left the term ``active`` with ZERO current senses, a
    ``preferred_sense_id`` naming a dead reading, and a ``superseded_by``
    chain terminating on it -- after which ``conflicts_for_term`` reported
    "fewer than two grounded current readings" and never raised it again.
    ``scoped`` had the same hole from the other end: a rejected member got a
    disambiguator and its term was marked ``split``.

    This is the posture :class:`~trialerror.lexicon.errors.
    RelationNotPendingError` already takes toward a stale *relation*,
    extended to a stale *member set*. It also does the whole load up front,
    which is what makes the two multi-row arms below refuse before their
    first write rather than partway through it (finding F2)."""
    loaded: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    for sense_id in members:
        sense = _load_sense(store, sense_id)
        loaded[sense_id] = sense
        if sense["status"] != "current":
            stale.append(f"{sense_id} ({sense['status']})")
    if stale:
        raise SenseNotDecidableError(
            f"relation {rel_id!r} names member sense(s) that are no longer current: "
            f"{', '.join(stale)} -- {decision!r} would decide a question the store has already "
            "moved past. Re-run `trialerror term scan` and decide the item the fresh scan opens."
        )
    return loaded


def open_relation(
    store: Store,
    *,
    src_kind: str,
    src_id: str,
    dst_kind: str,
    dst_id: str,
    verb: str,
    marked_by_kind: str = "launch",
    marked_by_launch: str | None = None,
    marked_by_model: str | None = None,
    reason: str | None = None,
    evidence: Any = None,
    confidence: float | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Open ONE judgment, always ``pending``.

    There is no argument that opens a confirmed relation, and that is the
    MINING §5.3 constraint made structural rather than remembered: a
    machine scan proposes, a launch decides, and the two are different
    calls even when the same operator makes them a second apart
    (``trialerror term merge`` is exactly that pair, in one CLI step).

    A ``system`` row carries no launch and defaults its
    ``marked_by_model`` to the scan's own version; a ``launch`` row must
    carry one, refused here before the write rather than by the DDL, which
    cannot express "NOT NULL when a sibling column says so".
    """
    _require_vocab(src_kind, ("term", "sense"), field="src_kind")
    _require_vocab(dst_kind, ("term", "sense"), field="dst_kind")
    _require_vocab(verb, policy.RELATION_VERBS, field="verb")
    _require_vocab(marked_by_kind, policy.MARKED_BY_KINDS, field="marked_by_kind")

    if marked_by_kind == "launch":
        _require_launch(store, marked_by_launch, what="a launch-marked relation")
    elif marked_by_launch:
        raise InvalidTermInputError(
            "a system-marked relation carries no launch: machine judgments are advisory "
            "(MINING §5.3), and attributing one to a launch would make it look decided"
        )

    stamp = ts or now()
    rel_id = new_id("TREL")
    insert(
        store,
        "term_relation",
        {
            "rel_id": rel_id,
            "src_kind": src_kind,
            "src_id": src_id,
            "dst_kind": dst_kind,
            "dst_id": dst_id,
            "verb": verb,
            "decided_verb": None,
            "status": "pending",
            "reason": reason,
            "evidence": _json_or_none(evidence),
            "confidence": confidence,
            "marked_by_kind": marked_by_kind,
            "marked_by_launch": marked_by_launch,
            "marked_by_model": marked_by_model
            or (policy.SYSTEM_SCAN_MODEL if marked_by_kind == "system" else None),
            "marked_ts": stamp,
            "decided_by_launch": None,
            "decided_ts": None,
            "superseded_by": None,
        },
    )
    append_event(
        store,
        event_type="term_relation_opened",
        payload={
            "rel_id": rel_id,
            "verb": verb,
            "src": [src_kind, src_id],
            "dst": [dst_kind, dst_id],
            "marked_by_kind": marked_by_kind,
        },
        launch_id=marked_by_launch,
        ts=stamp,
    )
    return {"rel_id": rel_id, "verb": verb, "status": "pending", "marked_by_kind": marked_by_kind}


def decide_relation(
    store: Store,
    rel_id: str,
    *,
    decision: str,
    by_launch: str,
    disambiguators: Mapping[str, str] | None = None,
    into: str | None = None,
    canonical: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Resolve one pending judgment. Three of the six decisions move other
    rows; the other three only close the relation.

    ``same_as`` / ``variant_of``
        The two terms name one thing. Folds one into the other
        (:func:`merge_terms`); the difference between the two verbs is the
        ``kind`` the folded lemma is kept under -- ``former_lemma`` for an
        outright duplicate, ``variant`` for a spelling a judge decided was
        a variation rather than the same coinage. By default the
        **destination** term is canonical (a candidate is opened from the
        newcomer toward the term that was already there); pass
        ``canonical=`` to keep the other one instead.

    ``scoped``
        "Keep both readings, name them apart." Every member sense needs a
        ``disambiguator``, the term becomes ``split``, and a
        ``contradicts`` provenance edge is written between each pair of
        members -- the lexicon's first honest contradiction edges (ruling
        L-E5).

    ``not_conflict --into <sense_id>``
        "These are one reading after all." The other members are superseded
        by the kept sense and their evidence is copied onto it, so the kept
        sense ends up standing on the union of the sources. No evidence row
        is lost; the superseded senses stay readable.

    ``unrelated`` / ``rejected``
        Nothing else moves. ``rejected`` says the candidate was a false
        positive; a later system scan sees the rejected row and does not
        reopen the same member set, which is the whole reason rejections
        are rows rather than deletions.
    """
    _require_launch(store, by_launch, what="decide_relation")
    rel = get_relation(store, rel_id)
    if rel is None:
        raise RelationNotFoundError(f"no such relation: {rel_id!r}")
    if rel["status"] != "pending":
        raise RelationNotPendingError(
            f"relation {rel_id!r} is {rel['status']!r}, not 'pending' -- a decision, once made, "
            "is superseded in the open, never silently redone"
        )
    if decision not in policy.RELATION_DECISIONS:
        raise InvalidDecisionError(
            f"decision={decision!r} is not one of {list(policy.RELATION_DECISIONS)!r} "
            "('conflicts_with' is the question a conflict item asks, not an answer to it)"
        )

    ts = now()
    outcome: dict[str, Any] = {}
    touched_terms: set[str] = set()

    if decision in ("same_as", "variant_of"):
        if rel["src_kind"] != "term" or rel["dst_kind"] != "term":
            raise InvalidDecisionError(
                f"{decision!r} resolves a term-to-term duplicate candidate; relation {rel_id!r} is "
                f"{rel['src_kind']}->{rel['dst_kind']}"
            )
        keep = canonical or rel["dst_id"]
        if keep not in (rel["src_id"], rel["dst_id"]):
            raise InvalidDecisionError(
                f"canonical={canonical!r} is neither side of relation {rel_id!r}"
            )
        other = rel["src_id"] if keep == rel["dst_id"] else rel["dst_id"]
        outcome["merge"] = merge_terms(
            store,
            keep,
            other,
            by_launch=by_launch,
            alias_kind="former_lemma" if decision == "same_as" else "variant",
            reason=reason,
            ts=ts,
        )
        touched_terms.update({keep, other})

    elif decision == "scoped":
        if rel["verb"] != "conflicts_with":
            raise InvalidDecisionError(f"'scoped' resolves a conflict; relation {rel_id!r} is {rel['verb']!r}")
        members = _relation_member_sense_ids(rel)
        if len(members) < 2:
            raise InvalidDecisionError(f"relation {rel_id!r} names no member senses to scope")
        supplied = dict(disambiguators or {})
        missing = [m for m in members if not str(supplied.get(m, "")).strip()]
        if missing:
            raise MissingDisambiguatorError(
                f"'scoped' keeps every reading, so each needs a name of its own; missing for {missing!r}"
            )
        loaded = _load_decidable_members(store, rel_id, members, decision=decision)
        for sense_id in members:
            sense = loaded[sense_id]
            update(
                store,
                "term_sense",
                pk_column="sense_id",
                pk_value=sense_id,
                changes={"disambiguator": str(supplied[sense_id]).strip()},
            )
            touched_terms.add(sense["term_id"])
        edges = []
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                edges.append(
                    _prov_edge(
                        store,
                        role="contradicts",
                        src_kind="term_sense",
                        src_id=left,
                        dst_kind="term_sense",
                        dst_id=right,
                        launch_id=by_launch,
                        ts=ts,
                    )
                )
        for term_id in sorted(touched_terms):
            update(
                store,
                "term",
                pk_column="term_id",
                pk_value=term_id,
                changes={"status": "split", "updated_ts": ts},
            )
            append_event(
                store,
                event_type="term_split",
                payload={"term_id": term_id, "rel_id": rel_id, "member_sense_ids": members},
                launch_id=by_launch,
                ts=ts,
            )
        outcome["member_sense_ids"] = members
        outcome["prov_edges"] = edges

    elif decision == "not_conflict":
        if rel["verb"] != "conflicts_with":
            raise InvalidDecisionError(
                f"'not_conflict' resolves a conflict; relation {rel_id!r} is {rel['verb']!r}"
            )
        members = _relation_member_sense_ids(rel)
        if len(members) < 2:
            raise InvalidDecisionError(f"relation {rel_id!r} names no member senses to reconcile")
        if not into or into not in members:
            raise InvalidDecisionError(
                f"'not_conflict' needs into=<sense_id> naming the reading to keep, one of {members!r}"
            )
        # Every member, INCLUDING `into`, is re-read and required to still be
        # current before anything moves: `into` naming a retired reading was
        # finding F5's headline, and loading the losers up front is what
        # keeps this arm from superseding two of three and then refusing.
        loaded = _load_decidable_members(store, rel_id, members, decision=decision)
        kept = loaded[into]
        carried: list[str] = []
        superseded: list[str] = []
        have = {_evidence_identity(e) for e in _live_evidence(store, into)}
        for sense_id in members:
            if sense_id == into:
                continue
            other = loaded[sense_id]
            for row in _live_evidence(store, sense_id):
                key = _evidence_identity(row)
                if key in have:
                    continue
                carried.append(_insert_evidence(store, into, dict(row), by_launch=by_launch, ts=ts)["evidence_id"])
                have.add(key)
            expire_fact(store, "term_sense", sense_id, tx_at=ts, superseded_by=into)
            update(
                store,
                "term_sense",
                pk_column="sense_id",
                pk_value=sense_id,
                changes={"status": "superseded", "decided_by_launch": by_launch, "decided_ts": ts},
            )
            superseded.append(sense_id)
            touched_terms.add(other["term_id"])
        touched_terms.add(kept["term_id"])
        for term_id in sorted(touched_terms):
            term = get_term(store, term_id) or {}
            changes: dict[str, Any] = {"updated_ts": ts}
            if term.get("status") in ("proposed", "split"):
                changes["status"] = "active"
            if term_id == kept["term_id"]:
                changes["preferred_sense_id"] = into
            update(store, "term", pk_column="term_id", pk_value=term_id, changes=changes)
        outcome["kept_sense_id"] = into
        outcome["superseded_sense_ids"] = superseded
        outcome["evidence_carried"] = carried

    elif decision == "unrelated":
        touched_terms.update(_terms_of_relation(store, rel))

    else:  # rejected
        touched_terms.update(_terms_of_relation(store, rel))

    new_status = "rejected" if decision == "rejected" else "confirmed"
    # MINING §5.3, enforced twice on purpose (the doctor check
    # ``term_system_relation_decided`` is the second place). ``_require_launch``
    # above has already made this unreachable; it stays because the invariant
    # it guards -- a machine judgment never becomes a decision on its own -- is
    # the one an incautious later edit to this function would break first.
    if new_status == "confirmed" and not by_launch:  # pragma: no cover - unreachable by construction
        raise LaunchRequiredError(
            f"relation {rel_id!r} cannot be confirmed without a deciding launch "
            f"(marked_by_kind={rel['marked_by_kind']!r})"
        )
    update(
        store,
        "term_relation",
        pk_column="rel_id",
        pk_value=rel_id,
        changes={
            "status": new_status,
            "decided_verb": None if decision == "rejected" else decision,
            "decided_by_launch": by_launch,
            "decided_ts": ts,
            "reason": reason if reason is not None else rel["reason"],
        },
    )
    for term_id in sorted(t for t in touched_terms if t):
        if get_term(store, term_id) is not None:
            reindex_term(store, term_id)

    append_event(
        store,
        event_type="term_relation_decided",
        payload={"rel_id": rel_id, "verb": rel["verb"], "decision": decision, "reason": reason, **outcome},
        launch_id=by_launch,
        ts=ts,
    )
    return {
        "rel_id": rel_id,
        "verb": rel["verb"],
        "decision": decision,
        "status": "rejected" if decision == "rejected" else "confirmed",
        **outcome,
    }


def _terms_of_relation(store: Store, rel: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for kind, value in ((rel["src_kind"], rel["src_id"]), (rel["dst_kind"], rel["dst_id"])):
        if kind == "term":
            out.append(str(value))
        else:
            sense = get_sense(store, str(value))
            if sense is not None:
                out.append(sense["term_id"])
    return out


def merge_terms(
    store: Store,
    canonical_id: str,
    other_id: str,
    *,
    by_launch: str,
    alias_kind: str = "former_lemma",
    reason: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Fold ``other_id`` into ``canonical_id``. **Nothing is deleted.**

    The folded term's senses and aliases re-parent, its own lemma is kept
    as an alias of the canonical term (so every citation of the old name
    still resolves through :func:`find_term`), and the row itself stays
    with ``status='merged'`` and a ``merged_into`` pointer. An alias whose
    key the canonical term already has stays parented to the merged term
    rather than being dropped -- a collision means the canonical side
    already has that lookup key, so nothing is lost by leaving the
    duplicate where it is, and deleting it would be the one destructive act
    this operation otherwise avoids.
    """
    _require_launch(store, by_launch, what="merge_terms")
    canonical = get_term(store, canonical_id)
    other = get_term(store, other_id)
    if canonical is None:
        raise TermNotFoundError(f"no such term: {canonical_id!r}")
    if other is None:
        raise TermNotFoundError(f"no such term: {other_id!r}")
    if canonical_id == other_id:
        raise InvalidMergeError("a term cannot be merged into itself")
    if other["status"] == "merged":
        raise InvalidMergeError(
            f"term {other_id!r} is already merged into {other['merged_into']!r}; "
            "two live merged_into chains would disagree about where the lemma went"
        )
    if canonical["status"] in ("merged", "retired"):
        raise InvalidMergeError(
            f"canonical term {canonical_id!r} is {canonical['status']!r}; merging into it would "
            "hide the folded senses behind a term nothing reads"
        )
    _require_vocab(alias_kind, policy.ALIAS_KINDS, field="alias kind")

    stamp = ts or now()
    conn = _conn(store)

    moved_senses = [r[0] for r in conn.execute("SELECT sense_id FROM term_sense WHERE term_id = ?", (other_id,))]
    for sense_id in moved_senses:
        update(store, "term_sense", pk_column="sense_id", pk_value=sense_id, changes={"term_id": canonical_id})

    canonical_keys = {
        r[0] for r in conn.execute("SELECT alias_norm FROM term_alias WHERE term_id = ?", (canonical_id,))
    }
    canonical_keys.add(canonical["lemma_norm"])
    moved_aliases: list[str] = []
    kept_behind: list[str] = []
    for alias_id, alias_norm in conn.execute(
        "SELECT alias_id, alias_norm FROM term_alias WHERE term_id = ? ORDER BY created_ts, alias_id",
        (other_id,),
    ).fetchall():
        if alias_norm in canonical_keys:
            kept_behind.append(alias_id)
            continue
        update(store, "term_alias", pk_column="alias_id", pk_value=alias_id, changes={"term_id": canonical_id})
        canonical_keys.add(alias_norm)
        moved_aliases.append(alias_id)

    lemma_alias_id = _add_alias(
        store, canonical_id, other["lemma"], alias_kind, by_launch=by_launch, ts=stamp
    )

    update(
        store,
        "term",
        pk_column="term_id",
        pk_value=other_id,
        changes={
            "status": "merged",
            "merged_into": canonical_id,
            "preferred_sense_id": None,
            "updated_ts": stamp,
        },
    )

    canonical_changes: dict[str, Any] = {"updated_ts": stamp}
    if not canonical.get("preferred_sense_id") and other.get("preferred_sense_id"):
        canonical_changes["preferred_sense_id"] = other["preferred_sense_id"]
    if canonical["status"] == "proposed" and senses_for_term(store, canonical_id, statuses=("current",)):
        canonical_changes["status"] = "active"
    update(store, "term", pk_column="term_id", pk_value=canonical_id, changes=canonical_changes)

    reindex_term(store, canonical_id)
    reindex_term(store, other_id)
    append_event(
        store,
        event_type="term_merged",
        payload={
            "canonical_term_id": canonical_id,
            "merged_term_id": other_id,
            "merged_lemma": other["lemma"],
            "alias_kind": alias_kind,
            "senses_moved": len(moved_senses),
            "aliases_moved": len(moved_aliases),
            "aliases_left_behind": len(kept_behind),
            "reason": reason,
        },
        launch_id=by_launch,
        ts=stamp,
    )
    return {
        "canonical_term_id": canonical_id,
        "merged_term_id": other_id,
        "senses_moved": moved_senses,
        "aliases_moved": moved_aliases,
        "aliases_left_behind": kept_behind,
        "lemma_alias_id": lemma_alias_id,
    }
