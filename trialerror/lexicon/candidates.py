"""Save-time duplicate surfacing -- engram-F4, adopted "with constraint"
(design ``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` §3; MINING §3 row
engram-F4, §5.3 for the constraint).

The question this module answers is asked once, at the moment a reading is
proposed: *is there already a term for this?* It answers with **candidates,
never with a merge**. Every hit becomes one ``term_relation(verb='same_as',
status='pending', marked_by_kind='system')`` -- a row that says "a machine
thinks these two might be one thing" and that only a launch-attributed
:func:`trialerror.lexicon.api.decide_relation` can move. That is the whole
of the constraint the adoption was qualified by: the scan is free to be
noisy, because nothing it produces is a decision.

**Two halves, one of them deferred.**

*Exact key collision* (:func:`_alias_hits`) -- another term whose
``lemma_norm`` is one of this term's alias keys, or one of whose aliases is
this term's lemma. That is not a near-miss at all: two terms are reachable
by the same lookup key, and :func:`trialerror.lexicon.api.find_term` has to
pick one of them. Surfaced with ``confidence=1.0``, always, floor or no
floor.

*Trigram near-miss* (:func:`_trigram_hits`, then :func:`_gate`) --
``term_fts`` (FTS5, ``tokenize='trigram'``) ranked by ``bm25``, top
``limit``, kept only when the score passes ``policy.DUPLICATE_BM25_FLOOR``,
and then only when the pair also passes the gate below. Trigram rather than
porter is what makes "initiative"/"initiatives"/"iniative" reachable from
each other without a stemmer folding two different named things into one
identity (:mod:`trialerror.lexicon.normalize` says why the key function
refuses to stem).

**The trigram half is two stages, and the second one was measured into
existence** (build step 1c; the numbers live in
:data:`trialerror.lexicon.policy.DUPLICATE_CALIBRATION_REFERENCE`). On the
60-row fixture the first stage alone looked calibrated. On the first real
register import -- 6,975 terms -- it opened **33,785** pending candidates,
mean fan-out 4.9 against a top-5 cap: the cap was firing for nearly every
term in the store, which means it had stopped selecting and started merely
counting. Two thirds of those pairs had nothing in common but
three-character windows.

So a hit that clears the bm25 floor is surfaced only when one of **three
routes** holds for the pair. The gate reports which one fired, and tries
them in this order (:func:`_gate`):

* ``informative_token`` -- the two sides' **NAMES** (lemma and aliases,
  both sides) share a whole word, in the
  :func:`trialerror.lexicon.normalize.name_tokens` sense, that fewer than
  ``duplicate_informative_token_fraction`` of the store's terms carry
  (default 2%, computed live from the store at scan time, never a baked
  list), and that is not a function word;
* ``name_in_text`` -- one side's **whole** normalized name or alias occurs
  as a PHRASE, on word boundaries, inside the other side's indexed text
  (names plus current glosses -- the two sides the FTS match itself was
  between). This is the route that keeps "a term whose gloss contains
  another term's name is reachable from that other term" true, which the
  design argued for and the E2 tests measure;
* ``similarity`` -- their **whole-name similarity** reaches
  ``duplicate_similarity_floor`` (default 0.5) --
  :func:`trialerror.lexicon.normalize.trigram_similarity`, Jaccard over the
  same trigram unit the index matched on.

The first route is what removes the flood: two names sharing only a generic
category word are not evidence of anything, and at corpus scale that is
most of what a trigram index finds. The third is what keeps the first from
being too clever -- a misspelling or a run-together compound shares no
whole token *because* it is nearly the same string, and the similarity
measure sees exactly that.

**The second route replaced a scoping the 1c gate got wrong** (build step
1d). 1c ran each side's names against the other side's *indexed text*, on
the reachability argument the second route now carries explicitly -- and at
corpus scale that leaked, because a real register gloss is twenty to a
hundred and sixty words of prose, and almost every pair shares SOME name
token that is rare among names and happens to turn up somewhere in the
other side's paragraph. The live rescan measured it: **6,992 terms, 33,870
pending candidates, of which the 1c gate withdrew 8,219 and kept 25,651**
-- eight times the top of the (100, 3,000) band the stage is calibrated
against, and the 1c reference had already counted 14,826 pairs sharing no
whole name token at all. Barely half of the pairs the token rule exists to
remove were being removed, because the gloss kept supplying the word the
two NAMES never shared. One word landing in a paragraph of prose is not
evidence that two names are one thing; a whole name landing there is, and
that is the narrower question the second route asks.

**Build step 1e adds COVERAGE on top of the first route, and one bar under
the second.** The frequency test says which shared words count; it does not
say how much of either name they account for, and at corpus scale that is
the difference between "these two names are the same thing" and "these two
names both mention one rare word". So the token route now also asks that the
shared informative tokens cover at least
``[lexicon] duplicate_coverage_min`` (default 0.5) of the SHORTER name's
informative tokens -- a two-word name sharing one of its two informative
words passes at exactly a half, a nine-word name sharing one of nine does
not. And the ``name_in_text`` route now requires the contained name to carry
at least one informative token of its own
(``[lexicon] name_in_text_requires_informative``, default true): a name made
of nothing but function words and the store's family word is written inside
half the glosses in any store, and that is a fact about prose rather than
evidence about two terms.

Both are applied **after** the frequency gate and both only ever REMOVE a
way of passing, so the gate surfaces a subset of what it surfaced before
them; setting the two keys to ``0`` / ``false`` reaches the pre-1e behaviour
exactly, which is what makes the tightening auditable rather than merely
asserted. Every existing configuration that names neither key therefore
behaves as it did plus the new rule.

**Every surfaced candidate carries a confidence**, and it is that
similarity: never ``None`` for a system-opened row (decision D1). An exact
key collision is 1.0 because it is not a guess. The bm25 score is still
recorded in the relation's ``evidence`` JSON, where it belongs -- it is the
reason the pair was *looked at*, not a statement about how alike the two
names are, and it is not comparable between stores.

**The index spans lemma, aliases and current glosses; the query is built
from names only** -- this term's lemma and its aliases, never its gloss.
That asymmetry is the whole difference between a useful candidate list and
a useless one, and it was measured rather than assumed: querying with gloss
text too, on a 60-row register fixture, opened 159 candidates whose top
matches were pairs like "baseline drift"/"hold time" -- glosses written in
the same register share "the", "records", "procedure", and those tokens
outrank the name. Names are what two terms have to share to be the same
thing; a gloss on the *indexed* side still earns its place, because a term
whose gloss contains another term's name is a real hit -- which is exactly
and only what the ``name_in_text`` route asks of it. Catching "same idea,
different words" is the deferred cosine half's job, not a job to do badly
with trigrams.

The **cosine half is deliberately absent** (empryo-F10 fold-in, design §11
"Deferred"): no embedding column exists on ``term_sense`` yet, and the
trigram half is the drop-in piece. When it lands it adds hits to the same
list and opens the same pending rows -- no consumer of this module changes.

**What is NOT a candidate**, and why each exclusion is a correctness rule
rather than a filter:

* the term itself (a term is not its own duplicate);
* a ``merged`` or ``retired`` term (its lemma already resolves to the term
  it was folded into -- proposing a merge into a merged term is exactly the
  double-chain :class:`~trialerror.lexicon.errors.InvalidMergeError` refuses);
* a pair that **already has a relation of any status**, including a
  ``rejected`` one. A rejected candidate is a judgment the program made and
  a later scan must not re-ask; that is the reason rejections are rows
  rather than deletions (design §3, "Decisions on relations").

**Index visibility.** ``term_fts`` is maintained by the write API, so a
term inserted directly into the tables (a fixture, a hand-patched store) is
invisible to the trigram half until ``trialerror term reindex``
(:func:`trialerror.lexicon.api.reindex_all`) runs. The exact-collision half
reads ``term``/``term_alias`` and therefore sees everything. That asymmetry
is stated rather than fixed: making the scan build its own index would give
it a second, quietly divergent view of the store.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Sequence

from trialerror.events.api import append_event
from trialerror.lexicon import api, policy
from trialerror.lexicon.normalize import fts_text, name_tokens, norm_lemma, trigram_similarity
from trialerror.stores.writer import update
from trialerror.util.timeutil import now

__all__ = [
    "MIN_TRIGRAM_CHARS",
    "MAX_QUERY_TERMS",
    "DEFAULT_LIMIT",
    "MAX_REPORTED_WITHDRAWALS",
    "TokenStats",
    "token_stats",
    "find_candidates",
    "surface_candidates",
    "rescan_duplicate_candidates",
]

#: FTS5's trigram tokenizer indexes 3-character sequences, so a token
#: shorter than three characters can never match anything. Dropped from the
#: query rather than passed through: an unmatchable token contributes
#: nothing but a chance of an FTS5 syntax error.
MIN_TRIGRAM_CHARS = 3

#: How many name tokens reach the MATCH query. A term with a dozen aliases
#: is already found by the exact-key half; past that many tokens an OR
#: query stops naming a thing and starts matching prose.
MAX_QUERY_TERMS = 12

#: Top-N from the trigram half (design §3, "top-5").
DEFAULT_LIMIT = 5

#: How many withdrawn ``rel_id``s :func:`rescan_duplicate_candidates` names
#: individually. The counts are always exact; the list is capped so a
#: re-scan over a 33,785-row queue does not return a 30,000-entry payload
#: through a CLI envelope (:data:`trialerror.lexicon.backfill.
#: MAX_REPORTED_REFUSALS`, same reasoning).
MAX_REPORTED_WITHDRAWALS = 20


# ---------------------------------------------------------------------------
# the gate's live view of the store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenStats:
    """How common each whole word is among this store's term names.

    Built from the store at scan time rather than shipped as a list, which
    is the point: what counts as a generic category word is a property of
    the corpus that was imported, and this repo neither holds that corpus
    nor is allowed to. A store of instrument names and a store of statute
    names have different common words and the same rule finds both.

    ``document_frequency`` counts TERMS, not occurrences: a word twice in
    one name is one term carrying it, and a term's aliases count toward the
    same term once. ``term_count`` is the denominator the fraction is taken
    of, and excludes ``merged``/``retired`` terms for the same reason
    :func:`find_candidates` refuses to offer them -- they are not names the
    store answers to any more.

    ``fraction`` is the knob the ``threshold`` was computed FROM, carried
    alongside it so a report can show the operator which number was in force
    (build step 1d). The live sweep that motivated 1d moved this knob three
    times and saw the threshold never move, because the CLI was not passing
    the program's config in at all; a threshold reported without the
    fraction beside it cannot tell you that.
    """

    term_count: int
    document_frequency: Mapping[str, int]
    threshold: float
    fraction: float = policy.DUPLICATE_INFORMATIVE_TOKEN_FRACTION
    stopwords: frozenset[str] = policy.DUPLICATE_STOPWORDS

    def is_informative(self, token: str) -> bool:
        """Is sharing this token evidence that two names are one thing?

        A token nobody in the store carries (``df`` 0 -- reachable when a
        caller measures a name that is not in the store yet) is informative:
        the unknown side of the line is the rare side.
        """
        if not token or token in self.stopwords:
            return False
        return self.document_frequency.get(token, 0) < self.threshold


def token_stats(store, *, config: Mapping[str, Any] | None = None) -> TokenStats:
    """Build :class:`TokenStats` from the store's live term names.

    One pass over ``term`` plus ``term_alias``. Cheap enough to do per scan
    (5,186 distinct tokens over 6,975 terms on the reference corpus) and
    deliberately NOT cached anywhere: a cache would have to be invalidated
    by every propose, and a duplicate scan reading a stale view of what is
    common is the exact failure this whole stage exists to fix. Callers that
    scan many terms at once -- :func:`trialerror.lexicon.scan.scan_terms`,
    :func:`rescan_duplicate_candidates` -- build it once and pass it down.
    """
    conn = store.conn_for_table("term")
    rows = conn.execute(
        "SELECT t.term_id AS term_id, t.lemma_norm AS name FROM term t "
        "WHERE t.status NOT IN ('merged','retired') "
        "UNION ALL "
        "SELECT a.term_id AS term_id, a.alias_norm AS name FROM term_alias a "
        "JOIN term t ON t.term_id = a.term_id "
        "WHERE t.status NOT IN ('merged','retired')"
    ).fetchall()

    per_term: dict[str, set[str]] = {}
    for row in rows:
        per_term.setdefault(row["term_id"], set()).update(name_tokens(row["name"]))

    document_frequency: dict[str, int] = {}
    for tokens in per_term.values():
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    resolved = policy.load_policy(config)
    return TokenStats(
        term_count=len(per_term),
        document_frequency=document_frequency,
        threshold=policy.informative_df_threshold(len(per_term), policy=resolved),
        fraction=float(resolved["duplicate_informative_token_fraction"]),
    )


def _keys_for_term(store, term_id: str) -> list[str]:
    """Every normalized lookup key that resolves to this term -- its lemma
    and each of its aliases. The union
    :func:`trialerror.lexicon.api.find_term` searches, which is what makes a
    collision on any one of them a real ambiguity."""
    conn = store.conn_for_table("term")
    keys: list[str] = []
    term = api.get_term(store, term_id)
    if term is not None:
        keys.append(term["lemma_norm"])
    for (alias_norm,) in conn.execute(
        "SELECT alias_norm FROM term_alias WHERE term_id = ? ORDER BY created_ts, alias_id", (term_id,)
    ).fetchall():
        if alias_norm not in keys:
            keys.append(alias_norm)
    return keys


def _alias_hits(store, term_id: str, keys: Sequence[str]) -> list[dict[str, Any]]:
    """Terms reachable by one of ``keys`` that are not this term."""
    if not keys:
        return []
    conn = store.conn_for_table("term")
    marks = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT t.term_id AS term_id, t.lemma_norm AS matched FROM term t "
        f"WHERE t.lemma_norm IN ({marks}) AND t.term_id != ? "
        f"UNION "
        f"SELECT t.term_id AS term_id, a.alias_norm AS matched FROM term_alias a "
        f"JOIN term t ON t.term_id = a.term_id "
        f"WHERE a.alias_norm IN ({marks}) AND t.term_id != ?",
        [*keys, term_id, *keys, term_id],
    ).fetchall()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row["term_id"] in seen:
            continue
        seen.add(row["term_id"])
        out.append(
            {
                "term_id": row["term_id"],
                "why": "shared_key",
                "matched_on": row["matched"],
                "score": None,
                "confidence": 1.0,
                "gate": "shared_key",
                "shared_tokens": [],
                "name_in_text": None,
            }
        )
    return out


def _indexed_text(store, term_id: str, keys: Sequence[str]) -> str:
    """The text ``term_fts`` holds for this term -- its names plus its
    current glosses -- normalized the way a name is, as one string.

    The indexed side of the match, read back so the gate can look for a
    PHRASE in it rather than for loose words (build step 1d; see
    :func:`_gate`). Built with :func:`trialerror.lexicon.normalize.fts_text`
    -- the same function the index itself is written with -- so the gate and
    the index cannot disagree about what the indexed side says.

    One string rather than a token set because the question changed: "is
    this whole name in there" needs the words in their order, and the words
    in their order answer "do these two names share a word" too.
    """
    glosses = [
        gloss
        for (gloss,) in store.conn_for_table("term_sense").execute(
            "SELECT gloss FROM term_sense WHERE term_id = ? AND status = 'current' "
            "ORDER BY created_at, sense_id",
            (term_id,),
        ).fetchall()
    ]
    return fts_text(*keys, *glosses)


@lru_cache(maxsize=8192)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """``phrase`` as a word-boundary regex, compiled once per distinct name.

    ``(?<!\\w)`` / ``(?!\\w)`` rather than ``\\b`` because a normalized name
    can begin or end with a character that is not a word character (a
    parenthesis, a dash), and ``\\b`` is defined relative to the pattern's
    own edge character -- it would silently stop anchoring for exactly those
    names. The lookarounds say the same thing for every name: whatever sits
    against the match may not be a word character.

    Punctuation in the surrounding text is therefore transparent, which is
    the whole point on real glosses: "... the settling time, which ..."
    contains the name and a token-equality test over
    :func:`trialerror.lexicon.normalize.name_tokens` would say it does not,
    because that tokenizer splits on whitespace alone and would hand back
    ``'time,'``.

    Cached because a whole-store rescan asks this of a few thousand distinct
    names tens of thousands of times, and ``re``'s own internal cache is far
    too small to hold a store's worth of them.
    """
    return re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")


def _name_in_text(
    keys: Sequence[str],
    text: str,
    *,
    stats: "TokenStats | None" = None,
    require_informative: bool = False,
) -> str | None:
    """The first of ``keys`` that occurs as a whole phrase in ``text``, or
    ``None``.

    Keys arrive in :func:`_keys_for_term` order -- lemma first, then aliases
    oldest-first -- so the phrase a reason string names is stable across
    runs rather than an accident of set iteration.

    **Rule 1e.** With ``require_informative`` (the
    ``[lexicon] name_in_text_requires_informative`` default), a key carrying
    no informative token of its own is skipped before the search rather than
    after it: a name made only of function words and the store's family word
    is written inside half the glosses in the store, and finding it there is
    a fact about prose. Skipped, not refused -- a later key of the same term
    may still qualify, which is why the filter sits inside the loop.
    """
    if not text:
        return None
    for key in keys:
        phrase = norm_lemma(key or "")
        if not phrase:
            continue
        if require_informative and stats is not None:
            if not any(stats.is_informative(t) for t in name_tokens(phrase)):
                continue
        if _phrase_pattern(phrase).search(text):
            return phrase
    return None


def _gate(
    store,
    left_id: str,
    left_keys: Sequence[str],
    right_id: str,
    right_keys: Sequence[str],
    stats: TokenStats,
    *,
    floor: float,
    coverage_min: float = policy.DUPLICATE_COVERAGE_MIN,
    name_in_text_requires_informative: bool = policy.NAME_IN_TEXT_REQUIRES_INFORMATIVE,
) -> dict[str, Any]:
    """The second stage, for one pair of terms: one verdict.

    Returns ``{"surfaced", "confidence", "shared_tokens", "coverage",
    "informative_shorter", "name_in_text", "gate"}``. ``confidence`` is the best whole-name similarity over the
    cross product of the two terms' keys, and is reported whether or not the
    pair passes -- a caller measuring the gate
    (``find_candidates(..., gate=False)``,
    :func:`rescan_duplicate_candidates`) needs the number that was compared,
    not just the verdict.

    **Both terms' aliases take part on both sides.** An alias is a lookup
    key: a term reachable by it is reachable by it, and a pair whose lemmas
    look unalike while an alias of one is nearly the lemma of the other is
    exactly a duplicate worth surfacing.

    **The token route runs NAMES against NAMES** (build step 1d; 1c ran each
    side's names against the other side's indexed text, and the module
    docstring carries the measurement of what that cost). Only a name token
    can be the shared word, and the frequency that decides whether it is
    generic is measured over names -- so measuring the *presence* of the
    word anywhere else was the inconsistency: it let a paragraph of prose
    stand in for a name.

    **The name route runs whole NAMES against INDEXED TEXT** -- names plus
    current glosses, the two sides the FTS match itself was between. That is
    the hit the design argued for, asked as the narrow question it always
    was: not "does some word of this name appear over there" but "is this
    name, entire, written over there".

    The two texts are read **only if the token route did not fire**, which
    is two SQL reads saved on every pair that passes -- the common case on a
    calibrated store, and worth having on a queue of tens of thousands.

    **Rule 1e sits AFTER the frequency test, never instead of it** (build
    step 1e). The frequency test is unchanged and still decides which shared
    words count at all; ``coverage_min`` then asks how much of the shorter
    name those words account for
    (:func:`trialerror.lexicon.policy.coverage_of_shorter_name`), and only a
    pair that passes BOTH goes out on the ``informative_token`` route. A
    pair that shares a rare word but covers too little of either name falls
    through to the two routes below it exactly as a pair sharing no rare word
    does -- rule 1e removes a way of passing, it never adds one, so the gate
    after it surfaces a subset of what the gate before it surfaced. The
    measured ``shared_tokens`` and ``coverage`` are reported either way,
    because a caller sweeping the knob needs the numbers that were compared
    and not only the verdict.
    """
    left_names: set[str] = set()
    for key in left_keys:
        left_names.update(name_tokens(key))
    right_names: set[str] = set()
    for key in right_keys:
        right_names.update(name_tokens(key))

    left_informative = {t for t in left_names if stats.is_informative(t)}
    right_informative = {t for t in right_names if stats.is_informative(t)}
    shared = sorted(left_informative & right_informative)
    coverage = policy.coverage_of_shorter_name(
        len(shared), len(left_informative), len(right_informative)
    )
    by_token = bool(shared) and coverage >= coverage_min

    similarity = 0.0
    for left in left_keys:
        for right in right_keys:
            similarity = max(similarity, trigram_similarity(left, right))

    name_in_text: str | None = None
    if not by_token:
        name_in_text = _name_in_text(
            left_keys,
            _indexed_text(store, right_id, right_keys),
            stats=stats,
            require_informative=name_in_text_requires_informative,
        )
        if name_in_text is None:
            name_in_text = _name_in_text(
                right_keys,
                _indexed_text(store, left_id, left_keys),
                stats=stats,
                require_informative=name_in_text_requires_informative,
            )

    if by_token:
        gate = "informative_token"
    elif name_in_text is not None:
        gate = "name_in_text"
    elif similarity >= floor:
        gate = "similarity"
    else:
        gate = None
    return {
        "surfaced": gate is not None,
        "confidence": similarity,
        "shared_tokens": shared,
        "coverage": coverage,
        "informative_shorter": min(len(left_informative), len(right_informative)),
        "name_in_text": name_in_text,
        "gate": gate,
    }


def _fts_query(names: Sequence[str]) -> str | None:
    """An FTS5 query string from this term's names, or ``None`` when none of
    them is long enough to be matchable.

    Each name contributes itself as a phrase (so "settling time" can match a
    term that merely contains it) and each of its words (so "settling" alone
    reaches "settling period").

    Every token is emitted as a **quoted phrase**, which is what makes the
    query safe against a lemma containing FTS5 operator syntax (``AND``,
    ``NEAR``, ``*``, ``(``) -- a lemma is data, and a store whose scan can
    be steered by the text of a term someone proposed is a store with an
    injection surface. Embedded double quotes are doubled, the FTS5 escape.
    """
    phrases: list[str] = []
    for name in names:
        normed = norm_lemma(name or "")
        for candidate in [normed, *normed.split()]:
            if len(candidate) >= MIN_TRIGRAM_CHARS and candidate not in phrases:
                phrases.append(candidate)
            if len(phrases) >= MAX_QUERY_TERMS:
                break
        if len(phrases) >= MAX_QUERY_TERMS:
            break
    if not phrases:
        return None
    return " OR ".join('"' + p.replace('"', '""') + '"' for p in phrases)


def _trigram_hits(store, term_id: str, query: str, *, limit: int, floor: float) -> list[dict[str, Any]]:
    conn = store.conn_for_table("term")
    try:
        rows = conn.execute(
            "SELECT term_id, bm25(term_fts) AS score FROM term_fts "
            "WHERE term_fts MATCH ? AND term_id != ? ORDER BY score LIMIT ?",
            (query, term_id, max(1, limit) * 4),
        ).fetchall()
    except sqlite3.OperationalError:
        # An FTS5 syntax error on a query built from a lemma is a bug in the
        # query builder, not a reason to refuse the proposal that triggered
        # it: the caller wrote a term, and surfacing is advisory. Reported as
        # "no hits" here and visible as a missing candidate, never as a
        # failed write.
        return []
    best: dict[str, float] = {}
    for row in rows:
        score = float(row["score"])
        if score > floor:
            continue
        current = best.get(row["term_id"])
        if current is None or score < current:
            best[row["term_id"]] = score
    ranked = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))[:limit]
    return [
        {"term_id": tid, "why": "trigram", "matched_on": query, "score": score, "confidence": None}
        for tid, score in ranked
    ]


def find_candidates(
    store,
    term_id: str,
    *,
    sense_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    config: Mapping[str, Any] | None = None,
    stats: TokenStats | None = None,
    gate: bool = True,
) -> list[dict[str, Any]]:
    """Terms that might be the same thing as ``term_id``. **Pure** -- no
    writes, no events, safe on a read-only store.

    Exact key collisions come first (they are certainties, not scores), then
    trigram hits best-first, each carrying the ``confidence`` and the
    ``shared_tokens`` the gate measured. ``sense_id`` names the reading
    whose proposal triggered the scan; it does not change the query (see the
    module docstring on why the query is built from names), it is carried
    into the opened relation so a reviewer can see which proposal raised the
    candidate.

    ``stats`` lets a caller scanning many terms build the store's token
    frequencies once (:func:`token_stats`) instead of once per term; omitted,
    they are built here, which is the correct default for the save-time call
    -- one proposal is one row, and the freshest possible view of what is
    common in this store is the one that includes it.

    ``gate=False`` returns what the FIRST stage alone would have surfaced --
    the pre-1c behaviour, hits and all, each still annotated with what the
    gate WOULD have said. It exists for measurement: the calibration test
    compares the two lists over one corpus, and an operator can do the same
    on a real one before running a withdrawal. Nothing that WRITES accepts
    this argument, so there is no route by which an ungated row is opened.
    """
    term = api.get_term(store, term_id)
    if term is None:
        return []

    resolved = policy.load_policy(config)
    keys = _keys_for_term(store, term_id)
    hits = _alias_hits(store, term_id, keys)
    found = {h["term_id"] for h in hits}

    query = _fts_query([term["lemma"], *keys])
    if query:
        for hit in _trigram_hits(
            store, term_id, query, limit=limit, floor=resolved["duplicate_bm25_floor"]
        ):
            if hit["term_id"] not in found:
                found.add(hit["term_id"])
                hits.append(hit)

    out: list[dict[str, Any]] = []
    measured: TokenStats | None = stats
    for hit in hits:
        other = api.get_term(store, hit["term_id"])
        if other is None or other["status"] in ("merged", "retired"):
            continue
        if hit["why"] == "trigram":
            if measured is None:
                measured = token_stats(store, config=config)
            verdict = _gate(
                store,
                term_id,
                keys,
                hit["term_id"],
                _keys_for_term(store, hit["term_id"]),
                measured,
                floor=resolved["duplicate_similarity_floor"],
                coverage_min=resolved["duplicate_coverage_min"],
                name_in_text_requires_informative=resolved[
                    "name_in_text_requires_informative"
                ],
            )
            hit = {
                **hit,
                "confidence": verdict["confidence"],
                "shared_tokens": verdict["shared_tokens"],
                "coverage": verdict["coverage"],
                "name_in_text": verdict["name_in_text"],
                "gate": verdict["gate"],
            }
            if gate and not verdict["surfaced"]:
                continue
        out.append({**hit, "lemma": other["lemma"], "status": other["status"]})
    return out


def _open_reason(hit: Mapping[str, Any], *, floor: float) -> str:
    """The ``term_relation.reason`` a surfaced hit is opened with -- one
    sentence a reviewer can act on without opening the evidence JSON.

    One sentence per gate route (build step 1d), because "why am I being
    asked about these two" has three different answers now and a reviewer
    ruling on a queue is entitled to the one that applies.

    The sentence is chosen by the ROUTE THAT FIRED rather than by which
    measurement happens to be non-empty (build step 1e): a pair can now share
    a rare word and still be opened by a later route, because coverage
    disqualified the token route, and reading the shared word out as the
    reason would tell a reviewer the gate asked a question it did not ask."""
    if hit["why"] == "shared_key":
        return "both names resolve through the same lookup key"
    gate = hit.get("gate")
    shared = hit.get("shared_tokens") or []
    if gate == "informative_token" and shared:
        words = ", ".join(repr(t) for t in shared)
        return (
            f"trigram near-miss between the two names, which share {words} -- "
            f"rare in this store (similarity {hit['confidence']})"
        )
    phrase = hit.get("name_in_text")
    if gate == "name_in_text" and phrase:
        return (
            f"trigram near-miss between the two names: the whole name {phrase!r} is written "
            f"inside the other term's name or gloss (similarity {hit['confidence']})"
        )
    if shared:
        words = ", ".join(repr(t) for t in shared)
        return (
            f"trigram near-miss between the two names: similarity {hit['confidence']} "
            f"at or above the {floor} floor -- the rare word(s) they share ({words}) "
            f"cover too little of the shorter name to open it on their own"
        )
    return (
        f"trigram near-miss between the two names: similarity {hit['confidence']} "
        f"at or above the {floor} floor, though they share no rare word"
    )


def _existing_relation(store, term_id: str, other_id: str) -> dict[str, Any] | None:
    """Any relation, of any status, already naming this pair term-to-term."""
    row = store.conn_for_table("term_relation").execute(
        "SELECT * FROM term_relation WHERE src_kind = 'term' AND dst_kind = 'term' "
        "AND ((src_id = ? AND dst_id = ?) OR (src_id = ? AND dst_id = ?)) "
        "ORDER BY marked_ts, rel_id LIMIT 1",
        (term_id, other_id, other_id, term_id),
    ).fetchone()
    return dict(row) if row is not None else None


def surface_candidates(
    store,
    term_id: str,
    *,
    sense_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    config: Mapping[str, Any] | None = None,
    stats: TokenStats | None = None,
) -> dict[str, Any]:
    """Run :func:`find_candidates` and open one pending ``same_as`` row per
    hit that does not already have a relation with this term.

    Called by :func:`trialerror.lexicon.api.propose` at save time; the
    return value is what lands in that function's result under
    ``"candidates"``. Idempotent by construction: the second call finds the
    rows the first one opened and reports them under ``"existing"`` instead
    of opening duplicates.

    Rows are opened ``marked_by_kind='system'`` with **no launch**. That is
    not an omission -- a scan has no launch to name, and attributing its
    guess to the launch that happened to be proposing would make an
    advisory row look decided (MINING §5.3; the
    ``term_system_relation_decided`` doctor check audits the other end of
    the same rule).
    """
    hits = find_candidates(
        store, term_id, sense_id=sense_id, limit=limit, config=config, stats=stats
    )
    floor = policy.load_policy(config)["duplicate_similarity_floor"]
    opened: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []
    for hit in hits:
        already = _existing_relation(store, term_id, hit["term_id"])
        if already is not None:
            existing.append(
                {
                    "term_id": hit["term_id"],
                    "lemma": hit["lemma"],
                    "rel_id": already["rel_id"],
                    "status": already["status"],
                    "verb": already["verb"],
                }
            )
            continue
        rel = api.open_relation(
            store,
            src_kind="term",
            src_id=term_id,
            dst_kind="term",
            dst_id=hit["term_id"],
            verb="same_as",
            marked_by_kind="system",
            marked_by_model=policy.SYSTEM_SCAN_MODEL,
            reason=_open_reason(hit, floor=floor),
            evidence={
                "why": hit["why"],
                "matched_on": hit["matched_on"],
                "score": hit["score"],
                "similarity": hit["confidence"],
                "gate": hit.get("gate"),
                "shared_tokens": hit.get("shared_tokens") or [],
                "coverage": hit.get("coverage"),
                "name_in_text": hit.get("name_in_text"),
                "sense_id": sense_id,
                "scan": policy.SYSTEM_SCAN_MODEL,
            },
            confidence=hit["confidence"],
        )
        opened.append(
            {
                "term_id": hit["term_id"],
                "lemma": hit["lemma"],
                "rel_id": rel["rel_id"],
                "why": hit["why"],
                "gate": hit.get("gate"),
                "confidence": hit["confidence"],
            }
        )
    return {
        "status": "ok",
        "term_id": term_id,
        "sense_id": sense_id,
        "candidates": hits,
        "opened": opened,
        "existing": existing,
    }


# ---------------------------------------------------------------------------
# withdrawal -- re-evaluating a queue that was opened under the old rule
# ---------------------------------------------------------------------------


#: The system-opened rows a re-scan is allowed to look at, in one place
#: because the WHERE clause *is* the rule (build step 1c, decision D4):
#: pending, ``same_as``, term-to-term, opened by the scan itself, and never
#: touched by a person. ``decided_ts IS NOT NULL`` is the second half of
#: "human-touched" -- a row somebody has already ruled on is not the
#: machine's to take back, whatever its status says.
_RESCAN_SELECT = (
    "SELECT * FROM term_relation WHERE verb = 'same_as' AND status = 'pending' "
    "AND marked_by_kind = 'system' AND decided_ts IS NULL AND decided_by_launch IS NULL "
    "AND src_kind = 'term' AND dst_kind = 'term' ORDER BY marked_ts, rel_id"
)


def _relation_why(rel: Mapping[str, Any]) -> str | None:
    """Which half of the scan opened this row, read back from its own
    ``evidence`` JSON. ``None`` when the JSON is absent or unreadable --
    which the caller treats as "leave it alone", not as "trigram"."""
    raw = rel.get("evidence")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    why = parsed.get("why")
    return str(why) if isinstance(why, str) else None


def rescan_duplicate_candidates(
    store,
    *,
    by_launch: str,
    config: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-evaluate the pending, system-opened ``same_as`` queue under the
    current gate and withdraw the rows the scan would no longer open
    (``trialerror term scan --rescan``; build step 1c, decision D4).

    A calibration change that only affects rows opened *after* it is not a
    calibration change -- the 33,785 candidates the first real backfill
    produced would sit in the queue forever, and the queue is the thing the
    change exists to make usable. This is the verb that goes back.

    **What it will not touch**, and each exclusion is a rule rather than a
    filter:

    * anything **human-touched** -- a relation whose ``marked_by_kind`` is
      not ``system``, or that carries a ``decided_ts``/``decided_by_launch``.
      A machine may take back its own guess; it may not take back a person's
      attention. That is the same MINING §5.3 boundary the rest of this
      module is built on, read in the other direction;
    * anything that is **not pending** -- a decided row is history;
    * an **exact key collision** (``why='shared_key'``). Two terms reachable
      by one lookup key is not a score and no gate applies to it: the
      ambiguity is still there, and withdrawing it would hide a real one;
    * a row whose ``evidence`` JSON cannot be read at all. Reported under
      ``skipped``, never guessed at.

    **What "withdrawn" means on the row.** ``status`` goes to ``rejected``
    -- the closest thing in the schema's four-value vocabulary to "closed,
    no merge, and do not raise this pair again", and *exactly* the state
    :func:`_existing_relation` already refuses to reopen, which is what
    makes the withdrawal idempotent and a later plain scan quiet. The word
    ``withdrawn`` is the EVENT's (``term_candidate_withdrawn``, one per
    relation, carrying ``status='withdrawn'``), because the distinction
    matters to anyone reading the log: a rejection is a judgment about two
    names, a withdrawal is the scan saying it should not have asked. The
    ``reason`` written to the row names the gate and the measured
    similarity, so the row explains itself without the event.

    ``decided_by_launch`` is the re-scan's own launch and is required
    (ruling L-E4, XID-checked before anything is written) -- withdrawing is
    an act somebody ran, and an unattributed one would be the machine
    deciding on its own, which is the one thing this package does not do.

    ``dry_run=True`` measures and reports and writes nothing -- not the
    rows, not the events. Run it first on a queue this size.

    ``config`` is the program's ``trialerror.toml`` as a plain dict, and
    passing it is the caller's job -- ``trialerror term scan --rescan`` does
    since build step 1d and did not before, which is why an operator's sweep
    of ``duplicate_informative_token_fraction`` over the live program moved
    the withdrawal count by zero on every setting. The summary reports the
    ``informative_token_fraction`` actually in force for exactly that
    reason: a threshold quoted on its own cannot tell you whether the knob
    you turned was read. Build step 1e adds ``coverage_min`` and
    ``name_in_text_requires_informative`` to that report on the same
    argument.

    **This is also how rule 1e reaches a queue that was opened before it**
    (build step 1e, Part A item 3). The rescan re-asks :func:`_gate` -- the
    whole gate, including the new coverage bar -- of every pending row the
    scan itself opened, so the pairs the new rule no longer opens are
    withdrawn by the verb that already existed rather than by a migration.
    ``withdrawn_by_coverage`` counts the subset the coverage bar is
    responsible for, which is what a dry run is for: the total says how much
    smaller the queue gets, that number says how much of it the new rule
    did. Nothing about WHICH rows may be touched changes -- a human-touched
    row is as untouchable under 1e as it was under 1c.
    """
    api._require_launch(store, by_launch, what="rescan_duplicate_candidates")
    resolved = policy.load_policy(config)
    floor = resolved["duplicate_similarity_floor"]
    coverage_min = resolved["duplicate_coverage_min"]
    requires_informative = resolved["name_in_text_requires_informative"]
    stats = token_stats(store, config=config)

    rows = [dict(r) for r in store.conn_for_table("term_relation").execute(_RESCAN_SELECT).fetchall()]

    withdrawn: list[dict[str, Any]] = []
    #: Of the withdrawals, how many rule 1e's coverage bar is responsible for
    #: -- the number that says what the new rule COST on this queue, which is
    #: the whole reason an operator runs the dry run before the real one.
    withdrawn_by_coverage = 0
    kept = 0
    kept_certainties = 0
    skipped: list[dict[str, str]] = []
    ts = now()

    for rel in rows:
        if _relation_why(rel) != "trigram":
            if _relation_why(rel) == "shared_key":
                kept_certainties += 1
            else:
                skipped.append({"rel_id": rel["rel_id"], "reason": "evidence does not name a scan half"})
            continue

        left = api.get_term(store, rel["src_id"])
        right = api.get_term(store, rel["dst_id"])
        if left is None or right is None:
            skipped.append({"rel_id": rel["rel_id"], "reason": "one side of the pair no longer exists"})
            continue

        left_keys = _keys_for_term(store, rel["src_id"])
        right_keys = _keys_for_term(store, rel["dst_id"])
        verdict = _gate(
            store,
            rel["src_id"],
            left_keys,
            rel["dst_id"],
            right_keys,
            stats,
            floor=floor,
            coverage_min=coverage_min,
            name_in_text_requires_informative=requires_informative,
        )

        gone = next(
            (t["status"] for t in (left, right) if t["status"] in ("merged", "retired")), None
        )
        if gone is not None:
            reason = (
                f"withdrawn: the scan no longer offers this pair -- one side is {gone!r}, "
                f"and a folded or retired term is not a duplicate candidate "
                f"(similarity {verdict['confidence']})"
            )
        elif verdict["surfaced"]:
            kept += 1
            continue
        elif verdict["shared_tokens"]:
            # Build step 1e: this pair DOES share a rare word and was opened
            # for it. Saying "share no token" here would be false, and the
            # operator sweeping `duplicate_coverage_min` would be reading a
            # sentence about the knob they did not turn.
            words = ", ".join(repr(t) for t in verdict["shared_tokens"])
            withdrawn_by_coverage += 1
            reason = (
                f"withdrawn by the duplicate gate's coverage rule: the two names share "
                f"{words} -- rare in this store -- but that covers {verdict['coverage']:.2f} of "
                f"the shorter name's {verdict['informative_shorter']} informative token(s), "
                f"under the {coverage_min:g} bar; neither name is written whole inside the "
                f"other's name or gloss, and their trigram similarity "
                f"{verdict['confidence']} is below the {floor} floor"
            )
        else:
            reason = (
                f"withdrawn by the duplicate gate: the two names share no token carried by fewer "
                f"than {stats.threshold:.1f} of {stats.term_count} terms "
                f"({stats.fraction:g} of them), neither name is written whole inside the other's "
                f"name or gloss, and their trigram similarity {verdict['confidence']} is below "
                f"the {floor} floor"
            )

        if not dry_run:
            update(
                store,
                "term_relation",
                pk_column="rel_id",
                pk_value=rel["rel_id"],
                changes={
                    "status": "rejected",
                    "decided_verb": None,
                    "decided_by_launch": by_launch,
                    "decided_ts": ts,
                    "reason": reason,
                    "confidence": verdict["confidence"],
                },
            )
            append_event(
                store,
                event_type="term_candidate_withdrawn",
                payload={
                    "rel_id": rel["rel_id"],
                    "verb": "same_as",
                    "status": "withdrawn",
                    "src": ["term", rel["src_id"]],
                    "dst": ["term", rel["dst_id"]],
                    "reason": reason,
                    "confidence": verdict["confidence"],
                    "shared_tokens": verdict["shared_tokens"],
                    "coverage": verdict["coverage"],
                    "gate": {
                        "informative_df_threshold": stats.threshold,
                        "informative_token_fraction": stats.fraction,
                        "term_count": stats.term_count,
                        "similarity_floor": floor,
                        "coverage_min": coverage_min,
                        "name_in_text_requires_informative": requires_informative,
                    },
                    "scan": policy.SYSTEM_SCAN_MODEL,
                },
                launch_id=by_launch,
                ts=ts,
            )
        withdrawn.append(
            {"rel_id": rel["rel_id"], "confidence": verdict["confidence"], "reason": reason}
        )

    summary = {
        "status": "ok",
        "dry_run": dry_run,
        "examined": len(rows),
        "withdrawn_count": len(withdrawn),
        # Build step 1e: of those, the ones the COVERAGE bar took -- pairs
        # that share a rare word and were opened for it. A rescan run after a
        # rule change has to say what the rule change did, and the total
        # alone cannot: it mixes 1e's withdrawals in with every pair the
        # gate would have withdrawn anyway.
        "withdrawn_by_coverage": withdrawn_by_coverage,
        "kept": kept,
        "kept_certainties": kept_certainties,
        "skipped_count": len(skipped),
        "skipped": skipped[:MAX_REPORTED_WITHDRAWALS],
        "withdrawn": withdrawn[:MAX_REPORTED_WITHDRAWALS],
        "informative_df_threshold": stats.threshold,
        # The knob the threshold above was computed FROM (build step 1d).
        # Reported because a dry run is how an operator sweeps it, and
        # without it the report cannot distinguish "I moved the fraction and
        # the threshold did not follow because the store is small" from "the
        # fraction I set never reached this code at all" -- which is exactly
        # the sweep that went nowhere and produced this build step.
        "informative_token_fraction": stats.fraction,
        "term_count": stats.term_count,
        "similarity_floor": floor,
        # ...and rule 1e's two, reported for the same reason the fraction is:
        # a sweep of a knob that never reached this code is indistinguishable
        # from a knob that had no work to do unless the run says which value
        # it ran under.
        "coverage_min": coverage_min,
        "name_in_text_requires_informative": requires_informative,
    }
    if not dry_run:
        # One run-level event on top of the per-relation ones, the shape
        # `term_backfill_run`/`term_relink_run` already established: a
        # withdrawal over a 33,785-row queue has to be reconcilable from the
        # ledger as ONE act, not by counting thirty thousand rows.
        append_event(
            store,
            event_type="term_rescan_run",
            payload={k: v for k, v in summary.items() if k != "status"} | {"route": "duplicates"},
            launch_id=by_launch,
            ts=ts,
        )
    return summary
