"""``trialerror.verify.citecheck`` — the two-tier citation-check pipeline
(design Section 8.1)."""

from __future__ import annotations

import json

import pytest

from trialerror.verify.citecheck import (
    CITATION_MARKER_RE,
    build_citecheck_judgment_envelope,
    extract_citation_pairs,
    run_citecheck,
)
from trialerror.verify.errors import CitecheckError

from tests._verify_fixtures import anchor_for_chunk, bootstrap_launch, build_small_corpus

# The fixture's open-source chunk text (see tests/_retrieve_fixtures.py::_add_document):
#   "Distributed schedulers use retry budgets to bound tail latency during failover.\n\n
#    A coordinator arbitrates lock conflicts and records the consequences of worker actions."
_MATCHING_SENTENCE = "Distributed schedulers use retry budgets to bound tail latency during failover."
_MISMATCHED_SENTENCE = "Combat resolves through opposed rolls in this alternate system entirely."


# ---------------------------------------------------------------------------
# extract_citation_pairs
# ---------------------------------------------------------------------------


def test_extract_citation_pairs_binds_marker_to_preceding_sentence():
    text = "First sentence here. [[cite:ANC-AAA]] Second sentence follows. [[cite:ANC-BBB]]"
    pairs = extract_citation_pairs(text)
    assert [p["sentence"] for p in pairs] == ["First sentence here.", "Second sentence follows."]
    assert [p["anchor_id"] for p in pairs] == ["ANC-AAA", "ANC-BBB"]
    assert [p["pair_id"] for p in pairs] == ["CPR-1", "CPR-2"]


def test_extract_citation_pairs_empty_text_yields_no_pairs():
    assert extract_citation_pairs("no markers in this text at all") == []


def test_citation_marker_regex_requires_anc_prefix():
    assert CITATION_MARKER_RE.search("[[cite:XYZ-123]]") is None
    assert CITATION_MARKER_RE.search("[[cite:ANC-123abc]]") is not None


# ---------------------------------------------------------------------------
# run_citecheck: input validation
# ---------------------------------------------------------------------------


def test_run_citecheck_requires_exactly_one_of_text_or_pairs(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(CitecheckError):
        run_citecheck(store, subject_id="S", issued_by_launch=launch_id)
    with pytest.raises(CitecheckError):
        run_citecheck(store, subject_id="S", text="x", pairs=[], issued_by_launch=launch_id)


# ---------------------------------------------------------------------------
# mechanical pass
# ---------------------------------------------------------------------------


def test_mechanical_pass_when_sentence_shares_a_six_word_shingle_with_the_anchor(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = f"{_MATCHING_SENTENCE} [[cite:{anchor['anchor_id']}]]"

    result = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id)
    assert len(result["pairs"]) == 1
    pair = result["pairs"][0]
    assert pair["status"] == "mechanical_pass"
    assert result["failures"] == []
    assert result["summary"]["overall"] == "PASS"
    assert result["summary"]["mechanical_pass"] == 1
    assert len(result["verdict_rows"]) == 1
    assert result["verdict_rows"][0]["label"] == "PASS"


def test_anchor_not_found_is_an_immediate_failure_no_escalation(store):
    launch_id = bootstrap_launch(store)
    build_small_corpus(store, launch_id=launch_id)
    text = f"{_MATCHING_SENTENCE} [[cite:ANC-0000000000000000000000]]"

    result = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id)
    pair = result["pairs"][0]
    assert pair["status"] == "anchor_not_found"
    assert pair in result["failures"]
    assert result["summary"]["overall"] == "FAIL"
    assert result["summary"]["anchor_not_found"] == 1
    # a fail is still recorded as a verdict row (PASS/FAIL label = FAIL) plus the summary
    assert any(r["label"] == "FAIL" for r in result["verdict_rows"])


def test_anchor_stale_when_the_anchor_no_longer_resolves_byte_exact(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    # simulate a re-normalization shifting the stored quote out from under the anchor
    store.knowledge.execute("UPDATE quote_anchor SET quote_text = ? WHERE anchor_id = ?", ("totally different text now", anchor["anchor_id"]))
    store.knowledge.commit()

    text = f"{_MATCHING_SENTENCE} [[cite:{anchor['anchor_id']}]]"
    result = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id)
    assert result["pairs"][0]["status"] == "anchor_stale"
    assert result["summary"]["anchor_stale"] == 1
    assert result["summary"]["overall"] == "FAIL"


# ---------------------------------------------------------------------------
# escalation: sampling + judge
# ---------------------------------------------------------------------------


def test_mismatched_sentence_escalates_and_stays_pending_selected_without_a_judge(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = f"{_MISMATCHED_SENTENCE} [[cite:{anchor['anchor_id']}]]"

    result = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id, judge=None, sample_rate=1)
    pair = result["pairs"][0]
    assert pair["status"] == "escalation_selected"
    assert "judgment_envelope" in pair
    assert result["failures"] == []  # not a failure yet -- unresolved, not judged
    assert result["verdict_rows"] == []  # nothing recorded until judged
    assert result["summary"]["escalation_selected"] == 1


def test_judge_supported_label_becomes_llm_pass(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = f"{_MISMATCHED_SENTENCE} [[cite:{anchor['anchor_id']}]]"

    result = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id, judge=lambda env: "supported", sample_rate=1)
    assert result["pairs"][0]["status"] == "llm_pass"
    assert result["failures"] == []
    assert result["summary"]["overall"] == "PASS"
    assert len(result["verdict_rows"]) == 1


def test_judge_unsupported_label_becomes_llm_fail(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = f"{_MISMATCHED_SENTENCE} [[cite:{anchor['anchor_id']}]]"

    result = run_citecheck(
        store, subject_id="ART-1", text=text, issued_by_launch=launch_id,
        judge=lambda env: {"label": "unsupported", "note": "no support found"}, sample_rate=1,
    )
    assert result["pairs"][0]["status"] == "llm_fail"
    assert result["pairs"][0]["judge_note"] == "no support found"
    assert result["failures"]
    assert result["summary"]["overall"] == "FAIL"


def test_deterministic_sampling_all_number_bearing_plus_every_kth_of_the_rest(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    aid = anchor["anchor_id"]

    rest_sentences = [
        "Bravo team regroups near dawn to prepare an assault.",
        "Echo squad retreats behind cover under heavy fire.",
        "Foxtrot patrol scouts the eastern ridge quietly.",
        "Golf team waits for the signal near the bridge.",
    ]
    number_sentence = "There are 7 soldiers waiting in reserve nearby."
    all_sentences = rest_sentences + [number_sentence]
    text = " ".join(f"{s} [[cite:{aid}]]" for s in all_sentences)

    result = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id, judge=None, sample_rate=2)
    by_sentence = {p["sentence"]: p["status"] for p in result["pairs"]}

    # number-bearing sentence: always escalated (100%)
    assert by_sentence[number_sentence] == "escalation_selected"
    # of the 4 non-number-bearing "rest" pairs, every 2nd is sampled: indices 0 and 2
    assert by_sentence[rest_sentences[0]] == "escalation_selected"
    assert by_sentence[rest_sentences[1]] == "escalation_not_sampled"
    assert by_sentence[rest_sentences[2]] == "escalation_selected"
    assert by_sentence[rest_sentences[3]] == "escalation_not_sampled"


def test_sampling_is_deterministic_across_repeated_runs(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    aid = anchor["anchor_id"]
    text = " ".join(f"Sentence number {i} about nothing shared. [[cite:{aid}]]" for i in range(6))

    r1 = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id, judge=None, sample_rate=3)
    r2 = run_citecheck(store, subject_id="ART-2", text=text, issued_by_launch=launch_id, judge=None, sample_rate=3)
    statuses_1 = [p["status"] for p in r1["pairs"]]
    statuses_2 = [p["status"] for p in r2["pairs"]]
    assert statuses_1 == statuses_2


def test_sample_rate_must_be_at_least_one(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = f"{_MISMATCHED_SENTENCE} [[cite:{anchor['anchor_id']}]]"
    with pytest.raises(CitecheckError):
        run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id, sample_rate=0)


# ---------------------------------------------------------------------------
# pre-extracted claim-set (pairs=) input
# ---------------------------------------------------------------------------


def test_run_citecheck_accepts_pre_extracted_pairs(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    pairs = [{"pair_id": "CPR-1", "sentence": _MATCHING_SENTENCE, "marker": "[[cite:x]]", "anchor_id": anchor["anchor_id"], "char_start": 0, "char_end": 0}]

    result = run_citecheck(store, subject_id="claimset-1", pairs=pairs, issued_by_launch=launch_id)
    assert result["pairs"][0]["status"] == "mechanical_pass"


# ---------------------------------------------------------------------------
# judgment envelope shape
# ---------------------------------------------------------------------------


def test_judgment_envelope_carries_sentence_marker_anchor_and_fixed_label_vocabulary(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    pair = {
        "pair_id": "CPR-1", "sentence": _MISMATCHED_SENTENCE, "marker": f"[[cite:{anchor['anchor_id']}]]",
        "anchor_id": anchor["anchor_id"], "anchor_quote": anchor["quote_text"],
    }
    envelope = build_citecheck_judgment_envelope(pair)
    assert envelope["sentence"] == _MISMATCHED_SENTENCE
    assert envelope["anchor_id"] == anchor["anchor_id"]
    assert envelope["labels"] == ["supported", "unsupported", "uncertain"]
    assert "supported" in envelope["instruction"]


# ---------------------------------------------------------------------------
# verdict rows written per resolved pair
# ---------------------------------------------------------------------------


def test_verdict_rows_carry_anchor_and_chunk_evidence(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = f"{_MATCHING_SENTENCE} [[cite:{anchor['anchor_id']}]]"
    result = run_citecheck(store, subject_id="ART-1", text=text, issued_by_launch=launch_id)
    row = result["verdict_rows"][0]
    assert row["subject_kind"] == "citation"
    assert row["subject_id"] == anchor["anchor_id"]
    assert row["procedure"] == "citecheck"
    evidence = json.loads(row["evidence"])
    assert evidence[0]["anchor_id"] == anchor["anchor_id"]
    assert evidence[0]["chunk_id"] == corpus["open_chunk_ids"][0]
