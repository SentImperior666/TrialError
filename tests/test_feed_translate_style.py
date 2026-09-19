"""lane-b-translator: the plain-register style contract as code
(:mod:`trialerror.feed_translate.style`) -- Section 4.5 of
``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md``, rule by rule.

Two things these tests exist to pin, beyond "the regexes work":

1. The **severity split** is the gate's whole contract. A dropped booking
   id is ``fidelity`` (the translation is FALSE); a 30-word sentence is
   ``register`` (the translation is WORSE). Collapsing the two would
   either withhold good translations or serve wrong ones.
2. The rules must not fire on an HONEST translation. Several tests below
   are deliberately negative: a plain rewrite that keeps every id and
   softens "may" to "might" must come back clean, or the gate would
   withhold exactly the output the feature exists to produce.
"""

from __future__ import annotations

import pytest

from trialerror.feed_translate.style import (
    HEDGE_FAMILIES,
    MAX_SENTENCE_WORDS,
    check_style,
    extract_fidelity_tokens,
    hedge_families_present,
    split_sentences,
)


def _rules(report) -> list[str]:
    return [v.rule for v in report.violations]


# ---------------------------------------------------------------------------
# token extraction (rule 8's inventory)
# ---------------------------------------------------------------------------


def test_extract_keeps_a_date_whole_instead_of_splitting_it_into_numbers():
    tokens = extract_fidelity_tokens("closed on 2026-09-05 after 3 attempts")
    assert tokens["dates"] == ["2026-09-05"]
    assert tokens["numbers"] == ["3"]


def test_extract_keeps_a_typed_id_whole_and_out_of_the_number_bucket():
    tokens = extract_fidelity_tokens("LNCH-01JXYZ4 was booked under C-0073")
    assert tokens["ids"] == ["C-0073", "LNCH-01JXYZ4"]
    assert tokens["numbers"] == []


def test_extract_strips_list_markers_only_when_asked():
    text = "1. start the worker\n2. read the ledger"
    assert extract_fidelity_tokens(text)["numbers"] == ["1", "2"]
    assert extract_fidelity_tokens(text, strip_list_markers=True)["numbers"] == []


def test_split_sentences_matches_the_codebase_sentence_convention():
    assert split_sentences("One thing. Two things! Three?") == ["One thing.", "Two things!", "Three?"]


# ---------------------------------------------------------------------------
# rule 8 -- ids, numbers, dates (fidelity)
# ---------------------------------------------------------------------------


def test_dropping_an_id_is_a_fidelity_violation():
    report = check_style("Booking LNCH-01JXYZ4 was refused.", "The booking was refused.")
    assert "r8_ids_dropped" in _rules(report)
    assert report.fidelity_violations
    assert not report.register_violations


def test_inventing_a_number_is_a_fidelity_violation():
    report = check_style("Some gates moved.", "7 gates moved.")
    assert "r8_numbers_invented" in _rules(report)


def test_dropping_a_date_is_a_fidelity_violation():
    report = check_style("Frozen on 2026-09-05.", "It was frozen recently.")
    assert "r8_dates_dropped" in _rules(report)


def test_a_repeated_count_is_a_fidelity_violation_even_though_the_set_of_numbers_is_unchanged():
    """FT-3, fix pass: a SET comparison sees {'1'} on both sides and calls
    this clean; a MULTISET comparison sees the count triple and does not."""
    report = check_style("1 gate moved.", "1 gate moved. 1 gate moved. 1 gate moved.")
    assert "r8_numbers_invented" in _rules(report)


def test_a_dropped_repetition_is_also_a_fidelity_violation():
    report = check_style("1 gate moved. 1 gate moved. 1 gate moved.", "1 gate moved.")
    assert "r8_numbers_dropped" in _rules(report)


def test_a_numbered_list_the_translation_introduces_is_not_an_invented_number():
    original = "The worker claims a job, renews the lease, and writes a checkpoint."
    translation = "1. The worker claims a job.\n2. The worker renews the lease.\n3. The worker writes a checkpoint."
    report = check_style(original, translation)
    assert not report.fidelity_violations, report.as_dict()


# ---------------------------------------------------------------------------
# rule 9 -- hedges (fidelity)
# ---------------------------------------------------------------------------


def test_promoting_a_hedge_to_a_fact_is_a_fidelity_violation():
    report = check_style("The booking DEFERRED, not FAILED.", "The booking failed.")
    assert "r9_hedge" in _rules(report)
    assert report.hedges_lost == ["deferred"]


def test_a_plainer_synonym_satisfies_the_same_hedge_family():
    report = check_style("The run may have failed.", "The run might have failed.")
    assert report.hedges_lost == []
    assert not report.fidelity_violations


def test_hedge_matching_is_whole_word_not_prefix():
    # "maybe" must not satisfy the "may" member by prefix, but it is not in
    # any family either -- so an original hedged with "may" and a
    # translation carrying only "maybe" loses the family.
    assert "possibility" not in hedge_families_present("maybe so")
    assert "possibility" in hedge_families_present("it may be so")


def test_every_hedge_family_is_detected_by_at_least_its_own_first_member():
    for family, members in HEDGE_FAMILIES.items():
        assert family in hedge_families_present(f"the state is {members[0]} here"), family


# ---------------------------------------------------------------------------
# register rules -- flagged, never fidelity
# ---------------------------------------------------------------------------


def test_a_long_sentence_is_register_not_fidelity():
    long_sentence = "The " + "very " * MAX_SENTENCE_WORDS["flavored"] + "long sentence keeps going."
    report = check_style("Short.", long_sentence)
    assert "r1_sentence_length" in _rules(report)
    assert not report.fidelity_violations


def test_strict_mode_uses_the_tighter_sentence_cap():
    words = " ".join(["word"] * 22) + "."
    assert not [v for v in check_style("x.", words, style_mode="flavored").violations if v.rule == "r1_sentence_length"]
    assert [v for v in check_style("x.", words, style_mode="strict").violations if v.rule == "r1_sentence_length"]


@pytest.mark.parametrize(
    "translation, rule",
    [
        ("Two things happened; both mattered.", "r4_semicolon"),
        ("We will spin up a second worker.", "r5_phrasal_verb"),
        ("This is a pivotal change to the ledger.", "r12_marketing"),
        ("The job completed. Hope this helps.", "r13_filler"),
        ("The job completed — cleanly.", "r14_dashes"),
    ],
)
def test_register_rules_fire_and_stay_register_severity(translation, rule):
    report = check_style("The job completed.", translation)
    assert rule in _rules(report)
    assert all(v.severity == "register" for v in report.violations if v.rule == rule)
    assert not report.fidelity_violations


def test_an_em_dash_the_original_also_uses_is_not_a_violation():
    report = check_style("Done — cleanly.", "Finished — cleanly.")
    assert "r14_dashes" not in _rules(report)


# ---------------------------------------------------------------------------
# the honest-translation negative case
# ---------------------------------------------------------------------------


def test_a_faithful_plain_rewrite_passes_the_whole_checklist():
    original = (
        "LNCH-01JXYZ4 was launch-booked against pool P-2 at 2026-09-05; the reconcile is pending, "
        "so the 3 downstream gates stay DEFERRED."
    )
    translation = (
        "We booked LNCH-01JXYZ4 against pool P-2 on 2026-09-05. "
        "The match-up is still pending. "
        "So the 3 gates after it stay deferred."
    )
    report = check_style(original, translation)
    assert report.ok, report.as_dict()


def test_check_style_refuses_an_unknown_mode():
    with pytest.raises(ValueError, match="style_mode"):
        check_style("a", "b", style_mode="terse")
