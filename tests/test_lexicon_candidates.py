"""``trialerror.lexicon.candidates`` -- save-time duplicate surfacing.

The module's whole job is to be **noisy in a way that costs nothing**: it
guesses, and every guess is a pending row a launch can reject. So these
tests are organised around what must be true of a guess rather than around
how good the guesses are:

1. *It finds the two kinds of hit it claims to.* An exact lookup-key
   collision (a certainty) and a trigram near-miss (a score).
2. *It never decides.* Every row it opens is ``pending``, ``system``-marked
   and unattributed -- there is no argument to any function here that
   confirms one.
3. *It does not re-ask an answered question.* A pair that already has a
   relation of any status -- including a ``rejected`` one -- is skipped, so
   running the scan twice, or after a rejection, opens nothing new.
4. *Reading is free.* ``find_candidates`` writes nothing and can run on a
   store nobody is allowed to write to.
"""

from __future__ import annotations

import pytest

from trialerror.lexicon import api, candidates, policy
from trialerror.lexicon.errors import LaunchRequiredError
from trialerror.stores.errors import XidTargetMissingError
from trialerror.util.timeutil import now

from tests._lexicon_fixtures import specific_words
from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


def _propose(store, ids, lemma, **overrides):
    kwargs = {
        "lemma": lemma,
        "gloss": f"how this program reads {lemma}",
        "origin_kind": "manual",
        "origin_ref": None,
        "evidence": [f"anchor:{ids['quote_anchor']}"],
        "by_launch": ids["launch"],
        "procedure_version": policy.MANUAL_PROCEDURE_VERSION,
    }
    kwargs.update(overrides)
    return api.propose(store, **kwargs)


#: Six unrelated names, seeded before any test that depends on a trigram
#: SCORE rather than on a match. bm25 weighs a hit by how rare its tokens
#: are, so in a two-row index every hit scores about -0.5 -- right on
#: ``DUPLICATE_BM25_FLOOR`` -- while the same hit in an eight-row index
#: scores -1.5. A lexicon with two terms in it is not a lexicon, and a test
#: that pretends otherwise is measuring the fixture, not the module.
_DECOY_LEMMAS = (
    "guard ring",
    "noise floor",
    "burst count",
    "ramp rate",
    "cold junction",
    "marker offset",
)


def _seed_decoys(store, ids):
    for lemma in _DECOY_LEMMAS:
        _propose(store, ids, lemma)


def _relations(store):
    return [dict(r) for r in store.knowledge.execute("SELECT * FROM term_relation").fetchall()]


def _same_as(store):
    return [r for r in _relations(store) if r["verb"] == "same_as"]


# ---------------------------------------------------------------------------
# the two halves
# ---------------------------------------------------------------------------


def test_an_exact_key_collision_is_surfaced_as_a_certainty(store, ids):
    """Two terms reachable by one lookup key is not a near-miss: ``find_term``
    has to pick one of them, and which one it picks is currently an accident
    of insertion order."""
    first = _propose(store, ids, "settling time")
    second = _propose(store, ids, "recovery interval", aliases=["settling time"])

    hits = candidates.find_candidates(store, second["term_id"])
    assert [h["term_id"] for h in hits] == [first["term_id"]]
    assert hits[0]["why"] == "shared_key"
    assert hits[0]["confidence"] == 1.0
    assert hits[0]["matched_on"] == "settling time"


def test_a_near_miss_on_the_name_is_surfaced_with_its_score(store, ids):
    _seed_decoys(store, ids)
    first = _propose(store, ids, "settling time")
    second = _propose(store, ids, "settling period")

    hits = candidates.find_candidates(store, second["term_id"])
    assert [h["term_id"] for h in hits] == [first["term_id"]]
    assert hits[0]["why"] == "trigram"
    assert hits[0]["score"] <= policy.DUPLICATE_BM25_FLOOR
    assert hits[0]["lemma"] == "settling time"


def test_two_unrelated_names_surface_nothing(store, ids):
    _seed_decoys(store, ids)
    _propose(store, ids, "settling time")
    other = _propose(store, ids, "aperture mask")
    assert candidates.find_candidates(store, other["term_id"]) == []
    assert _same_as(store) == []


def test_the_query_is_built_from_names_not_glosses(store, ids):
    """The measured reason the module reads the way it does: two terms whose
    glosses are written in the same register share "the", "records",
    "procedure" -- and a query carrying those outranks the names, which is
    how a 60-row fixture produced 159 candidates instead of 18."""
    _seed_decoys(store, ids)
    _propose(store, ids, "aperture mask", gloss="the value this procedure records for the reading")
    other = _propose(store, ids, "shutter lag", gloss="the value this procedure records for the reading")
    assert candidates.find_candidates(store, other["term_id"]) == []


def test_a_gloss_containing_another_terms_name_is_still_reachable(store, ids):
    """The index does span glosses, so a term defined in terms of another
    one is found from that other one's side -- which is the half of "over
    lemma and gloss" that survives."""
    _seed_decoys(store, ids)
    defined = _propose(store, ids, "aperture mask", gloss="the interval a settling time is measured over")
    api.accept_sense(store, defined["sense_id"], by_launch=ids["launch"])
    named = _propose(store, ids, "settling time")
    assert defined["term_id"] in [h["term_id"] for h in candidates.find_candidates(store, named["term_id"])]


# ---------------------------------------------------------------------------
# what surfacing writes
# ---------------------------------------------------------------------------


def test_surfacing_opens_one_pending_unattributed_system_row_per_hit(store, ids):
    _seed_decoys(store, ids)
    first = _propose(store, ids, "settling time")
    second = _propose(store, ids, "settling period")

    rows = _same_as(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending"
    assert row["marked_by_kind"] == "system"
    assert row["marked_by_launch"] is None
    assert row["marked_by_model"] == policy.SYSTEM_SCAN_MODEL
    assert row["decided_verb"] is None and row["decided_ts"] is None
    # newcomer -> the term that was already there, which is the direction
    # decide_relation's default canonical assumes.
    assert row["src_id"] == second["term_id"] and row["dst_id"] == first["term_id"]

    event = store.ops.execute(
        "SELECT launch_id FROM event WHERE type = 'term_relation_opened'"
    ).fetchone()
    assert event["launch_id"] is None, "a scan has no launch to name"


def test_propose_reports_what_the_scan_did(store, ids):
    _seed_decoys(store, ids)
    _propose(store, ids, "settling time")
    second = _propose(store, ids, "settling period")
    surfaced = second["candidates"]
    assert surfaced["status"] == "ok"
    assert len(surfaced["opened"]) == 1
    assert surfaced["opened"][0]["lemma"] == "settling time"
    assert surfaced["sense_id"] == second["sense_id"], "which proposal raised it"


def test_the_opened_row_carries_the_evidence_of_its_own_guess(store, ids):
    import json

    _seed_decoys(store, ids)
    _propose(store, ids, "settling time")
    second = _propose(store, ids, "settling period")
    row = _same_as(store)[0]
    evidence = json.loads(row["evidence"])
    assert evidence["why"] == "trigram"
    assert evidence["sense_id"] == second["sense_id"]
    assert evidence["scan"] == policy.SYSTEM_SCAN_MODEL
    assert evidence["score"] <= policy.DUPLICATE_BM25_FLOOR


# ---------------------------------------------------------------------------
# not re-asking an answered question
# ---------------------------------------------------------------------------


def test_a_second_scan_opens_nothing_and_says_so(store, ids):
    _seed_decoys(store, ids)
    _propose(store, ids, "settling time")
    second = _propose(store, ids, "settling period")
    before = len(_relations(store))

    again = candidates.surface_candidates(store, second["term_id"])
    assert again["opened"] == []
    assert len(again["existing"]) == 1
    assert again["existing"][0]["status"] == "pending"
    assert len(_relations(store)) == before


def test_a_rejected_candidate_is_not_reopened(store, ids):
    """A rejection is a row precisely so that the scan can see it. Reopening
    the same pair would turn a decision into a treadmill."""
    _seed_decoys(store, ids)
    _propose(store, ids, "settling time")
    second = _propose(store, ids, "settling period")
    rel_id = _same_as(store)[0]["rel_id"]
    api.decide_relation(store, rel_id, decision="rejected", by_launch=ids["launch"])

    again = candidates.surface_candidates(store, second["term_id"])
    assert again["opened"] == []
    assert again["existing"][0]["rel_id"] == rel_id
    assert [r["rel_id"] for r in _same_as(store)] == [rel_id]


def test_a_merged_term_is_not_offered_as_a_candidate(store, ids):
    """A merge deletes nothing, so the folded row keeps its own
    ``lemma_norm`` and an exact-key lookup still reaches it. It must not be
    offered: proposing a merge INTO a merged term is the double
    ``merged_into`` chain ``merge_terms`` refuses outright. The term it was
    folded into is offered instead -- it now owns that key as an alias."""
    keep = _propose(store, ids, "settling time")
    other = _propose(store, ids, "settling period")
    api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])

    third = _propose(store, ids, "recovery interval", aliases=["settling period"])
    hits = [h["term_id"] for h in candidates.find_candidates(store, third["term_id"])]
    assert other["term_id"] not in hits, "the folded row is a dead end, not a duplicate"
    assert hits == [keep["term_id"]]


def test_a_retired_term_is_not_offered_either(store, ids):
    _seed_decoys(store, ids)
    first = _propose(store, ids, "settling time")
    api.accept_sense(store, first["sense_id"], by_launch=ids["launch"])
    api.retire_sense(store, first["sense_id"], by_launch=ids["launch"])
    from trialerror.stores.writer import update

    update(store, "term", pk_column="term_id", pk_value=first["term_id"],
           changes={"status": "retired"})

    second = _propose(store, ids, "settling period")
    assert candidates.find_candidates(store, second["term_id"]) == []


# ---------------------------------------------------------------------------
# reading is free, and cannot be steered
# ---------------------------------------------------------------------------


def test_find_candidates_writes_nothing(store, ids):
    _seed_decoys(store, ids)
    _propose(store, ids, "settling time")
    second = _propose(store, ids, "settling period")
    before = len(_relations(store))
    events_before = store.ops.execute("SELECT count(*) FROM event").fetchone()[0]

    candidates.find_candidates(store, second["term_id"])

    assert len(_relations(store)) == before
    assert store.ops.execute("SELECT count(*) FROM event").fetchone()[0] == events_before


def test_an_unknown_term_has_no_candidates_rather_than_raising(store, ids):
    assert candidates.find_candidates(store, "TERM-does-not-exist") == []


def test_a_lemma_full_of_fts_operators_is_data_not_syntax(store, ids):
    """A lemma is text somebody proposed. If it reached the MATCH query
    unquoted, the store's own scan would be steerable by the text of a term
    -- so every token goes in as a quoted phrase and a hostile lemma simply
    finds nothing."""
    _seed_decoys(store, ids)
    _propose(store, ids, "settling time")
    hostile = _propose(store, ids, 'settling AND NEAR("time" OR *) "unbalanced')
    hits = candidates.find_candidates(store, hostile["term_id"])
    assert all(h["term_id"] != hostile["term_id"] for h in hits)


def test_a_term_invisible_to_the_index_is_still_found_by_its_key(store, ids):
    """``term_fts`` is API-maintained, so the fixture's hand-inserted term is
    not in it. The exact-key half reads the tables directly and sees it
    anyway -- the asymmetry the module docstring states, asserted."""
    collides = _propose(store, ids, "recovery interval", aliases=["Test Term"])
    hits = candidates.find_candidates(store, collides["term_id"])
    assert [h["term_id"] for h in hits] == [ids["term"]]
    assert hits[0]["why"] == "shared_key"


# ---------------------------------------------------------------------------
# the second stage: the gate, and the score every surfaced row now carries
# (build step 1c -- see tests/test_lexicon_calibration.py for the corpus-scale
# measurement this half is calibrated against)
# ---------------------------------------------------------------------------


def _specifics(n: int = 4) -> tuple[str, ...]:
    """Invented words that are mutually unlike by construction -- borrowed
    from the calibration fixture so a test that needs "two names sharing
    only a common word" gets exactly that and not an accidental letter
    overlap."""
    return specific_words(n)


def test_a_shared_generic_word_is_not_a_reason_to_surface_anything(store, ids):
    """The 1c gate, on the shape that produced 33,785 candidates from 6,975
    terms: three names ending in the same category word. The third one makes
    that word common enough to stop being informative, and the pair falls
    out -- while the FIRST stage still finds it, which is what the flood
    was."""
    _seed_decoys(store, ids)
    words = _specifics(3)
    for word in words[:2]:
        _propose(store, ids, f"{word} profile")
    third = _propose(store, ids, f"{words[2]} profile")

    assert candidates.find_candidates(store, third["term_id"]) == []
    ungated = candidates.find_candidates(store, third["term_id"], gate=False)
    assert ungated, "the bm25 stage still finds them -- the gate is what drops them"
    assert all(hit["gate"] is None for hit in ungated)
    assert all(hit["shared_tokens"] == [] for hit in ungated)


def test_a_shared_rare_word_still_surfaces(store, ids):
    """The other side of the same rule: one shared word that almost nothing
    else in the store carries is exactly what a duplicate looks like."""
    _seed_decoys(store, ids)
    word = _specifics(1)[0]
    first = _propose(store, ids, f"{word} profile")
    second = _propose(store, ids, f"{word} interval")

    hits = candidates.find_candidates(store, second["term_id"])
    assert [h["term_id"] for h in hits] == [first["term_id"]]
    assert hits[0]["gate"] == "informative_token"
    assert hits[0]["shared_tokens"] == [word]


def test_two_nearly_identical_names_surface_without_sharing_a_word(store, ids):
    """The similarity arm, on the case the token arm cannot see: a
    run-together compound shares no whole word with its spaced spelling
    BECAUSE it is nearly the same string. A gate with only the token arm
    would drop it.

    Proposed in this order because the first stage is a phrase match and
    only reaches the compound from the spaced side: "shutter" is a
    substring of "shutterlag", while "shutterlag" is a substring of
    nothing. That asymmetry is the FTS layer's, not the gate's, and it is
    the reason this pair is worth a test of its own."""
    _seed_decoys(store, ids)
    first = _propose(store, ids, "shutterlag")
    second = _propose(store, ids, "shutter lag")

    hits = candidates.find_candidates(store, second["term_id"])
    assert [h["term_id"] for h in hits] == [first["term_id"]]
    assert hits[0]["gate"] == "similarity"
    assert hits[0]["shared_tokens"] == []
    assert hits[0]["confidence"] >= policy.DUPLICATE_SIMILARITY_FLOOR


def test_every_surfaced_row_carries_a_confidence(store, ids):
    """Decision D1: a system-opened candidate never records ``None`` again.
    The bm25 score is kept where it belongs -- in the evidence, as the
    reason the pair was looked at -- and the confidence is the one number
    that means the same thing in every store and every year."""
    import json

    _seed_decoys(store, ids)
    word = _specifics(1)[0]
    _propose(store, ids, f"{word} profile")
    _propose(store, ids, f"{word} interval")

    row = _same_as(store)[0]
    assert row["confidence"] is not None
    assert 0.0 < row["confidence"] <= 1.0
    evidence = json.loads(row["evidence"])
    assert evidence["similarity"] == row["confidence"]
    assert evidence["gate"] == "informative_token"
    assert evidence["shared_tokens"] == [word]
    assert evidence["score"] <= policy.DUPLICATE_BM25_FLOOR, "the ranking score is still recorded"
    assert word in (row["reason"] or ""), "the reason names why, in words"


def test_an_exact_key_collision_ignores_the_gate_entirely(store, ids):
    """A shared lookup key is not a score and no threshold applies to it.
    Even when the two names share nothing informative and look nothing
    alike, ``find_term`` still has to pick one of them."""
    _seed_decoys(store, ids)
    words = _specifics(3)
    for word in words[:2]:
        _propose(store, ids, f"{word} profile")
    first = _propose(store, ids, f"{words[2]} profile")
    second = _propose(store, ids, "recovery interval", aliases=[f"{words[2]} profile"])

    hits = candidates.find_candidates(store, second["term_id"])
    assert [h["term_id"] for h in hits] == [first["term_id"]]
    assert hits[0]["confidence"] == 1.0
    assert hits[0]["gate"] == "shared_key"


def test_a_function_word_never_counts_however_rare_it_is(store, ids):
    """Stopwords are excluded from the token arm by name, not by frequency:
    in a small store "of" may well be rare, and two names sharing only "of"
    are still two names sharing nothing."""
    stats = candidates.token_stats(store)
    assert stats.is_informative("of") is False
    assert stats.is_informative("the") is False
    assert stats.is_informative("between") is False
    assert stats.is_informative(_specifics(1)[0]) is True


def test_what_counts_as_common_is_read_from_the_store_each_time(store, ids):
    """The frequencies are a live measurement, not a shipped list -- what is
    generic is a property of the corpus somebody imported. The same pair is
    surfaced before the word becomes common and not after."""
    _seed_decoys(store, ids)
    words = _specifics(3)
    first = _propose(store, ids, f"{words[0]} profile")
    second = _propose(store, ids, f"{words[1]} profile")
    assert [h["term_id"] for h in candidates.find_candidates(store, second["term_id"])] == [
        first["term_id"]
    ], "two names carry 'profile'; it is still rare"

    _propose(store, ids, f"{words[2]} profile")
    assert candidates.find_candidates(store, second["term_id"]) == [], "three carry it; it is not"


def test_a_gloss_that_names_another_term_survives_the_gate(store, ids):
    """The E2 property that a term defined in terms of another is reachable
    from it, preserved rather than quietly dropped -- and after build step
    1d it is the ``name_in_text`` route that preserves it, on the WHOLE
    name rather than on one of its words."""
    _seed_decoys(store, ids)
    word = _specifics(1)[0]
    defined = _propose(store, ids, "aperture mask", gloss=f"the interval a {word} profile is measured over")
    api.accept_sense(store, defined["sense_id"], by_launch=ids["launch"])
    named = _propose(store, ids, f"{word} profile")

    hits = candidates.find_candidates(store, named["term_id"])
    assert defined["term_id"] in [h["term_id"] for h in hits]
    assert hits[0]["gate"] == "name_in_text"
    assert hits[0]["name_in_text"] == f"{word} profile", "the whole name, as the gloss writes it"
    assert hits[0]["shared_tokens"] == [], "and not through a word the two NAMES never shared"


def test_one_word_of_a_name_inside_the_other_gloss_is_no_longer_enough(store, ids):
    """The leak build step 1d closed, at unit scale.

    Under the 1c gate this pair surfaced: that gate ran one side's NAME
    tokens against the other side's indexed TEXT, so a word of one name
    turning up anywhere in the other's prose counted as a shared word. On a
    real corpus, where a gloss runs twenty to a hundred and sixty words,
    that is nearly every pair -- 25,651 of 33,870 candidates survived the
    live rescan because of it. Here the two names share nothing, and neither
    name is written whole in the other's gloss; only one word of one of them
    is."""
    _seed_decoys(store, ids)
    word = _specifics(1)[0]
    defined = _propose(
        store, ids, "aperture mask",
        gloss=f"the interval a {word} is measured over, taken again while the {word} settles",
    )
    api.accept_sense(store, defined["sense_id"], by_launch=ids["launch"])
    named = _propose(store, ids, f"{word} profile")

    ungated = candidates.find_candidates(store, named["term_id"], gate=False)
    assert defined["term_id"] in [h["term_id"] for h in ungated], "the bm25 stage still finds it"

    # The 1c rule spelled out, so this is a test about a leak that was
    # closed rather than about a gate that happens to say no: the word IS
    # rare among names, and it IS in the other side's indexed text.
    stats = candidates.token_stats(store)
    assert stats.is_informative(word)
    indexed = candidates._indexed_text(
        store, defined["term_id"], candidates._keys_for_term(store, defined["term_id"])
    )
    assert word in indexed.split()

    assert defined["term_id"] not in [
        h["term_id"] for h in candidates.find_candidates(store, named["term_id"])
    ]


def test_the_name_route_reads_through_the_punctuation_a_real_gloss_has(store, ids):
    """A gloss writes "(a settling time)," far more often than it writes the
    name with a space on either side. The phrase test is defined on word
    boundaries rather than on whitespace-split tokens for exactly that: the
    ``name_tokens`` unit splits on whitespace alone and would be comparing
    ``'profile),'``."""
    _seed_decoys(store, ids)
    word = _specifics(1)[0]
    defined = _propose(store, ids, "aperture mask", gloss=f"the interval (a {word} profile), measured twice")
    api.accept_sense(store, defined["sense_id"], by_launch=ids["launch"])
    named = _propose(store, ids, f"{word} profile")

    hits = candidates.find_candidates(store, named["term_id"])
    assert [h["gate"] for h in hits if h["term_id"] == defined["term_id"]] == ["name_in_text"]


def test_the_gate_reads_the_programs_own_knobs(store, ids):
    """Both floors are ``[lexicon]`` config like the gloss cap. A program
    that sets the fraction high enough calls everything generic, and only
    the similarity arm is left."""
    _seed_decoys(store, ids)
    word = _specifics(1)[0]
    _propose(store, ids, f"{word} profile")
    second = _propose(store, ids, f"{word} interval")
    config = {"lexicon": {"duplicate_informative_token_fraction": 0.0, "duplicate_informative_token_min_df": 0}}
    assert candidates.find_candidates(store, second["term_id"], config=config) == []


# ---------------------------------------------------------------------------
# withdrawal -- taking back guesses made under the old rule
# ---------------------------------------------------------------------------


@pytest.fixture()
def stale(store, ids):
    """A store holding one candidate the current gate would not open --
    produced the way the real one was, by the store growing.

    Two names ending in the same word open a candidate: at that moment the
    word is carried by two of nine terms, which is rare. A third name
    carrying it makes it a category, and the row already in the queue is now
    a question the scan would not ask. That is the 33,785 in one relation.
    """
    _seed_decoys(store, ids)
    words = specific_words(3)
    first = _propose(store, ids, f"{words[0]} profile")
    second = _propose(store, ids, f"{words[1]} profile")
    rows = _same_as(store)
    assert len(rows) == 1, "the pair was surfaced while the shared word was still rare"
    _propose(store, ids, f"{words[2]} profile")
    assert candidates.find_candidates(store, second["term_id"]) == [], "and would not be now"
    return {"rel_id": rows[0]["rel_id"], "terms": [second["term_id"], first["term_id"]]}


def test_a_rescan_withdraws_a_candidate_the_gate_no_longer_opens(store, ids, stale):
    out = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert out["examined"] == 1
    assert out["withdrawn_count"] == 1
    assert out["kept"] == 0

    row = api.get_relation(store, stale["rel_id"])
    assert row["status"] == "rejected"
    assert row["decided_by_launch"] == ids["launch"]
    assert row["decided_ts"] is not None
    assert row["decided_verb"] is None, "a withdrawal is not a verdict about the two names"
    assert row["confidence"] == out["withdrawn"][0]["confidence"]


def test_the_withdrawal_reason_names_the_gate_and_the_score(store, ids, stale):
    """A row a person opens next year has to explain itself without the
    event log: which rule closed it, and on what number."""
    candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    reason = api.get_relation(store, stale["rel_id"])["reason"]
    assert "duplicate gate" in reason
    assert str(policy.DUPLICATE_SIMILARITY_FLOOR) in reason
    assert "terms" in reason and "similarity" in reason
    # All three routes, because a reader has to know which questions were
    # asked before "no" means anything (build step 1d).
    assert "share no token" in reason
    assert "neither name is written whole" in reason
    assert f"{policy.DUPLICATE_INFORMATIVE_TOKEN_FRACTION:g} of them" in reason


def test_the_withdrawal_appends_one_event_per_relation(store, ids, stale):
    """'withdrawn' is the word the LOG uses; the row's own vocabulary has no
    such status, and the distinction is the point -- a rejection is a
    judgment about two names, a withdrawal is the scan taking back a
    question it should not have asked."""
    import json

    candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    rows = store.ops.execute(
        "SELECT payload, launch_id FROM event WHERE type = 'term_candidate_withdrawn'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["rel_id"] == stale["rel_id"]
    assert payload["status"] == "withdrawn"
    assert payload["verb"] == "same_as"
    assert payload["gate"]["similarity_floor"] == policy.DUPLICATE_SIMILARITY_FLOOR
    assert rows[0]["launch_id"] == ids["launch"]

    run = store.ops.execute("SELECT count(*) FROM event WHERE type = 'term_rescan_run'").fetchone()[0]
    assert run == 1, "and one run-level event, so the pass reconciles as one act"


def test_a_second_rescan_finds_nothing_left_to_do(store, ids, stale):
    """Idempotent because the withdrawn row is no longer pending -- and for
    the same reason a later plain scan stays quiet about that pair."""
    candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    again = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert again["examined"] == 0
    assert again["withdrawn_count"] == 0

    surfaced = candidates.surface_candidates(store, stale["terms"][0])
    assert surfaced["opened"] == []


def test_a_rescan_keeps_a_candidate_the_gate_still_opens(store, ids):
    _seed_decoys(store, ids)
    word = specific_words(1)[0]
    _propose(store, ids, f"{word} profile")
    _propose(store, ids, f"{word} interval")
    rel_id = _same_as(store)[0]["rel_id"]

    out = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert out["kept"] == 1
    assert out["withdrawn_count"] == 0
    assert api.get_relation(store, rel_id)["status"] == "pending"


def test_a_rescan_never_touches_an_exact_key_collision(store, ids):
    """No gate applies to a certainty, so no re-calibration of one can
    withdraw it."""
    _seed_decoys(store, ids)
    first = _propose(store, ids, "settling time")
    _propose(store, ids, "recovery interval", aliases=["settling time"])
    rel = [r for r in _same_as(store) if "lookup key" in (r["reason"] or "")][0]

    out = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert out["kept_certainties"] == 1
    assert out["withdrawn_count"] == 0
    assert api.get_relation(store, rel["rel_id"])["status"] == "pending"
    assert first["term_id"]


def test_a_rescan_never_touches_a_human_opened_candidate(store, ids, stale):
    """A launch-marked row is somebody's judgment that these two might be
    one thing. The machine does not get to take that back."""
    words = specific_words(5)
    left = _propose(store, ids, f"{words[3]} profile")
    right = _propose(store, ids, f"{words[4]} profile")
    theirs = api.open_relation(
        store,
        src_kind="term", src_id=left["term_id"], dst_kind="term", dst_id=right["term_id"],
        verb="same_as", marked_by_kind="launch", marked_by_launch=ids["launch"],
        marked_by_model="a-person", reason="I think these are the same thing",
    )

    out = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert out["withdrawn_count"] == 1, "the system-opened one, and only it"
    assert api.get_relation(store, theirs["rel_id"])["status"] == "pending"


def test_a_rescan_never_touches_a_row_a_launch_has_already_reached_into(store, ids, stale):
    """The other half of "human-touched": a decided timestamp on a still-
    pending row means somebody is mid-decision, and the queue is not the
    machine's to tidy."""
    from trialerror.stores.writer import update

    update(
        store, "term_relation", pk_column="rel_id", pk_value=stale["rel_id"],
        changes={"decided_ts": now(), "decided_by_launch": ids["launch"]},
    )
    out = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert out["examined"] == 0
    assert api.get_relation(store, stale["rel_id"])["status"] == "pending"


def test_a_dry_run_measures_and_writes_nothing(store, ids, stale):
    events_before = store.ops.execute("SELECT count(*) FROM event").fetchone()[0]

    out = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"], dry_run=True)

    assert out["dry_run"] is True
    assert out["withdrawn_count"] == 1
    assert api.get_relation(store, stale["rel_id"])["status"] == "pending"
    assert store.ops.execute("SELECT count(*) FROM event").fetchone()[0] == events_before


def test_a_rescan_refuses_without_a_launch(store, ids, stale):
    """L-E4: a withdrawal is an act somebody ran. Refused before anything is
    read, and refused again if the launch names no row."""
    with pytest.raises(LaunchRequiredError):
        candidates.rescan_duplicate_candidates(store, by_launch="")
    with pytest.raises(XidTargetMissingError):
        candidates.rescan_duplicate_candidates(store, by_launch="LNCH-does-not-exist")
    assert api.get_relation(store, stale["rel_id"])["status"] == "pending"
