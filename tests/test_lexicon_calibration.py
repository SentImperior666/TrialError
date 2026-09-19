"""The duplicate scan's second stage, measured on a name distribution that
reproduces the failure it was written for (build step 1c, decision D3).

The 60-row register fixture never showed this. Its 58 names barely overlap,
so the scan looks calibrated on it no matter what its thresholds are -- and
that is exactly why the first real register import came as a surprise: 6,975
terms, **33,785** pending duplicate candidates, mean fan-out 4.9 against a
top-5 cap. A cap that fires for nearly every term has stopped selecting.

``tests._lexicon_fixtures.name_corpus`` is that shape at a size a test can
run: 216 names, each `<specific> <category>`, eight category words carried
by 27 names each and 72 specifics carried by 3. Every number below was
measured against it, not chosen:

===========================================  =======
first stage alone (``gate=False``)             1,080
...of which share only a category word           648
...of which share their specific                 432
second stage (``gate=True``)                     432
...of which share only a category word             0
distinct pairs the scan then opens                216
===========================================  =======

1,080 is 5 x 216: the cap fires for every single term, the corpus-scale
symptom in miniature. The gate removes the 648 and keeps all 432 -- not
"most of the noise and some of the signal", all of one and none of the
other, on this corpus. The remaining tests say why each half lands where it
does, because a reduction whose mechanism is not asserted is a number that
will silently stop being true.

**Build step 1d: the same corpus with the register's prose on it.** Names
alone were only half the shape. A real register gloss is twenty to a hundred
and sixty words that mention the neighbouring named things in passing, and
the 1c gate asked whether one side's NAME shared an informative word with
the other side's *indexed text* -- names plus glosses. On the live store
that gate withdrew 8,219 of 33,870 candidates and kept 25,651, eight times
the top of the calibration band, and sweeping the fraction knob moved
nothing. ``install_name_corpus(..., glosses=True)`` puts the missing half
in, and the second table is what the two gates do with it:

=============================================  =======
first stage alone (``gate=False``)               1,080
...of which share their specific                   280
the 1c gate keeps                                1,080
the 1d gate keeps                                  281
...by the ``informative_token`` route              280
...by the ``name_in_text`` route                     1
distinct pairs the 1d scan then opens              199
=============================================  =======

The first row of that table is the finding: **with glosses on, the 1c gate
keeps every single hit the first stage produced.** It has not become a bad
filter, it has stopped being a filter -- 1,080 of 1,080 -- because in a
corpus where every gloss names other specifics, every pair can find a word
that is rare among names and present in the other side's paragraph. The 1d
gate keeps 281 of the same 1,080: the 280 pairs that share their specific
IN BOTH NAMES, and the single pair whose gloss quotes another term's whole
name, which is the recall property the rescoping had to preserve. It adds
nothing the 1c gate would have dropped -- the change is a narrowing, and
that is asserted rather than assumed.
"""

from __future__ import annotations

import pytest

from trialerror.lexicon import candidates as candidates_module
from trialerror.lexicon import policy
from trialerror.lexicon.candidates import find_candidates, surface_candidates, token_stats
from trialerror.lexicon.normalize import name_tokens, trigram_similarity

from tests._lexicon_fixtures import (
    CATEGORY_WORDS,
    GLOSS_QUOTES_WHOLE_NAME,
    NAME_CORPUS_SIZE,
    NAMES_PER_SPECIFIC,
    SPECIFIC_COUNT,
    install_name_corpus,
    name_corpus,
    specific_words,
)
from tests._store_fixtures import populate_one_of_everything

#: What the sweep below measures, once, as the module docstring's table.
UNGATED_HITS = 1080
GATED_HITS = 432
CATEGORY_ONLY_HITS = 648

#: And what the same sweep measures over the corpus WITH glosses (build
#: step 1d) -- the second table in the module docstring.
GLOSS_UNGATED_HITS = 1080
GLOSS_LEGACY_GATED_HITS = 1080
GLOSS_GATED_HITS = 281
GLOSS_SAME_SPECIFIC_HITS = 280
GLOSS_NAME_IN_TEXT_HITS = 1
GLOSS_OPENED_PAIRS = 199


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


@pytest.fixture()
def corpus(store, ids):
    return install_name_corpus(store, by_launch=ids["launch"])


def _sweep(store, corpus, *, gate: bool) -> dict[str, object]:
    """Run the scan over every term in the corpus and split the hits by
    whether the pair shares its specific word or only its category word."""
    stats = token_stats(store)
    lemma_of = {term_id: lemma for lemma, term_id in corpus["term_ids"].items()}
    same_specific: list[tuple[str, str, float, str | None]] = []
    category_only: list[tuple[str, str, float, str | None]] = []
    for lemma, term_id in corpus["term_ids"].items():
        for hit in find_candidates(store, term_id, stats=stats, gate=gate):
            other = lemma_of.get(hit["term_id"], "")
            bucket = (
                same_specific
                if corpus["specific_of"][lemma] == corpus["specific_of"].get(other)
                else category_only
            )
            bucket.append((lemma, other, hit["confidence"], hit["gate"]))
    return {
        "total": len(same_specific) + len(category_only),
        "same_specific": same_specific,
        "category_only": category_only,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# the fixture is the distribution it claims to be
# ---------------------------------------------------------------------------


def test_the_corpus_has_the_shape_that_floods(store, ids, corpus):
    """A few words carried by many names, many words carried by few. That
    ratio -- not the corpus size -- is what a top-N-per-term scan turns into
    a five-figure queue."""
    stats = token_stats(store)
    assert corpus["terms"] == NAME_CORPUS_SIZE == 216
    for category in CATEGORY_WORDS:
        assert stats.document_frequency[category] == NAME_CORPUS_SIZE // len(CATEGORY_WORDS) == 27
        assert not stats.is_informative(category)
    for specific in specific_words():
        assert stats.document_frequency[specific] == NAMES_PER_SPECIFIC == 3
        assert stats.is_informative(specific)


def test_the_threshold_is_a_fraction_of_this_store_not_a_constant(store, ids, corpus):
    """2% of the terms actually present, computed at scan time. The one
    number in the gate that has to move as a store grows, and the reason the
    knob is a fraction (``policy.DUPLICATE_INFORMATIVE_TOKEN_FRACTION``)."""
    stats = token_stats(store)
    # 216 fixture terms + the one `populate_one_of_everything` inserts.
    assert stats.term_count == 217
    assert stats.threshold == pytest.approx(217 * policy.DUPLICATE_INFORMATIVE_TOKEN_FRACTION)
    assert stats.threshold == pytest.approx(4.34)


# ---------------------------------------------------------------------------
# what the first stage does on its own
# ---------------------------------------------------------------------------


def test_the_first_stage_alone_floods(store, ids, corpus):
    """The top-5 cap fires for every one of the 216 terms -- 1,080 hits from
    216 names. This is the 33,785 in miniature, and it is what the queue
    looked like before the gate."""
    swept = _sweep(store, corpus, gate=False)
    assert swept["total"] == UNGATED_HITS == 5 * NAME_CORPUS_SIZE


def test_three_fifths_of_the_flood_share_only_a_category_word(store, ids, corpus):
    """648 of the 1,080 pairs have nothing in common but a word 27 other
    names also carry. Asking a human to rule on those is asking them to read
    the same 'no' 648 times."""
    swept = _sweep(store, corpus, gate=False)
    assert len(swept["category_only"]) == CATEGORY_ONLY_HITS == 648
    assert len(swept["same_specific"]) == GATED_HITS


def test_no_category_only_pair_is_actually_alike(store, ids, corpus):
    """Measured, because the similarity arm has to be shown NOT to be what
    rescues these: the most alike category-only pair in the corpus scores
    0.4091, below the 0.5 floor. The specifics are invented to be mutually
    unlike (see ``specific_words``), so a category-only pair shares the
    category word and nothing else."""
    swept = _sweep(store, corpus, gate=False)
    worst = max(confidence for _, _, confidence, _ in swept["category_only"])
    # The claim is that no category-only pair clears the similarity floor, not
    # which exact pair is the most alike: the first stage's top-N is a bm25
    # ranking whose tie order can shift with the process's SQLite state, so the
    # exact value (0.4091 in isolation, 0.4286 once under the full suite on
    # 2026-09-10) is reported, not pinned.
    assert worst < policy.DUPLICATE_SIMILARITY_FLOOR, worst
    assert worst < policy.DUPLICATE_SIMILARITY_FLOOR


# ---------------------------------------------------------------------------
# what the gate does to it
# ---------------------------------------------------------------------------


def test_the_gate_keeps_every_specifics_sharing_pair_and_nothing_else(store, ids, corpus):
    """The headline: 1,080 -> 432, and the 432 are exactly the pairs the
    fixture built to be found. No category-only pair survives, and no real
    pair is lost -- on this corpus the gate is not a trade-off between
    precision and recall, it is the removal of a class of hit that carried
    no information in the first place."""
    swept = _sweep(store, corpus, gate=True)
    assert swept["total"] == GATED_HITS == 432
    assert swept["category_only"] == []
    assert len(swept["same_specific"]) == SPECIFIC_COUNT * NAMES_PER_SPECIFIC * 2


def test_the_kept_pairs_are_kept_by_the_token_arm_not_by_looking_alike(store, ids, corpus):
    """Every survivor passes on the shared rare word -- and 414 of the 432
    would have FAILED the similarity floor on their own, because
    ``<specific> record`` and ``<specific> procedure`` are only 0.29 alike.
    Similarity alone would have lost 96% of the real pairs on this corpus:
    the two arms do different jobs, and this is the one the flood needed."""
    swept = _sweep(store, corpus, gate=True)
    assert {gate for _, _, _, gate in swept["same_specific"]} == {"informative_token"}
    below_floor = [
        hit for hit in swept["same_specific"] if hit[2] < policy.DUPLICATE_SIMILARITY_FLOOR
    ]
    assert len(below_floor) == 414
    assert min(c for _, _, c, _ in swept["same_specific"]) == pytest.approx(0.2857)


def test_the_similarity_arm_catches_what_the_token_arm_cannot(store, ids, corpus):
    """The other half of the gate, on the case it exists for: two names that
    share no whole word BECAUSE they are nearly the same string. A gate with
    only the token arm would drop a misspelling, which is the single most
    common real duplicate there is."""
    specific = specific_words()[0]
    assert trigram_similarity(f"{specific} table", f"{specific}s table") >= policy.DUPLICATE_SIMILARITY_FLOOR


# ---------------------------------------------------------------------------
# and to the queue a person actually sees
# ---------------------------------------------------------------------------


def test_the_scan_opens_one_row_per_pair_not_per_direction(store, ids, corpus):
    """432 directed hits are 216 distinct pairs (three names per specific,
    three pairs each, 72 specifics). What a reviewer faces is the pair
    count, and on this corpus it is 216 -- a morning's work rather than a
    queue nobody starts."""
    stats = token_stats(store)
    for term_id in corpus["term_ids"].values():
        surface_candidates(store, term_id, stats=stats)
    opened = store.knowledge.execute(
        "SELECT count(*) FROM term_relation WHERE verb = 'same_as' AND status = 'pending' "
        "AND marked_by_kind = 'system'"
    ).fetchone()[0]
    assert opened == SPECIFIC_COUNT * 3 == 216


def test_the_calibration_target_is_stated_as_numbers(store, ids):
    """D3 asks for the target to live in ``policy.py`` rather than in a
    report nobody re-reads. Asserted here so it cannot be quietly deleted:
    the band, and the measured distribution it is set against."""
    low, high = policy.DUPLICATE_CALIBRATION_TARGET
    assert 0 < low < high
    reference = policy.DUPLICATE_CALIBRATION_REFERENCE
    assert reference["terms"] == 6975
    assert reference["pairs_opened_first_stage_only"] == 33785
    assert reference["glosses_over_80_words"] == 271
    # The band is a target for the REFERENCE corpus, not for this fixture --
    # stated here so a later reader does not mistake the 216 above for a
    # measurement of it. What it does assert is the direction: the target is
    # an order of magnitude under what the first stage produced alone.
    assert high < reference["pairs_opened_first_stage_only"] / 10


# ---------------------------------------------------------------------------
# build step 1d: the same corpus with the register's prose on it
# ---------------------------------------------------------------------------


@pytest.fixture()
def glossed(store, ids):
    """The 216-name corpus with one imported gloss per term -- register
    prose that mentions two OTHER names' specifics in passing, the way a
    register defines a thing by naming what it is measured against."""
    return install_name_corpus(store, by_launch=ids["launch"], glosses=True)


def _legacy_verdict(store, left_id, right_id, stats, *, confidence: float) -> bool:
    """What the **1c** gate would have said about this pair.

    A second implementation of a rule, kept deliberately and only here: it
    is the rule build step 1d replaced, and a claim that the replacement
    removes something is worth nothing unless the thing it removes is still
    executable. One side's NAME tokens against the other side's INDEXED
    TEXT -- names plus current glosses -- with informative measured over
    names, and the similarity floor as the second arm.
    """
    left_keys = candidates_module._keys_for_term(store, left_id)
    right_keys = candidates_module._keys_for_term(store, right_id)

    def _names(keys):
        out: set[str] = set()
        for key in keys:
            out.update(name_tokens(key))
        return out

    def _indexed(term_id, keys):
        out = _names(keys)
        for (gloss,) in store.conn_for_table("term_sense").execute(
            "SELECT gloss FROM term_sense WHERE term_id = ? AND status = 'current'", (term_id,)
        ).fetchall():
            out.update(name_tokens(gloss))
        return out

    left_names, right_names = _names(left_keys), _names(right_keys)
    left_indexed = _indexed(left_id, left_keys)
    right_indexed = _indexed(right_id, right_keys)
    shared = {t for t in left_names if stats.is_informative(t) and t in right_indexed} | {
        t for t in right_names if stats.is_informative(t) and t in left_indexed
    }
    return bool(shared) or confidence >= policy.DUPLICATE_SIMILARITY_FLOOR


def _sweep_glossed(store, corpus) -> dict[str, object]:
    """Every first-stage hit over the glossed corpus, each carrying both
    gates' verdicts and whether the pair is one the fixture built."""
    stats = token_stats(store)
    lemma_of = {term_id: lemma for lemma, term_id in corpus["term_ids"].items()}
    hits: list[dict[str, object]] = []
    for lemma, term_id in corpus["term_ids"].items():
        for hit in find_candidates(store, term_id, stats=stats, gate=False):
            other = lemma_of.get(hit["term_id"], "")
            hits.append(
                {
                    "lemma": lemma,
                    "other": other,
                    "same_specific": corpus["specific_of"][lemma]
                    == corpus["specific_of"].get(other),
                    "gate": hit["gate"],
                    "kept_1d": hit["gate"] is not None,
                    "kept_1c": _legacy_verdict(
                        store, term_id, hit["term_id"], stats, confidence=hit["confidence"]
                    ),
                    "name_in_text": hit["name_in_text"],
                }
            )
    return {"hits": hits, "stats": stats}


def test_the_gloss_corpus_stops_the_1c_gate_filtering_at_all(store, ids, glossed):
    """The finding build step 1d was opened by, reproduced at test size.

    Not "the gate got worse": with register prose on the terms it keeps
    1,080 of the 1,080 hits the first stage produced -- all of them. Every
    pair can find a word that is rare among NAMES sitting somewhere in the
    other side's paragraph, so the question the gate asks has stopped having
    a "no" in it. On the live store the same rule kept 25,651 of 33,870.
    """
    swept = _sweep_glossed(store, glossed)
    hits = swept["hits"]
    assert len(hits) == GLOSS_UNGATED_HITS == 5 * NAME_CORPUS_SIZE
    assert sum(1 for h in hits if h["kept_1c"]) == GLOSS_LEGACY_GATED_HITS == len(hits)


def test_the_1d_gate_withdraws_the_leak_and_keeps_the_real_pairs(store, ids, glossed):
    """1,080 -> 281. The 280 pairs that share their specific IN BOTH NAMES
    survive; the 799 that shared it only through one side's prose do not."""
    swept = _sweep_glossed(store, glossed)
    hits = swept["hits"]
    kept = [h for h in hits if h["kept_1d"]]
    assert len(kept) == GLOSS_GATED_HITS == 281
    assert sum(1 for h in kept if h["same_specific"]) == GLOSS_SAME_SPECIFIC_HITS == 280
    assert sum(1 for h in hits if h["same_specific"]) == GLOSS_SAME_SPECIFIC_HITS, (
        "and every same-specific pair the first stage found is kept -- the change costs "
        "no recall on the pairs this fixture built to be found"
    )
    withdrawn = [h for h in hits if h["kept_1c"] and not h["kept_1d"]]
    assert len(withdrawn) == GLOSS_LEGACY_GATED_HITS - GLOSS_GATED_HITS == 799
    assert not any(h["same_specific"] for h in withdrawn)


def test_the_1d_gate_never_surfaces_what_the_1c_gate_would_not(store, ids, glossed):
    """The change is a narrowing, and in the strong sense: not one of the
    1,080 hits is surfaced by the new gate and refused by the old. Worth
    asserting rather than assuming -- ``name_in_text`` is a new route, and a
    new route is exactly how a narrowing quietly becomes a trade."""
    swept = _sweep_glossed(store, glossed)
    assert [h for h in swept["hits"] if h["kept_1d"] and not h["kept_1c"]] == []


def test_a_gloss_quoting_a_whole_name_is_still_reachable_from_that_name(store, ids, glossed):
    """The recall property the rescoping had to keep, and the only pair in
    the corpus that has it. Its two names share nothing and look nothing
    alike (similarity 0.0); it survives because one term's gloss writes the
    other's name out in full, which is the hit the design argued the index
    should span glosses for."""
    names = [lemma for lemma, _ in name_corpus()]
    quoting, quoted = names[GLOSS_QUOTES_WHOLE_NAME[0]], names[GLOSS_QUOTES_WHOLE_NAME[1]]

    swept = _sweep_glossed(store, glossed)
    by_route = [h for h in swept["hits"] if h["gate"] == "name_in_text"]
    assert len(by_route) == GLOSS_NAME_IN_TEXT_HITS == 1
    assert {by_route[0]["lemma"], by_route[0]["other"]} == {quoting, quoted}
    assert by_route[0]["name_in_text"] == quoted, "the whole name, as the gloss wrote it"


def test_the_queue_the_1d_gate_leaves_on_the_glossed_corpus(store, ids, glossed):
    """What a reviewer actually faces: 199 distinct pairs, out of a first
    stage that produced 1,080 directed hits and a 1c gate that would have
    opened every one of them."""
    stats = token_stats(store)
    for term_id in glossed["term_ids"].values():
        surface_candidates(store, term_id, stats=stats)
    opened = store.knowledge.execute(
        "SELECT count(*) FROM term_relation WHERE verb = 'same_as' AND status = 'pending' "
        "AND marked_by_kind = 'system'"
    ).fetchone()[0]
    assert opened == GLOSS_OPENED_PAIRS == 199


def test_the_live_rescan_numbers_are_recorded_as_data(store, ids):
    """Build step 1d's own measurement, in ``policy.py`` beside the 1c one
    rather than in a report -- what the gate that shipped actually did to the
    queue it was written for, so the next re-calibration has something to be
    compared with."""
    reference = policy.DUPLICATE_CALIBRATION_REFERENCE
    assert reference["rescan_terms"] == 6992
    assert reference["rescan_pairs_examined"] == 33870
    assert reference["rescan_withdrawn"] == 8219
    assert reference["rescan_kept"] == 25651
    assert (
        reference["rescan_withdrawn"] + reference["rescan_kept"]
        == reference["rescan_pairs_examined"]
    )
    low, high = policy.DUPLICATE_CALIBRATION_TARGET
    assert reference["rescan_kept"] > high, "which is why there is a build step 1d"
