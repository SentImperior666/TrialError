"""Tests for the ``extraction_quality_suspect`` doctor check
(``trialerror.ingest.checks``).

Three properties this check is defined by, and each has a test that would
fail if it were quietly lost: it WARNs and never FAILs, it SAMPLES (so its
cost does not grow with the corpus) and its sample is SEEDED (so an
unchanged corpus reports the same documents twice running).
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest import quality
from trialerror.ingest.checks import check_extraction_quality_suspect
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, registered_checks, run_checks
from tests._ingest_fixtures import bootstrap_launch
from tests._quality_fixtures import (
    CLEAN_TEXT,
    GLUED_TEXT,
    seed_document,
    seed_many_documents,
    seed_quality_corpus,
)


def _ctx(program_root) -> DoctorContext:
    return DoctorContext(program_root=program_root)


def _write_config(program_root, body: str, *, valid: bool = True) -> None:
    """A program config carrying only what ``load_config`` demands (the
    ``[program]`` table) plus the table under test."""
    head = '[program]\nid = "PROG-quality-fixture"\n\n' if valid else ""
    (program_root / "trialerror.toml").write_text(head + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# registration + the empty cases
# ---------------------------------------------------------------------------
def test_the_check_is_registered_under_the_ingest_category():
    discover_and_register_checks()
    registry = registered_checks()
    assert "extraction_quality_suspect" in registry
    assert registry["extraction_quality_suspect"][0] == "ingest"


def test_skip_without_a_knowledge_db(tmp_path):
    result = check_extraction_quality_suspect(_ctx(tmp_path / "nowhere"))
    assert result.status == "skip"


def test_skip_on_a_corpus_with_no_documents(store, program_root):
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "skip"
    assert result.details["corpus_documents"] == 0


def test_pass_on_a_clean_corpus(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_document(store, launch_id, [CLEAN_TEXT, CLEAN_TEXT], page_count=2)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["suspect_count"] == 0


def test_pass_when_nothing_is_measurable_yet(store, program_root):
    """A registered-but-not-normalized corpus is not a clean corpus and not
    a suspect one -- the pipeline's stage chain reports where it is."""
    launch_id = bootstrap_launch(store)
    seed_document(store, launch_id, None, status="registered")
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["measurable"] == 0
    assert "unmeasurable" in result.message or "no document" in result.message


# ---------------------------------------------------------------------------
# warn, never fail
# ---------------------------------------------------------------------------
def test_warn_on_a_corpus_with_bad_extractions(store, program_root):
    launch_id = bootstrap_launch(store)
    docs = seed_quality_corpus(store, launch_id)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "warn"
    assert result.details["suspect_count"] == 3  # glued, unusable, fragments
    worst_ids = [r["doc_id"] for r in result.details["worst"]]
    assert docs["glued"] in worst_ids
    assert docs["clean"] == worst_ids[-1]  # clean sorts last, by severity


def test_the_check_never_fails_however_bad_the_corpus_is(store, program_root):
    launch_id = bootstrap_launch(store)
    for _ in range(6):
        seed_document(store, launch_id, GLUED_TEXT)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "warn"
    assert result.status != "fail"


def test_a_document_whose_measurement_raises_is_reported_not_raised(store, program_root, monkeypatch):
    """``run_checks`` turns a raise into a FAIL, which this check has
    promised never to be."""
    launch_id = bootstrap_launch(store)
    seed_document(store, launch_id, CLEAN_TEXT)

    def boom(_store, doc_id):
        raise RuntimeError("unreadable element text")

    monkeypatch.setattr(quality, "measure_document", boom)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["unreadable"][0]["error"].startswith("RuntimeError")


def test_the_check_is_warn_at_most_through_run_checks(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_quality_corpus(store, launch_id)
    discover_and_register_checks()
    results = run_checks(_ctx(program_root), only=["extraction_quality_suspect"])
    assert [r.status for r in results] == ["warn"]


# ---------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------
def test_the_check_samples_rather_than_scanning_the_whole_corpus(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_many_documents(store, launch_id, 12)
    _write_config(program_root, "[ingest.quality]\nsample = 4\nseed = 3\n")
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.details["corpus_documents"] == 12
    assert result.details["sampled"] == 4
    assert "seeded sample of 4 of 12" in result.message


def test_the_sample_is_seeded_so_two_runs_name_the_same_documents(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_many_documents(store, launch_id, 20)
    _write_config(program_root, "[ingest.quality]\nsample = 5\nseed = 11\n")
    first = check_extraction_quality_suspect(_ctx(program_root))
    again = check_extraction_quality_suspect(_ctx(program_root))
    ids = lambda r: [row["doc_id"] for row in r.details["worst"]]  # noqa: E731
    assert ids(first) == ids(again)

    _write_config(program_root, "[ingest.quality]\nsample = 5\nseed = 12\n")
    other = check_extraction_quality_suspect(_ctx(program_root))
    assert set(ids(other)) != set(ids(first))


def test_the_default_sample_bounds_the_work_on_a_corpus_larger_than_it(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_many_documents(store, launch_id, quality.DEFAULT_SAMPLE + 7)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.details["sampled"] == quality.DEFAULT_SAMPLE
    assert result.details["corpus_documents"] == quality.DEFAULT_SAMPLE + 7


@pytest.mark.parametrize("configured", ["-1", "0"])
def test_a_non_positive_configured_sample_cannot_make_the_check_full_scan(store, program_root, configured):
    """Fix pass V-6: `sample = -1` reached the sampler as "no sample" and
    made the check measure the whole corpus on every doctor run -- the one
    thing its docstring promises never to do -- while `sample = 0` returned
    a PASS whose message described a corpus it had not measured."""
    launch_id = bootstrap_launch(store)
    seed_many_documents(store, launch_id, quality.DEFAULT_SAMPLE + 5)
    _write_config(program_root, f"[ingest.quality]\nsample = {configured}\n")
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.details["sample_size"] == quality.DEFAULT_SAMPLE
    assert result.details["sampled"] == quality.DEFAULT_SAMPLE
    assert "the whole corpus" not in result.message


def test_a_corpus_smaller_than_the_sample_is_measured_whole(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_many_documents(store, launch_id, 3)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.details["sampled"] == 3
    assert "the whole corpus" in result.message


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def test_thresholds_come_from_the_programs_config(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_document(store, launch_id, GLUED_TEXT)
    assert check_extraction_quality_suspect(_ctx(program_root)).status == "warn"

    # a program that has decided glued tokens and missing terminators are
    # what its corpus looks like
    _write_config(
        program_root,
        "[ingest.quality]\nglued_token_rate_max = 1.0\nterminator_density_min = 0.0\n",
    )
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["thresholds"]["glued_token_rate_max"] == 1.0


def test_worst_n_bounds_the_reported_rows(store, program_root):
    launch_id = bootstrap_launch(store)
    for _ in range(8):
        seed_document(store, launch_id, GLUED_TEXT)
    _write_config(program_root, "[ingest.quality]\nworst_n = 2\n")
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert len(result.details["worst"]) == 2
    assert result.details["suspect_count"] == 8


def test_an_unparseable_config_reports_against_the_defaults(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_document(store, launch_id, GLUED_TEXT)
    _write_config(program_root, "[ingest.quality\nthis is not toml", valid=False)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "warn"
    assert result.details["thresholds"]["glued_token_rate_max"] == (
        quality.DEFAULT_THRESHOLDS["glued_token_rate_max"]
    )


def test_the_result_is_json_serializable(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_quality_corpus(store, launch_id)
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert json.loads(json.dumps(result.to_dict()))["name"] == "extraction_quality_suspect"


# ---------------------------------------------------------------------------
# FB-1b item 4: the size floor, from the check's side
# ---------------------------------------------------------------------------
TINY_NOTE = "sync the offload queue then re-run the worker"  # 8 tokens, no terminator


def test_a_corpus_of_tiny_notes_passes_and_says_why(store, program_root):
    launch_id = bootstrap_launch(store)
    for _ in range(4):
        seed_document(store, launch_id, TINY_NOTE)
    store.close()
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["suspect_count"] == 0
    assert result.details["below_min_tokens"] == 4
    assert result.details["min_tokens"] == quality.DEFAULT_MIN_TOKENS
    assert "min_tokens" in result.message
    # the numbers are still there to read
    worst = {r["doc_id"]: r for r in result.details["worst"]}
    assert all(r["below_min_tokens"] is True and r["suspect"] is False for r in worst.values())
    assert all(r["terminator_density"] == 0.0 for r in worst.values())


def test_the_floor_does_not_hide_a_real_suspect_beside_the_notes(store, program_root):
    launch_id = bootstrap_launch(store)
    for _ in range(3):
        seed_document(store, launch_id, TINY_NOTE)
    bad = seed_document(store, launch_id, GLUED_TEXT)
    store.close()
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "warn"
    assert result.details["suspect_count"] == 1
    assert result.details["below_min_tokens"] == 3
    assert bad in result.message
    assert result.details["worst"][0]["doc_id"] == bad


def test_a_configured_floor_is_what_the_check_uses(store, program_root):
    launch_id = bootstrap_launch(store)
    seed_document(store, launch_id, TINY_NOTE)
    store.close()
    _write_config(program_root, "[ingest.quality]\nmin_tokens = 0\n")
    result = check_extraction_quality_suspect(_ctx(program_root))
    assert result.status == "warn"
    assert result.details["below_min_tokens"] == 0
    assert result.details["min_tokens"] == 0
