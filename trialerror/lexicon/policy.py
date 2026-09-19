"""The lexicon's closed vocabularies, its numeric knobs, and the one
``[lexicon]`` config table that overrides them.

Two jobs, kept in one module because they answer the same question from
two directions:

**The vocabularies** mirror the CHECK constraints in
``trialerror.stores.schema.knowledge`` ``_V5``. They exist so the write API
can refuse a bad value with a named error and a readable message *before*
SQLite refuses it with an integrity violation, and so a CLI or a dashboard
can enumerate the legal values without parsing DDL. ``test_lexicon_schema``
asserts each tuple against the live CHECK constraint, so the two cannot
drift apart in silence -- which is the only way a mirrored vocabulary is
worth having.

**The knobs** are the two engram-derived numbers (MINING §5.3/§5.7) plus
the gloss cap:

``REVIEW_AFTER_DAYS``
    engram-F5's type-keyed decay, as a *read-time flag*, never a mutation.
    A sense's ``review_after`` is stamped when it is accepted; the doctor
    check and every read compute ``needs_review`` from it. Nothing in this
    package changes a status because a date passed -- staleness surfaces a
    row for a human, it does not decide anything (MINING §5.7, verbatim
    posture).

    The four values differ because the four origins age differently. An
    imported register row (365 d) was gated once against a corpus that does
    not move; an extracted or hand-written reading (180 d) is one agent's
    reading of one document; an ideation-round coinage (90 d) is the most
    provisional thing the store holds, and the round that coined it is
    usually still running.

``GLOSS_MAX_WORDS``
    The paraphrase cap. See
    :class:`trialerror.lexicon.errors.GlossTooLongError` for why it is a
    grounding rule rather than a formatting one.

``DUPLICATE_BM25_FLOOR``
    The score below which a trigram near-miss is not worth opening a
    ``same_as`` candidate for. Consumed by step E2's
    ``trialerror.lexicon.candidates``; declared here so the knob and its
    override live with every other one rather than in the module that
    happens to read it first.

``DUPLICATE_INFORMATIVE_TOKEN_FRACTION`` / ``DUPLICATE_INFORMATIVE_TOKEN_MIN_DF``
/ ``DUPLICATE_SIMILARITY_FLOOR`` / ``DUPLICATE_STOPWORDS``
    The scan's **second** stage, added at build step 1c once the first real
    register backfill measured what the first stage alone does at corpus
    scale. See :data:`DUPLICATE_CALIBRATION_REFERENCE` for those
    measurements and :data:`DUPLICATE_CALIBRATION_TARGET` for what they are
    set against.

``DUPLICATE_COVERAGE_MIN`` / ``NAME_IN_TEXT_REQUIRES_INFORMATIVE``
    Rule 1e, added on top of the second stage rather than inside it: the
    frequency test says which shared words COUNT, and these two say how
    much of a name has to be covered by them before the pair is opened.
    Both tighten; neither can open a pair the stage below refused. See
    :func:`coverage_of_shorter_name`.

``GLOSS_MAX_WORDS_IMPORT``
    The same cap, raised for the register-import route alone. See the
    constant for why one route gets a different number.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any, Mapping

from trialerror.util.timeutil import now_dt, parse

__all__ = [
    "TERM_STATUSES",
    "GRANULARITIES",
    "ALIAS_KINDS",
    "ORIGIN_KINDS",
    "SENSE_STATUSES",
    "LIVE_SENSE_STATUSES",
    "EVIDENCE_KINDS",
    "RELATION_VERBS",
    "RELATION_STATUSES",
    "RELATION_DECISIONS",
    "MARKED_BY_KINDS",
    "ORIGIN_REF_TABLE",
    "GLOSS_MAX_WORDS",
    "GLOSS_MAX_WORDS_IMPORT",
    "IMPORT_GLOSS_ORIGIN_KINDS",
    "REVIEW_AFTER_DAYS",
    "DUPLICATE_BM25_FLOOR",
    "DUPLICATE_SIMILARITY_FLOOR",
    "DUPLICATE_INFORMATIVE_TOKEN_FRACTION",
    "DUPLICATE_INFORMATIVE_TOKEN_MIN_DF",
    "DUPLICATE_COVERAGE_MIN",
    "NAME_IN_TEXT_REQUIRES_INFORMATIVE",
    "DUPLICATE_STOPWORDS",
    "DUPLICATE_CALIBRATION_REFERENCE",
    "DUPLICATE_CALIBRATION_TARGET",
    "gloss_cap_for",
    "informative_df_threshold",
    "coverage_of_shorter_name",
    "SYSTEM_SCAN_MODEL",
    "RECORD_IMPORT_PROCEDURE_VERSION",
    "EXTRACT_PROCEDURE_VERSION",
    "EXTRACT_HEURISTIC_PROCEDURE_VERSION",
    "MANUAL_PROCEDURE_VERSION",
    "load_policy",
    "review_after_for",
]

# ---------------------------------------------------------------------------
# closed vocabularies -- mirrors of the _V5 CHECK constraints
# ---------------------------------------------------------------------------

#: ``term.status``. ``split`` is a term with more than one current sense and
#: a decided disambiguator on each; ``merged`` is a term folded into
#: another (its rows are all still there, reachable through ``merged_into``).
TERM_STATUSES: tuple[str, ...] = ("proposed", "active", "split", "merged", "retired")

#: ``term.granularity`` -- C-0051's two inventory levels in generic
#: vocabulary. Nullable: a term whose level nobody has decided yet is a
#: normal state, not a defaulted one.
GRANULARITIES: tuple[str, ...] = ("family", "instance")

#: ``term_alias.kind``. ``former_lemma`` is what a merged term's own lemma
#: becomes -- the reason a merge loses no lookup key.
ALIAS_KINDS: tuple[str, ...] = ("variant", "abbreviation", "plural", "former_lemma", "other")

#: ``term_sense.origin_kind`` -- the four proposal routes of design §4.
ORIGIN_KINDS: tuple[str, ...] = ("extract", "record_import", "ideation", "manual")

#: ``term_sense.status``.
SENSE_STATUSES: tuple[str, ...] = ("proposed", "current", "superseded", "rejected", "retired")

#: The two ``term_sense`` statuses that are a LIVE reading -- one the store
#: is still offering as an answer, either as the program's current one or as
#: a proposal awaiting a decision. The grounding law is scoped to exactly
#: these (fix pass, finding F1): :func:`trialerror.lexicon.api.
#: retract_evidence` refuses to leave one of them with no live evidence, and
#: the ``term_sense_without_evidence`` doctor check counts the same
#: population -- so the API guard and the audit cover one set rather than the
#: check being reachable through a route the guard does not watch.
#:
#: The other three are history. A ``rejected`` reading was refused, a
#: ``superseded`` one has a successor carrying its evidence forward, and a
#: ``retired`` one stopped being used and says so -- none of them is the
#: store claiming a reading is grounded now, so retracting the last row under
#: one is a correction to the record rather than a hole in it. Scoping the
#: guard any wider than this would also be a dead end: every escape from
#: "this evidence was wrong and the reading has nothing else" runs through
#: rejecting, retiring or superseding the reading first.
LIVE_SENSE_STATUSES: tuple[str, ...] = ("proposed", "current")

#: ``term_sense_evidence.evidence_kind``.
EVIDENCE_KINDS: tuple[str, ...] = ("quote_anchor", "record", "claim", "idea")

#: ``term_relation.verb`` / ``decided_verb`` -- the engram-F4 verb lock.
RELATION_VERBS: tuple[str, ...] = (
    "same_as",
    "variant_of",
    "conflicts_with",
    "scoped",
    "supersedes",
    "not_conflict",
    "unrelated",
)

#: ``term_relation.status``.
RELATION_STATUSES: tuple[str, ...] = ("pending", "confirmed", "rejected", "superseded")

#: What ``decide_relation`` accepts -- the artboard's three actions, plus
#: ``variant_of`` (a ``same_as`` candidate a judge downgrades), plus
#: ``unrelated`` (confirm-and-close, nothing moves), plus ``rejected``.
#:
#: Narrower than :data:`RELATION_VERBS` on purpose. ``conflicts_with`` is
#: the question a conflict item asks, not an answer to it -- confirming a
#: conflict without resolving it would leave a decided row that still needs
#: deciding. ``supersedes`` is a sense-level operation with its own verb
#: (:func:`trialerror.lexicon.api.supersede_sense`), not a relation verdict.
RELATION_DECISIONS: tuple[str, ...] = (
    "same_as",
    "variant_of",
    "scoped",
    "not_conflict",
    "unrelated",
    "rejected",
)

#: ``term_relation.marked_by_kind`` -- the MINING §5.3 boundary. A
#: ``system`` row may only ever be opened ``pending``.
MARKED_BY_KINDS: tuple[str, ...] = ("system", "launch")

#: ``origin_kind`` -> the knowledge.db table its ``origin_ref`` names. Used
#: for the ``prov_edge(role='derived_from')`` an accepted sense writes;
#: ``manual`` has no origin table (its ``origin_ref`` is NULL), which is
#: why the mapping is a lookup with a legitimate miss rather than a total
#: function.
ORIGIN_REF_TABLE: dict[str, str] = {
    "extract": "claim",
    "record_import": "record",
    "ideation": "idea",
}

# ---------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------

#: Words. See :class:`trialerror.lexicon.errors.GlossTooLongError`.
GLOSS_MAX_WORDS: int = 80

#: The same cap for the register-import route, and only that route (build
#: step 1c, decision D5).
#:
#: The first real backfill refused **271 of 7,475 rows** on the 80-word cap,
#: at 82-93 words apiece. Every one of those refusals was the cap working
#: exactly as designed against a case it was not written for. The cap's
#: argument (see :class:`trialerror.lexicon.errors.GlossTooLongError`) is
#: that a long gloss is a transcription wearing a paraphrase's clothes --
#: which is true of a gloss somebody WRITES, and false of one that is
#: imported: an imported gloss is the source register's own reading, carried
#: across whole so a reviewer can see what the register said, with
#: ``review_after`` already scheduling the second look. Truncating it would
#: be this program editing another program's words; refusing it drops the
#: row on the floor and leaves the term store quietly incomplete. Keeping it
#: whole, flagged for review, is the only option that neither invents text
#: nor loses it.
#:
#: 160 rather than "no cap": the cap still has a job on this route, which is
#: to catch the payload that is not a gloss at all -- a whole section pasted
#: into a description field. Twice the hand-written cap clears every one of
#: the 271 measured refusals with room to spare and still refuses a page.
GLOSS_MAX_WORDS_IMPORT: int = 160

#: The origin kinds the import cap applies to. A tuple rather than a bare
#: equality test so the set is a stated policy, and a short one because the
#: argument above is specifically about a row lifted out of somebody else's
#: register: ``extract`` and ``ideation`` glosses are this program's own
#: words (the 80-word cap is exactly the rule for them), and ``manual`` is
#: someone typing. Note this keys on the ROUTE, not on a row's later
#: history: a launch superseding an imported reading with a hand-written
#: correction is writing its own gloss and gets the hand-written cap, which
#: is why :func:`trialerror.lexicon.api.supersede_sense` does not pass an
#: origin kind here.
IMPORT_GLOSS_ORIGIN_KINDS: tuple[str, ...] = ("record_import",)

#: engram-F5 decay, per ``origin_kind``, in days.
REVIEW_AFTER_DAYS: dict[str, int] = {
    "record_import": 365,
    "manual": 180,
    "extract": 180,
    "ideation": 90,
}

#: FTS5 bm25 scores are negative and better the more negative they are, so
#: a "floor" is an upper bound on the score: a candidate counts only when
#: ``bm25(term_fts) <= DUPLICATE_BM25_FLOOR``. Stated because a positive
#: reading of the name is the natural one and would invert the filter.
DUPLICATE_BM25_FLOOR: float = -0.5

# --- the duplicate scan's second stage (build step 1c) ---------------------
#
# What the first stage does alone, measured on the live program after the
# first real register backfill (2026-09-07) rather than on the 60-row
# fixture, which never showed any of this:
#
#     6,975 terms / 7,204 senses  ->  33,785 pending same_as candidates
#
# Fan-out max 14, mean 4.9 over 6,843 source terms: the top-5 cap fires for
# nearly every term in the store, which is the tell. A cap that is reached
# almost always is not selecting anything -- it is just naming how many rows
# per term the scan will produce, and the answer "five" times seven thousand
# terms is a queue nobody will ever read. 14,826 of those pairs share no
# whole token at all (two names that happen to overlap in three-character
# windows, which is what a trigram index is for and also what it is bad at),
# and only 4,232 share a token common enough to appear in more than 150 of
# the 6,975 terms. Name shapes, for the record: ``lemma_norm`` length deciles
# [3,16,20,24,28,31,35,39,44,52] characters; word counts 1:210 2:1,140
# 3:1,878 4:1,442 5:1,027 6:633 7:335 8:150 9:82 10:45 11+:33. Token
# frequency is heavy-tailed -- 5,186 distinct tokens, of which the 25 most
# common each appear in 115-447 terms and are all generic category words or
# stopwords.
#
# So the second stage asks the question the first one cannot: *is there a
# reason to think these two names are the same thing, beyond the fact that
# some three-letter window matched?* One shared word that is rare in this
# store is such a reason. A high whole-name similarity is such a reason. A
# shared generic category word is not, and neither is a bare trigram
# overlap. Both stages are kept: the first one bounds the work per term, the
# second one decides what a human is asked to look at.
#
# --- amended at build step 1d -------------------------------------------
#
# The first live re-scan of that queue, at the default 2% below, withdrew
# 8,219 of 33,870 pending candidates over 6,992 terms and kept 25,651 --
# eight times the top of DUPLICATE_CALIBRATION_TARGET. Sweeping the fraction
# to 1%, 0.5% and 0.2% changed nothing at all, and the reason was not the
# fraction: `trialerror term scan --rescan` was calling the library without
# the program's config, so no `[lexicon]` value had ever reached this
# module (fixed at 1d, and the dry run now reports the fraction it used
# beside the threshold, so a sweep that goes nowhere says so).
#
# The knob was not what was wrong either. The 1c token test asked whether
# one side's NAME shared an informative word with the other side's INDEXED
# TEXT -- names plus glosses -- and on the real corpus a gloss is 20 to 160
# words of prose, so nearly every pair could find some rare-among-names word
# of one name sitting somewhere in the other's paragraph. Tightening the
# fraction cannot fix that: it makes fewer words informative, but the words
# it removes are the common ones, and the leak runs through the rare ones.
# 1d rescopes the token test to NAMES against NAMES and gives the
# reachability case its own narrow route -- one side's WHOLE name, as a
# phrase, inside the other side's text. See
# :func:`trialerror.lexicon.candidates._gate`.

#: A trigram hit whose two names share no informative token is surfaced
#: anyway when :func:`trialerror.lexicon.normalize.trigram_similarity` of
#: the pair reaches this floor. The escape hatch for the case the token gate
#: is blind to by construction: a misspelling, a run-together compound, a
#: singular/plural pair -- names that ARE nearly the same string and
#: therefore share no whole token to be informative about.
#:
#: 0.5 in Jaccard-over-trigrams is a strong statement about two short names
#: -- half of all the three-character windows either name has, the other one
#: has too. A pair of three-word names differing in one word lands near
#: 0.6-0.7; a pair sharing only a category word lands near 0.2-0.4.
DUPLICATE_SIMILARITY_FLOOR: float = 0.5

#: A token is INFORMATIVE when it appears in fewer than this fraction of the
#: store's terms. 2% is set against the reference distribution above: 2% of
#: 6,975 terms is 139.5, which sits just under the 150-term line that 4,232
#: of the 33,785 pairs were measured against, and comfortably under the
#: 115-447 band the 25 most common tokens occupy. A token every fiftieth
#: term carries is a category, not a name.
#:
#: Expressed as a fraction rather than a count because it has to hold at
#: both ends of the store's life -- the same absolute threshold that is
#: right at 7,000 terms would call every token in a 200-term store
#: informative, and every token in a 70,000-term one generic.
DUPLICATE_INFORMATIVE_TOKEN_FRACTION: float = 0.02

#: ...with a floor under the fraction, because a fraction of a small store
#: is not a frequency. At 60 terms, 2% is 1.2, and no token shared by two
#: names could ever clear it -- the gate would silently become "similarity
#: only" on every small store, including every test fixture and every
#: program in its first week. Below this many terms carrying it, a token is
#: informative regardless of the fraction: in a store where the commonest
#: word appears twice, twice is rare.
DUPLICATE_INFORMATIVE_TOKEN_MIN_DF: int = 3

#: **Rule 1e.** How much of the shorter name the shared informative tokens
#: have to cover before the token route opens a candidate.
#:
#: The frequency test above answers "is this word rare enough to mean
#: something"; it says nothing about how much of either NAME the word
#: accounts for. One rare word shared between a two-word name and a
#: nine-word one is a coincidence with a rare word in it -- the same word
#: shared between two two-word names is half of each of them. Coverage is
#: that ratio, measured against the SHORTER side because the shorter name is
#: the one a shared word can plausibly be most of: `shared / min(informative
#: tokens either side carries)`, compared with ``>=`` so a name of two
#: informative tokens sharing exactly one passes.
#:
#: 0.5 is "at least half of the smaller name is the thing they have in
#: common". Set at the rule's adoption rather than measured -- the flood
#: fixture's names are `<specific> <category>`, one informative token each,
#: so they cover 1.0 and the fixture cannot distinguish 0.5 from 0.1. What
#: the fixture DOES establish is that the rule costs nothing there (the
#: report's before/after table), and the live rescan is where its price is
#: read.
#:
#: **Set to 0 and the rule is off** -- every value of ``shared`` covers 0,
#: which is the pre-1e gate exactly. A value outside [0, 1] is a typo rather
#: than a setting (nothing can cover more than all of a name) and falls back
#: to this default, the same way every other mistyped knob here does.
DUPLICATE_COVERAGE_MIN: float = 0.5

#: **Rule 1e, the ``name_in_text`` half.** Whether a name found written
#: whole inside the other side's text has to carry at least one informative
#: token to count.
#:
#: The route asks "is this name, entire, written over there", and for a name
#: that is nothing but function words plus the store's family word the answer
#: is yes in half the glosses in the store -- prose contains "the record" and
#: "of the table" constantly. Such a name being present is a fact about
#: English, not evidence that two terms are one thing, and it arrives on the
#: route that was built to be the NARROW question.
#:
#: ``True`` by default, which is a tightening: a name with no informative
#: token of its own stops qualifying by containment. ``false`` restores the
#: pre-1e behaviour. The name still counts for the other two routes -- the
#: token route never looked at it (it has no informative token to share) and
#: similarity measures the whole string, function words included.
NAME_IN_TEXT_REQUIRES_INFORMATIVE: bool = True

#: Words that never count as informative however rare they are in a
#: particular store. Function words only -- articles, conjunctions,
#: prepositions, copulas, bare determiners -- and the test they are chosen
#: by is: *if these two names shared only this word, would that be evidence
#: they name one thing?* For every word below the answer is no in any
#: domain, which is why the list carries no vocabulary from any corpus (a
#: domain stopword list would be exactly the corpus-specific content this
#: repo does not hold).
#:
#: They are excluded from the TOKEN gate only. Similarity still measures the
#: whole name, function words included, because "hold to" and "hold at" are
#: two names and the difference between them is the preposition.
DUPLICATE_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "been", "being", "between",
        "both", "but", "by", "each", "for", "from", "if", "in", "into", "is", "it",
        "its", "no", "not", "of", "on", "or", "other", "per", "so", "such", "than",
        "that", "the", "their", "then", "there", "these", "this", "those", "to",
        "under", "until", "upon", "via", "was", "were", "when", "which", "while",
        "with", "within", "without",
    }
)

#: The measured distribution the two knobs above are set against, kept as
#: data so a later re-calibration has something to compare with instead of a
#: paragraph to re-read. Numbers only -- what they were measured over is the
#: sandbox program's own business and does not belong in this repo.
#:
#: The ``rescan_*`` keys were added at build step 1d and are a DIFFERENT
#: measurement of the same store, taken later and after more importing: the
#: first live ``term scan --rescan --dry-run``, under the 1c gate, at the
#: default fraction. They are kept beside the 1c numbers rather than
#: replacing them because the whole use of this dict is to make a
#: re-calibration comparable with what came before.
DUPLICATE_CALIBRATION_REFERENCE: dict[str, float] = {
    "record_rows": 7475,
    "terms": 6975,
    "senses": 7204,
    "pairs_opened_first_stage_only": 33785,
    "source_terms": 6843,
    "fan_out_max": 14,
    "fan_out_mean": 4.9,
    "pairs_sharing_no_token": 14826,
    "pairs_sharing_a_token_in_over_150_terms": 4232,
    "distinct_tokens": 5186,
    "top_25_token_df_min": 115,
    "top_25_token_df_max": 447,
    "glosses_over_80_words": 271,
    # --- the first live rescan (build step 1d), under the 1c gate ---
    "rescan_terms": 6992,
    "rescan_pairs_examined": 33870,
    "rescan_withdrawn": 8219,
    "rescan_kept": 25651,
}

#: What a scan over that distribution should cost a reviewer: **hundreds to
#: low thousands** of open candidates, not tens of thousands. The band is
#: the target, not a measurement -- it says what "calibrated" means so a
#: future run can be judged against a number rather than against a feeling.
#:
#: The reasoning behind the shape of the band: a queue of a few hundred is a
#: week of review; a few thousand is a quarter of it and still bounded; the
#: 33,785 the first stage produced alone is not a queue at all, because
#: nobody will ever start it. The lower end matters too -- a scan that opens
#: nothing has stopped doing its job, and a store this size certainly holds
#: real duplicates.
#:
#: **The 1c projection stated here was that the token arm alone would leave
#: on the order of 1.5e4 pairs standing -- below the first stage, above this
#: band -- so the fraction knob would need tightening once a real re-scan
#: reported. The re-scan reported (acceptance check I), and both halves of
#: that were wrong in an instructive way**: it left 25,651, well above the
#: projection, and the fraction was not the knob that could have moved it
#: (see the amendment above the two constants). 1d rescopes the token route
#: instead.
#:
#: **The band itself is still un-met and still a target rather than a
#: measurement.** The sweep is the orchestrator's to re-run on the sandbox
#: program under the 1d gate, and what that run reports is what decides
#: whether the fraction now has real work to do.
DUPLICATE_CALIBRATION_TARGET: tuple[int, int] = (100, 3000)

#: ``term_relation.marked_by_model`` for machine-opened candidates. A
#: system row's "model" is the scan that opened it, not an LLM -- the whole
#: point of the trigram half is that it is deterministic and free.
SYSTEM_SCAN_MODEL: str = "scan-v1"

#: ``procedure_version`` for the register-import route (ruling L-E2, as
#: amended at the lane merge -- finding F6). The literal is deliberately
#: generic: it ships to the public repo as a module constant, and a
#: procedure version names the PROCEDURE, not the corpus one program happened
#: to run it over.
RECORD_IMPORT_PROCEDURE_VERSION: str = "register-import-v1"

#: ``procedure_version`` for the extraction route's two halves: the envelope
#: carrying an explicit ``term`` key, and the substring-of-an-entity-name
#: fallback. Only the first may land ``current`` (design §4).
EXTRACT_PROCEDURE_VERSION: str = "extract-term-v1"
EXTRACT_HEURISTIC_PROCEDURE_VERSION: str = "extract-heuristic-v1"

#: ``procedure_version`` for a hand-written proposal.
MANUAL_PROCEDURE_VERSION: str = "manual-v1"


def load_policy(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Resolve the knobs against a program's ``[lexicon]`` config table.

    ``config`` is a plain ``ProgramConfig.raw`` dict (or ``None`` -- most
    tests and every un-configured program), read with the same "read
    generically, tolerate absence" convention every other subsystem's
    private config loader uses. Unknown keys are ignored rather than
    refused: a config that mentions a knob a later version added must not
    stop an older harness from opening the store.

    Returns a dict with ``gloss_max_words``, ``gloss_max_words_import``,
    ``review_after_days`` (a full four-key mapping, defaults filled in for
    anything the table omits), ``duplicate_bm25_floor``,
    ``duplicate_similarity_floor``,
    ``duplicate_informative_token_fraction``,
    ``duplicate_informative_token_min_df``, ``duplicate_coverage_min`` and
    ``name_in_text_requires_informative``. Values of the wrong type fall
    back to the default for that key -- a typo in a TOML number is a bad
    knob, not a reason to refuse to run the lexicon at all.

    The last two are rule 1e's and arrive with every existing configuration
    already set to them: a program that has never heard of the keys gets
    today's gate PLUS the new rule, which is a tightening (fewer candidates
    opened, never more) and is stated as such in the guide.
    """
    table: Mapping[str, Any] = {}
    if config:
        candidate = config.get("lexicon")
        if isinstance(candidate, Mapping):
            table = candidate

    def _int(key: str, default: int) -> int:
        value = table.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default

    def _float(key: str, default: float) -> float:
        value = table.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    def _fraction(key: str, default: float) -> float:
        """A ``_float`` that also refuses a value outside [0, 1].

        Only for knobs that are a SHARE of something. Nothing can cover more
        than all of a name, so ``duplicate_coverage_min = 50`` is somebody
        writing a percentage where a fraction goes -- and silently reading it
        as "unsatisfiable" would turn the token route off across a whole
        store without a word anywhere saying so."""
        value = _float(key, default)
        return value if 0.0 <= value <= 1.0 else default

    def _bool(key: str, default: bool) -> bool:
        value = table.get(key)
        return value if isinstance(value, bool) else default

    days_table: Mapping[str, Any] = {}
    candidate_days = table.get("review_after_days")
    if isinstance(candidate_days, Mapping):
        days_table = candidate_days

    review_after_days = dict(REVIEW_AFTER_DAYS)
    for origin_kind, default in REVIEW_AFTER_DAYS.items():
        value = days_table.get(origin_kind)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            review_after_days[origin_kind] = value
        else:
            review_after_days[origin_kind] = default

    gloss_max_words = _int("gloss_max_words", GLOSS_MAX_WORDS)
    return {
        "gloss_max_words": gloss_max_words,
        # The import cap can never be STRICTER than the hand-written one: an
        # imported gloss is kept whole precisely because nobody here wrote
        # it, so a program that tightens the base cap must not thereby start
        # refusing register rows a hand-written gloss of the same length
        # would sail through. Configuring the import knob DOWN below the base
        # cap is therefore a no-op, and configuring the base cap up carries
        # the import cap with it.
        "gloss_max_words_import": max(
            gloss_max_words, _int("gloss_max_words_import", GLOSS_MAX_WORDS_IMPORT)
        ),
        "review_after_days": review_after_days,
        "duplicate_bm25_floor": _float("duplicate_bm25_floor", DUPLICATE_BM25_FLOOR),
        "duplicate_similarity_floor": _float(
            "duplicate_similarity_floor", DUPLICATE_SIMILARITY_FLOOR
        ),
        "duplicate_informative_token_fraction": _float(
            "duplicate_informative_token_fraction", DUPLICATE_INFORMATIVE_TOKEN_FRACTION
        ),
        "duplicate_informative_token_min_df": _int(
            "duplicate_informative_token_min_df", DUPLICATE_INFORMATIVE_TOKEN_MIN_DF
        ),
        "duplicate_coverage_min": _fraction("duplicate_coverage_min", DUPLICATE_COVERAGE_MIN),
        "name_in_text_requires_informative": _bool(
            "name_in_text_requires_informative", NAME_IN_TEXT_REQUIRES_INFORMATIVE
        ),
    }


def gloss_cap_for(origin_kind: str | None, *, policy: Mapping[str, Any] | None = None) -> int:
    """The word cap a gloss arriving by ``origin_kind`` is measured against.

    One function rather than a conditional at each call site, because "which
    cap applies here" is a policy question and there are two callers who
    would otherwise each answer it: the propose route, which knows the
    origin, and :func:`trialerror.lexicon.api.supersede_sense`, which
    deliberately does not pass one (see
    :data:`IMPORT_GLOSS_ORIGIN_KINDS`). ``None`` -- an unknown or unstated
    route -- gets the strict cap, which is the safe direction: the worst
    outcome is a refusal a caller can see and argue with, rather than a
    transcription landing unremarked.
    """
    resolved = policy if policy is not None else load_policy(None)
    if origin_kind in IMPORT_GLOSS_ORIGIN_KINDS:
        value = resolved.get("gloss_max_words_import", GLOSS_MAX_WORDS_IMPORT)
    else:
        value = resolved.get("gloss_max_words", GLOSS_MAX_WORDS)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else GLOSS_MAX_WORDS


def informative_df_threshold(
    term_count: int, *, policy: Mapping[str, Any] | None = None
) -> float:
    """The document-frequency line below which a token counts as
    informative, for a store holding ``term_count`` terms.

    ``max(fraction * term_count, min_df)`` -- the fraction is the rule the
    corpus-scale calibration is stated in, the floor is what keeps it from
    degenerating on a small store (see
    :data:`DUPLICATE_INFORMATIVE_TOKEN_MIN_DF`). Returned as a float and
    compared with ``<`` by the caller, so a token in exactly 2% of the terms
    is NOT informative -- the fraction names the generic side of the line.
    """
    resolved = policy if policy is not None else load_policy(None)
    fraction = resolved.get("duplicate_informative_token_fraction", DUPLICATE_INFORMATIVE_TOKEN_FRACTION)
    min_df = resolved.get("duplicate_informative_token_min_df", DUPLICATE_INFORMATIVE_TOKEN_MIN_DF)
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or fraction < 0:
        fraction = DUPLICATE_INFORMATIVE_TOKEN_FRACTION
    if not isinstance(min_df, int) or isinstance(min_df, bool) or min_df < 0:
        min_df = DUPLICATE_INFORMATIVE_TOKEN_MIN_DF
    return max(float(fraction) * max(0, int(term_count)), float(min_df))


def coverage_of_shorter_name(
    shared_count: int, left_count: int, right_count: int
) -> float:
    """Rule 1e's coverage: what share of the SHORTER name's informative
    tokens the two names' shared informative tokens account for.

    ``shared_count`` is how many informative tokens the two names have in
    common; ``left_count``/``right_count`` are how many each side carries.
    The denominator is the smaller of the two because the question the rule
    asks is "is this shared word most of a name", and the only name it can
    be most of is the shorter one -- taking the longer side, or the union,
    would make a pair of near-identical short names fail because one of them
    happens to have a long alias.

    Zero on the shorter side is zero coverage rather than a division: a name
    with no informative token of its own has nothing for a shared word to be
    a share OF, and it cannot have contributed to ``shared_count`` in the
    first place (a shared informative token is informative on both sides), so
    the case is unreachable from :func:`trialerror.lexicon.candidates._gate`
    and defined here only so the function is total.
    """
    shorter = min(int(left_count), int(right_count))
    if shorter <= 0 or shared_count <= 0:
        return 0.0
    return float(shared_count) / float(shorter)


def review_after_for(
    origin_kind: str,
    *,
    at: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> str:
    """The ``review_after`` stamp for a sense of ``origin_kind``, accepted at
    ``at`` (default: now).

    An unknown ``origin_kind`` gets the shortest configured window rather
    than no window at all -- fail toward "a human looks at this sooner",
    never toward "this never comes up again".
    """
    days_table = (policy or {}).get("review_after_days") if policy else None
    days_table = days_table if isinstance(days_table, Mapping) else REVIEW_AFTER_DAYS
    days = days_table.get(origin_kind)
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        days = min(days_table.values()) if days_table else min(REVIEW_AFTER_DAYS.values())

    base = parse(at) if at else now_dt()
    dt = (base + timedelta(days=days)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
