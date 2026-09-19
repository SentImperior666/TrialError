"""Rule 1e -- the duplicate gate's coverage bar, and the informative token
the ``name_in_text`` route now asks the contained name for.

The rule is a **tightening applied after the frequency gate**, and both
halves of that sentence are what these tests measure:

* *after*, not instead of: the frequency test still decides which shared
  words count at all, and coverage is a second question asked only of the
  words that passed it. A pair that shares no rare word is refused by the
  same test that refused it before;
* *a tightening*: every pair the gate surfaces under rule 1e was surfaced
  without it, and setting the two keys to ``0``/``false`` reaches the old
  behaviour EXACTLY -- asserted here by running both configurations over the
  same store and comparing the two results as data, which is what makes the
  rule auditable rather than merely claimed.

**On the flood fixture the rule costs nothing**, and that is the first table
below: its names are ``<specific> <category>``, one informative token each,
so a shared specific covers all of the shorter name and clears any bar under
1.0. The fixture therefore cannot tell 0.5 from 0.1 -- what it can tell, and
does, is that the pairs it was built to surface are all still surfaced. The
cases the bar actually bites are built here, small and on purpose.
"""

from __future__ import annotations

import json

import pytest

from trialerror.lexicon import api, candidates, policy
from trialerror.lexicon.candidates import find_candidates, surface_candidates, token_stats

from tests._lexicon_fixtures import install_name_corpus
from tests._store_fixtures import populate_one_of_everything

#: What the two keys have to be set to for the pre-1e gate: nothing can
#: cover less than 0, and the containment route stops asking for a rare word.
RULE_1E_OFF: dict[str, object] = {
    "lexicon": {
        "duplicate_coverage_min": 0.0,
        "name_in_text_requires_informative": False,
    }
}

#: The measurement the flood fixture supports: rule 1e withdraws none of its
#: pairs, with and without the register prose (the 1c and 1d tables in
#: ``tests/test_lexicon_calibration.py``).
FLOOD_GATED_HITS = 432
GLOSS_GATED_HITS = 281


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
#: SCORE rather than on a match -- ``tests/test_lexicon_candidates.py``
#: states the reason: bm25 weighs a hit by how rare its tokens are, so in a
#: two-row index every hit scores about the floor and the first stage finds
#: nothing to gate.
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


def _sweep(store, term_ids, *, config=None) -> str:
    """Every gated hit over ``term_ids``, as one JSON string.

    A string rather than a structure because the claim being tested is
    byte-identity: two configurations produce the same candidates, with the
    same confidences, the same shared tokens and the same routes, or they do
    not."""
    stats = token_stats(store, config=config)
    out = []
    for term_id in sorted(term_ids):
        for hit in find_candidates(store, term_id, stats=stats, config=config):
            out.append({"from": term_id, **hit})
    return json.dumps(out, sort_keys=True)


def _pairs(store, term_ids, *, config=None) -> set[frozenset[str]]:
    """The gated pairs among ``term_ids`` -- hits on the seeded decoys are
    not what any test here is about."""
    wanted = set(term_ids)
    stats = token_stats(store, config=config)
    found: set[frozenset[str]] = set()
    for term_id in term_ids:
        for hit in find_candidates(store, term_id, stats=stats, config=config):
            if hit["term_id"] in wanted:
                found.add(frozenset({term_id, hit["term_id"]}))
    return found


def _gate_of(store, left_id, right_id, *, config=None) -> str | None:
    stats = token_stats(store, config=config)
    for hit in find_candidates(store, left_id, stats=stats, config=config, gate=False):
        if hit["term_id"] == right_id:
            return hit["gate"]
    return None


# ---------------------------------------------------------------------------
# the flood fixture: the before/after table
# ---------------------------------------------------------------------------


def test_the_flood_corpus_is_byte_identical_before_and_after_rule_1e(store, ids):
    """216 names, 432 gated hits, and rule 1e withdraws none of them.

    The corpus's names carry one informative token each, so every pair the
    gate keeps covers 1.0 of the shorter name. That is the honest reading of
    this fixture: it establishes the rule's RECALL cost here is zero, and it
    establishes nothing about where the bar should sit."""
    corpus = install_name_corpus(store, by_launch=ids["launch"])
    term_ids = list(corpus["term_ids"].values())

    after = _sweep(store, term_ids)
    before = _sweep(store, term_ids, config=RULE_1E_OFF)

    assert after == before
    assert len(json.loads(after)) == FLOOD_GATED_HITS == 432


def test_the_glossed_flood_corpus_is_byte_identical_too(store, ids):
    """The same corpus with the register's prose on it -- the shape that
    made build step 1d necessary. 281 hits, 280 by the token route and one by
    containment, and rule 1e leaves all 281 standing: the contained name
    there is a real name carrying a real specific, which is exactly the hit
    the new bar is written to keep."""
    corpus = install_name_corpus(store, by_launch=ids["launch"], glosses=True)
    term_ids = list(corpus["term_ids"].values())

    after = _sweep(store, term_ids)
    before = _sweep(store, term_ids, config=RULE_1E_OFF)

    assert after == before
    hits = json.loads(after)
    assert len(hits) == GLOSS_GATED_HITS == 281
    assert sum(1 for h in hits if h["gate"] == "name_in_text") == 1


# ---------------------------------------------------------------------------
# where the coverage bar bites
# ---------------------------------------------------------------------------


@pytest.fixture()
def four_word_pair(store, ids):
    """Two four-word names sharing exactly one rare word.

    Both names are built from invented syllables so the only thing they have
    in common is the word put there on purpose -- the pair is found by the
    trigram half for that word, shares it, and under the pre-1e gate is
    opened for it. One word of four is a coincidence with a rare word in it,
    which is the whole case rule 1e exists for."""
    _seed_decoys(store, ids)
    left = _propose(store, ids, "vontal drisker pemway kolust")
    right = _propose(store, ids, "vontal manrike tebsoy welnor")
    return left["term_id"], right["term_id"]


def test_one_shared_word_in_four_no_longer_opens_a_pair(store, ids, four_word_pair):
    left, right = four_word_pair
    assert _gate_of(store, right, left) is None
    assert _pairs(store, [left, right]) == set()


def test_the_same_pair_is_opened_with_the_coverage_key_at_zero(store, ids, four_word_pair):
    """The old behaviour, reachable by config. Without this the rule would
    be a change nobody could audit against what it replaced."""
    left, right = four_word_pair
    assert _gate_of(store, right, left, config=RULE_1E_OFF) == "informative_token"
    assert _pairs(store, [left, right], config=RULE_1E_OFF) == {frozenset({left, right})}


def test_the_coverage_measured_is_reported_whether_or_not_the_pair_passes(
    store, ids, four_word_pair
):
    """A knob is swept by reading the number that was compared, not the
    verdict. ``find_candidates(gate=False)`` is that reading, and it carries
    the coverage beside the shared tokens the frequency gate found."""
    left, right = four_word_pair
    stats = token_stats(store)
    hit = next(
        h
        for h in find_candidates(store, right, stats=stats, gate=False)
        if h["term_id"] == left
    )
    assert hit["shared_tokens"] == ["vontal"]
    assert hit["coverage"] == pytest.approx(0.25)
    assert hit["gate"] is None


def test_exactly_half_coverage_passes(store, ids):
    """The boundary, stated as ``>=`` in the gate and asserted here: two
    two-word names sharing one rare word cover exactly half of the shorter
    name, and half is enough. A rule whose boundary is not pinned is a rule
    that moves by one epsilon in a later refactor."""
    _seed_decoys(store, ids)
    left = _propose(store, ids, "hakrem drisker")
    right = _propose(store, ids, "hakrem tebsoy")
    stats = token_stats(store)
    hit = next(
        h
        for h in find_candidates(store, right["term_id"], stats=stats, gate=False)
        if h["term_id"] == left["term_id"]
    )
    assert hit["coverage"] == pytest.approx(0.5) == policy.DUPLICATE_COVERAGE_MIN
    assert hit["gate"] == "informative_token"


def test_one_shared_word_in_three_does_not_pass(store, ids):
    """One below the boundary, from the same shape -- so the boundary test
    above is measuring the bar and not the fixture."""
    _seed_decoys(store, ids)
    left = _propose(store, ids, "solvek drisker pemway")
    right = _propose(store, ids, "solvek tebsoy welnor")
    stats = token_stats(store)
    hit = next(
        h
        for h in find_candidates(store, right["term_id"], stats=stats, gate=False)
        if h["term_id"] == left["term_id"]
    )
    assert hit["coverage"] == pytest.approx(1 / 3)
    assert hit["gate"] is None


def test_coverage_is_taken_of_the_shorter_name(store, ids):
    """A two-word name against a four-word one sharing one word: 1/2, not
    1/4. The shorter side is the denominator because it is the only name a
    single shared word can plausibly be most of -- measuring against the
    longer one would refuse a short name whose whole content is the word."""
    _seed_decoys(store, ids)
    short = _propose(store, ids, "kelvor drisker")
    long_name = _propose(store, ids, "kelvor manrike tebsoy welnor")
    stats = token_stats(store)
    hit = next(
        h
        for h in find_candidates(store, long_name["term_id"], stats=stats, gate=False)
        if h["term_id"] == short["term_id"]
    )
    assert hit["coverage"] == pytest.approx(0.5)
    assert hit["gate"] == "informative_token"


def test_a_pair_sharing_no_rare_word_is_still_refused_by_the_frequency_gate(store, ids):
    """Rule 1e is applied AFTER the frequency gate, never instead of it: a
    word three names carry is not informative, so there is nothing for
    coverage to be a coverage OF, and the pair is refused exactly where it
    was refused before."""
    _seed_decoys(store, ids)
    first = _propose(store, ids, "drisker reading")
    second = _propose(store, ids, "pemway reading")
    _propose(store, ids, "welnor reading")
    stats = token_stats(store)
    assert not stats.is_informative("reading")
    hit = next(
        h
        for h in find_candidates(store, second["term_id"], stats=stats, gate=False)
        if h["term_id"] == first["term_id"]
    )
    assert hit["shared_tokens"] == []
    assert hit["coverage"] == 0.0
    assert hit["gate"] is None


# ---------------------------------------------------------------------------
# the containment route's new bar
# ---------------------------------------------------------------------------


@pytest.fixture()
def stopword_only_name(store, ids):
    """A term whose whole name is a function word plus a word the store
    carries everywhere, and a term whose gloss contains that name verbatim.

    Under the pre-1e gate the second reaches the first by containment, which
    is a fact about English prose: "the reading" is written inside half the
    glosses in a store that keeps readings."""
    _seed_decoys(store, ids)
    contained = _propose(store, ids, "the reading")
    _propose(store, ids, "drisker reading")
    _propose(store, ids, "pemway reading")
    quoting = _propose(
        store,
        ids,
        "kolust carrier",
        gloss="the reading this program keeps while the carrier is warm",
        # `current`, because ``term_fts`` and the gate's indexed-text read
        # both span CURRENT glosses only -- a proposed sense's prose is not
        # in the index and no containment route could reach it.
        status="current",
    )
    return contained["term_id"], quoting["term_id"]


def test_a_stopword_only_name_is_refused_by_containment(store, ids, stopword_only_name):
    contained, quoting = stopword_only_name
    stats = token_stats(store)
    assert not stats.is_informative("reading")
    assert not stats.is_informative("the")
    # Asked from the CONTAINED side: the trigram query is built from a
    # term's own names, and it is "the reading" that is written inside the
    # other term's gloss, so that is the direction the first stage finds the
    # pair in at all.
    assert _gate_of(store, contained, quoting) is None


def test_the_same_containment_fires_with_the_key_off(store, ids, stopword_only_name):
    """Again reachable by config, and the route it fires on is named -- so
    the test is measuring the bar rather than a pair that stopped being
    found for some other reason."""
    contained, quoting = stopword_only_name
    stats = token_stats(store, config=RULE_1E_OFF)
    hit = next(
        h
        for h in find_candidates(store, contained, stats=stats, config=RULE_1E_OFF, gate=False)
        if h["term_id"] == quoting
    )
    assert hit["gate"] == "name_in_text"
    assert hit["name_in_text"] == "the reading"


def test_a_name_carrying_one_informative_token_still_qualifies(store, ids):
    """The recall property the bar had to keep: a real name quoted whole in
    another term's gloss is still the hit the design argued for. Only a name
    with NO rare word of its own stops qualifying."""
    _seed_decoys(store, ids)
    contained = _propose(store, ids, "drisker reading")
    _propose(store, ids, "pemway reading")
    _propose(store, ids, "welnor reading")
    quoting = _propose(
        store,
        ids,
        "kolust carrier",
        gloss="the drisker reading this program keeps while the carrier is warm",
        status="current",
    )
    assert _gate_of(store, contained["term_id"], quoting["term_id"]) == "name_in_text"


# ---------------------------------------------------------------------------
# the rescan carries the rule to a queue opened before it
# ---------------------------------------------------------------------------


def _open_under_old_rule(store, ids, term_ids) -> list[dict]:
    """Open the queue the pre-1e gate would have opened, so the rescan has
    something from before the rule to re-ask."""
    stats = token_stats(store, config=RULE_1E_OFF)
    for term_id in term_ids:
        surface_candidates(store, term_id, stats=stats, config=RULE_1E_OFF)
    return [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT * FROM term_relation WHERE verb = 'same_as' AND status = 'pending'"
        ).fetchall()
    ]


def test_the_rescan_withdraws_what_the_coverage_bar_no_longer_opens(
    store, ids, four_word_pair
):
    left, right = four_word_pair
    opened = _open_under_old_rule(store, ids, [left, right])
    assert len(opened) == 1

    summary = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert summary["examined"] == 1
    assert summary["withdrawn_count"] == 1
    assert summary["withdrawn_by_coverage"] == 1
    assert summary["kept"] == 0
    assert summary["coverage_min"] == policy.DUPLICATE_COVERAGE_MIN
    assert summary["name_in_text_requires_informative"] is True

    row = dict(
        store.knowledge.execute(
            "SELECT * FROM term_relation WHERE rel_id = ?", (opened[0]["rel_id"],)
        ).fetchone()
    )
    assert row["status"] == "rejected"
    assert "coverage rule" in row["reason"]
    assert "'vontal'" in row["reason"], "the shared rare word is named, not denied"
    assert "0.25" in row["reason"]


def test_the_rescan_keeps_the_pair_when_the_keys_are_off(store, ids, four_word_pair):
    """The same queue under the same rescan verb with rule 1e configured
    off: nothing is withdrawn, which is the second half of "reachable by
    config" -- the withdrawal is the RULE's, not the verb's."""
    left, right = four_word_pair
    _open_under_old_rule(store, ids, [left, right])

    summary = candidates.rescan_duplicate_candidates(
        store, by_launch=ids["launch"], config=RULE_1E_OFF
    )
    assert summary["withdrawn_count"] == 0
    assert summary["withdrawn_by_coverage"] == 0
    assert summary["kept"] == 1
    assert summary["coverage_min"] == 0.0
    assert summary["name_in_text_requires_informative"] is False


def test_the_rescan_never_takes_back_a_human_touched_candidate(
    store, ids, four_word_pair
):
    """The one boundary rule 1e does not move. A pair a person has ruled on
    is a decision, and the machine may take back its own guess and nothing
    else -- so a coverage failure somebody already accepted stays accepted."""
    left, right = four_word_pair
    opened = _open_under_old_rule(store, ids, [left, right])
    api.decide_relation(
        store,
        rel_id=opened[0]["rel_id"],
        decision="same_as",
        by_launch=ids["launch"],
    )

    summary = candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])
    assert summary["examined"] == 0
    assert summary["withdrawn_count"] == 0

    row = dict(
        store.knowledge.execute(
            "SELECT * FROM term_relation WHERE rel_id = ?", (opened[0]["rel_id"],)
        ).fetchone()
    )
    assert row["status"] == "confirmed"
    assert row["decided_by_launch"] == ids["launch"]


def test_the_withdrawal_event_records_the_knobs_that_were_in_force(
    store, ids, four_word_pair
):
    left, right = four_word_pair
    _open_under_old_rule(store, ids, [left, right])
    candidates.rescan_duplicate_candidates(store, by_launch=ids["launch"])

    rows = [
        json.loads(r["payload"])
        for r in store.ops.execute(
            "SELECT payload FROM event WHERE type = 'term_candidate_withdrawn'"
        ).fetchall()
    ]
    assert len(rows) == 1
    gate = rows[0]["gate"]
    assert gate["coverage_min"] == policy.DUPLICATE_COVERAGE_MIN
    assert gate["name_in_text_requires_informative"] is True
    assert rows[0]["coverage"] == pytest.approx(0.25)
    assert rows[0]["shared_tokens"] == ["vontal"]


# ---------------------------------------------------------------------------
# the two knobs, read the way every other one is
# ---------------------------------------------------------------------------


def test_the_defaults_are_what_an_unconfigured_program_gets():
    resolved = policy.load_policy(None)
    assert resolved["duplicate_coverage_min"] == policy.DUPLICATE_COVERAGE_MIN == 0.5
    assert resolved["name_in_text_requires_informative"] is True
    assert policy.NAME_IN_TEXT_REQUIRES_INFORMATIVE is True


def test_the_keys_are_read_from_the_lexicon_table():
    resolved = policy.load_policy(RULE_1E_OFF)
    assert resolved["duplicate_coverage_min"] == 0.0
    assert resolved["name_in_text_requires_informative"] is False


@pytest.mark.parametrize("value", [-0.1, 1.5, 50, "half", None, True])
def test_a_coverage_value_that_is_not_a_fraction_falls_back_to_the_default(value):
    """Nothing can cover more than all of a name, so ``50`` is a percentage
    written where a fraction goes. Read as unsatisfiable it would turn the
    token route off across a whole store with nothing saying so."""
    resolved = policy.load_policy({"lexicon": {"duplicate_coverage_min": value}})
    assert resolved["duplicate_coverage_min"] == policy.DUPLICATE_COVERAGE_MIN


@pytest.mark.parametrize("value", ["yes", 1, 0, None])
def test_a_containment_key_that_is_not_a_boolean_falls_back_to_the_default(value):
    resolved = policy.load_policy({"lexicon": {"name_in_text_requires_informative": value}})
    assert resolved["name_in_text_requires_informative"] is True


@pytest.mark.parametrize(
    "shared,left,right,expected",
    [
        (1, 2, 2, 0.5),
        (1, 2, 4, 0.5),
        (1, 4, 4, 0.25),
        (2, 2, 3, 1.0),
        (0, 3, 3, 0.0),
        (1, 0, 3, 0.0),
    ],
)
def test_coverage_of_shorter_name(shared, left, right, expected):
    assert policy.coverage_of_shorter_name(shared, left, right) == pytest.approx(expected)
