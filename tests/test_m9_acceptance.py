"""M9 acceptance criteria, design Section 12 row, gathered in one place --
mirrors the ``tests/test_m10_acceptance.py``/``tests/test_m8_acceptance.py``
convention: this file IS the acceptance-criteria mapping, each test here
re-running (not replacing) a narrower assertion that already lives in its
dedicated module.

    | Acceptance criterion (design Section 12, M9 row)                                          | Test |
    |-----------------------------------------------------------------------------------------------|------|
    | fixture artifact w/ 1 broken citation -> caught mechanically                                   | test_fixture_artifact_with_one_broken_citation_is_caught_mechanically (see test_verify_citecheck.py) |
    | sampling deterministic across re-runs                                                          | test_escalation_sampling_is_deterministic_across_reruns (see test_verify_citecheck.py::test_sampling_is_deterministic_across_repeated_runs) |
    | hypothesis fixture (planted agreement+contradiction chunks) -> correct label distribution + verdict row w/ prereg_compliant stamp | test_hypothesis_fixture_with_planted_agreement_and_contradiction_yields_mixed_status_and_stamped_verdict (see test_verify_hypothesis.py) |
    | reproduce fixture mismatch -> recorded + blocks gate path                                      | test_reproduce_fixture_mismatch_is_recorded_and_blocks_the_gate_path (see test_verify_reproduce.py) |
    | reveal w/ tampered escrow refused                                                              | test_reveal_with_tampered_escrow_is_refused (see test_verify_prereg.py) |
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trialerror.artifacts.errors import GateEntryConditionError
from trialerror.artifacts.gates import apply_union, open_gate, record_verdict as gate_record_verdict, submit_gate
from trialerror.stores.writer import get, insert
from trialerror.util.ids import new_id
from trialerror.verify.citecheck import run_citecheck
from trialerror.verify.errors import PreregTamperedError
from trialerror.verify.hypothesis import run_hypothesis_verification
from trialerror.verify.prereg import commit_prereg, reveal_prereg
from trialerror.verify.reproduce import reproduce_verdict
from trialerror.verify.verdicts import record_verdict

from tests._verify_fixtures import anchor_for_chunk, bootstrap_launch, build_small_corpus

pytestmark = pytest.mark.acceptance


# ---------------------------------------------------------------------------
# criterion 1: fixture artifact w/ 1 broken citation -> caught mechanically
# ---------------------------------------------------------------------------


def test_fixture_artifact_with_one_broken_citation_is_caught_mechanically(store):
    """Two citations in one artifact: one genuinely supported by its
    anchor (mechanical pass, no LLM), one BROKEN -- cites an anchor that
    does not exist. The mechanical pass alone (zero LLM, no judge given)
    must catch the broken one and put it in ``failures`` with the exact
    sentence/marker/anchor (design Section 8.1's own "surgical patching"
    wording)."""
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    good_anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])

    good_sentence = "Distributed schedulers use retry budgets to bound tail latency during failover."
    broken_sentence = "This claim cites a citation that was never actually anchored anywhere."
    text = (
        f"{good_sentence} [[cite:{good_anchor['anchor_id']}]] "
        f"{broken_sentence} [[cite:ANC-0000000000000000000BROKEN]]"
    )

    result = run_citecheck(store, subject_id="ART-fixture-1", text=text, issued_by_launch=launch_id)

    assert result["summary"]["overall"] == "FAIL"
    assert result["summary"]["mechanical_pass"] == 1
    assert result["summary"]["anchor_not_found"] == 1
    assert len(result["failures"]) == 1

    broken = result["failures"][0]
    assert broken["sentence"] == broken_sentence
    assert broken["marker"] == "[[cite:ANC-0000000000000000000BROKEN]]"
    assert broken["anchor_id"] == "ANC-0000000000000000000BROKEN"
    assert broken["status"] == "anchor_not_found"

    # the broken pair's own FAIL verdict is recorded, distinct from the good pair's PASS
    labels_by_subject = {row["subject_id"]: row["label"] for row in result["verdict_rows"]}
    assert labels_by_subject[good_anchor["anchor_id"]] == "PASS"
    assert labels_by_subject["ANC-0000000000000000000BROKEN"] == "FAIL"


# ---------------------------------------------------------------------------
# criterion 2: sampling deterministic across re-runs
# ---------------------------------------------------------------------------


def test_escalation_sampling_is_deterministic_across_reruns(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = anchor_for_chunk(store, corpus["open_chunk_ids"][0])
    text = " ".join(f"Unrelated filler sentence number {i} sharing nothing. [[cite:{anchor['anchor_id']}]]" for i in range(6))

    first = run_citecheck(store, subject_id="ART-sample-1", text=text, issued_by_launch=launch_id, judge=None, sample_rate=2)
    second = run_citecheck(store, subject_id="ART-sample-2", text=text, issued_by_launch=launch_id, judge=None, sample_rate=2)
    assert [p["status"] for p in first["pairs"]] == [p["status"] for p in second["pairs"]]


# ---------------------------------------------------------------------------
# criterion 3: hypothesis fixture (planted agreement+contradiction) ->
# correct label distribution + verdict row w/ prereg_compliant stamp
# ---------------------------------------------------------------------------


def test_hypothesis_fixture_with_planted_agreement_and_contradiction_yields_mixed_status_and_stamped_verdict(store):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    open_chunk_id = corpus["open_chunk_ids"][0]
    restricted_chunk_id = corpus["restricted_chunk_ids"][0]

    # planted per-chunk stances: the open-source chunk AGREES, the
    # restricted-source chunk CONTRADICTS -- a deterministic fake judge,
    # never a real LLM call (this build's LLM-judgment boundary).
    planted = {open_chunk_id: "explicit agreement", restricted_chunk_id: "explicit contradiction"}

    def fixture_judge(envelope):
        return planted[envelope["chunk_id"]]

    result = run_hypothesis_verification(
        store, hypothesis_text="rulebooks universally describe combat and dice mechanics the same way",
        query="distributed systems retry coordinator lock", judge=fixture_judge, issued_by_launch=launch_id, mode="vector",
        k_total=2, prereg=True,
    )

    assert result["status"] == "mixed"
    assert result["distribution"].get("explicit agreement", 0) >= 1
    assert result["distribution"].get("explicit contradiction", 0) >= 1

    verdict = result["verdict"]
    assert verdict["procedure"] == "contracrow"
    assert verdict["label"] == "mixed"
    assert verdict["prereg_id"] == result["prereg_id"]
    assert verdict["prereg_compliant"] == 1  # stamped True: the executed procedure/params matched the commit
    evidence = json.loads(verdict["evidence"])
    stances = {e["chunk_id"]: e["stance"] for e in evidence}
    assert stances[open_chunk_id] == "explicit agreement"
    assert stances[restricted_chunk_id] == "explicit contradiction"


# ---------------------------------------------------------------------------
# criterion 4: reproduce fixture mismatch -> recorded + blocks gate path
# ---------------------------------------------------------------------------


def test_reproduce_fixture_mismatch_is_recorded_and_blocks_the_gate_path(store, tmp_path):
    launch_id = bootstrap_launch(store)
    insert(store, "template", {"type_key": "m9-fixture-note", "title": "Note", "version": "1", "path": "templates/note.md", "gated": 1})
    artifact_id = new_id("ART")
    insert(
        store, "artifact",
        {
            "artifact_id": artifact_id, "type": "m9-fixture-note", "title": "t", "path": "x.md", "sha256": "0" * 64,
            "status": "draft", "registered_ts": None, "registered_by_launch": launch_id,
        },
    )
    gate = open_gate(store, artifact_id=artifact_id)
    gate = submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    gate_record_verdict(store, gate_id=gate["gate_id"], verdict="PASS", by_launch=launch_id)

    script = tmp_path / "mismatch_script.py"
    script.write_text("import sys\nsys.stdout.write('actual output')\n", encoding="utf-8")
    ref = json.dumps({"script": str(script), "args": [], "expected_sha256": "f" * 64})  # deliberately wrong
    original = record_verdict(
        store, subject_kind="artifact", subject_id=artifact_id, procedure="citecheck", procedure_version="1",
        label="PASS", reproduction_ref=ref, issued_by_launch=launch_id,
    )

    result = reproduce_verdict(store, verdict_id=original["verdict_id"], gate_id=gate["gate_id"], issued_by_launch=launch_id)
    assert result["status"] == "mismatch"
    assert result["verdict"]["procedure"] == "reproduction"
    assert result["verdict"]["label"] == "mismatch"

    refreshed_gate = get(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"])
    assert refreshed_gate["reproduction_status"] == "mismatch"

    # the reproduction mismatch BLOCKS the gate path -- apply_union refuses (design Section 4.2, F10)
    with pytest.raises(GateEntryConditionError):
        apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)


# ---------------------------------------------------------------------------
# criterion 5: reveal w/ tampered escrow refused
# ---------------------------------------------------------------------------


def test_reveal_with_tampered_escrow_is_refused(store):
    committed = commit_prereg(store, title="acceptance fixture", procedure="the committed procedure text", params={"seed": 1})
    escrow_path = Path(committed["escrow_path"])
    tampered = json.loads(escrow_path.read_text(encoding="utf-8"))
    tampered["procedure"] = "a swapped-in procedure, post-commit"
    escrow_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(PreregTamperedError):
        reveal_prereg(store, prereg_id=committed["prereg_id"])

    refreshed = get(store, "prereg", pk_column="prereg_id", pk_value=committed["prereg_id"])
    assert refreshed["status"] == "voided"
