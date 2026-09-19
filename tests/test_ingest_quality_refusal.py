"""Tests for the OPT-IN extraction-quality refusal
(``[ingest.quality] refuse_below``) in the normalize/OCR tail.

The property that matters most here is the one about what happens WITHOUT
configuration: a program that has not asked for a refusal must behave
exactly as it did before this code existed, on every document, however bad
its text is.
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest import pipeline, quality
from trialerror.ingest.errors import DocumentQualityRefusedError
from trialerror.ingest.pipeline_status import document_pipeline
from trialerror.jobs.worker import run_one
from trialerror.stores.writer import get
from tests._ingest_fixtures import bootstrap_launch, write_scanned_pdf_fixture
from tests._quality_fixtures import CLEAN_TEXT, GLUED_TEXT, NO_TERMINATOR_TEXT


def _write_config(program_root, quality_body: str = "") -> None:
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-refusal-fixture"\n\n' + quality_body, encoding="utf-8"
    )


def _ingest_text(store, program_root, text: str, *, name: str = "doc.txt"):
    """One document whose recognized text is exactly ``text``: the fake OCR
    backend reads the raw file's own bytes as its page text, which is the
    only way to hand the normalize tail a chosen string end to end."""
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="book", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / name
    write_scanned_pdf_fixture(path, [text])
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=path,
        created_by_launch=launch_id, media_type="pdf-scan",
    )
    return result["document"]["doc_id"], launch_id


def _run_next(store, worker_id="w1"):
    return run_one(store, worker_id=worker_id)


def _drain(store, max_steps=10):
    for i in range(max_steps):
        if run_one(store, worker_id=f"w{i}")["status"] == "idle":
            break


# ---------------------------------------------------------------------------
# absent by default
# ---------------------------------------------------------------------------
def test_without_configuration_bad_text_normalizes_exactly_as_before(store, program_root):
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    assert _run_next(store)["status"] == "complete"
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    assert doc["status"] == "normalized"
    assert quality.quality_refusal_record(store.knowledge, doc_id) is None


def test_without_configuration_the_pipeline_chains_onward(store, program_root):
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    _drain(store)
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    assert doc["status"] == "indexed"
    assert store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0] > 0


def test_an_empty_refuse_below_table_refuses_nothing(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = {}\n")
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    assert _run_next(store)["status"] == "complete"
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "normalized"


def test_thresholds_that_the_text_passes_refuse_nothing(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.5 }\n")
    doc_id, _launch = _ingest_text(store, program_root, CLEAN_TEXT)
    assert _run_next(store)["status"] == "complete"
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "normalized"


# ---------------------------------------------------------------------------
# configured: the refusal itself
# ---------------------------------------------------------------------------
def test_a_configured_refusal_writes_failed_and_stops_the_pipeline(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    assert _run_next(store)["status"] == "complete"  # the JOB succeeded; the document did not

    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    assert doc["status"] == "failed"
    # the pipeline stops: no chunk job was enqueued, so nothing downstream runs
    _drain(store)
    assert store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0] == 0
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "failed"


def test_the_refusal_record_carries_the_four_numbers_and_the_reasons(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, launch_id = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)

    record = quality.quality_refusal_record(store.knowledge, doc_id)
    assert record is not None
    assert set(record["measures"]) == set(quality.MEASURE_KEYS)
    assert record["thresholds"] == {"glued_token_rate_max": 0.2}
    assert any("glued_token_rate" in r for r in record["reasons"])
    assert record["launch_id"] == launch_id


def test_the_refusal_appends_an_event(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)

    rows = store.ops.execute(
        "SELECT payload FROM event WHERE type = 'ingest_quality_refused'"
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["doc_id"] == doc_id


def test_the_elements_and_the_archived_text_survive_a_refusal(store, program_root):
    """A refusal that destroyed its own evidence would be unarguable."""
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)

    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    assert store.knowledge.execute("SELECT COUNT(*) FROM element WHERE doc_id=?", (doc_id,)).fetchone()[0] > 0
    assert (store.program_root / doc["rel_path"]).is_file()
    assert quality.measure_document(store, doc_id)["measurable"] is True


def test_only_the_configured_measure_can_refuse(store, program_root):
    """A program that configured the glued-token bound has not configured
    the terminator bound, and text that trips only the second one passes."""
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, _launch = _ingest_text(store, program_root, NO_TERMINATOR_TEXT)
    _run_next(store)
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "normalized"


def test_the_refusal_is_idempotent_across_a_rerun_of_the_stage(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, launch_id = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    # a crash-resume re-runs the same stage against the same document
    pipeline.requeue_stage(store, doc_id=doc_id, kind="ocr", created_by_launch=launch_id)
    _run_next(store, worker_id="w2")

    count = store.knowledge.execute(
        "SELECT COUNT(*) FROM record WHERE register_key = ?", (quality.QUALITY_REFUSAL_REGISTER_KEY,)
    ).fetchone()[0]
    assert count == 1
    events = store.ops.execute(
        "SELECT COUNT(*) FROM event WHERE type = 'ingest_quality_refused'"
    ).fetchone()[0]
    assert events == 1


def test_ingest_status_shows_a_refused_document_as_failed_with_the_reason(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)

    out = document_pipeline(store, doc_id)
    assert out["pipeline_state"] == "failed"
    assert "refused" in out["reason"]
    assert out["quality_refusal"]["reasons"]
    # the job itself is complete -- it did exactly what it was asked to
    assert {s["state"] for s in out["stages"]} == {"complete"}


def test_a_refused_document_is_not_reported_as_a_retraction(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    from trialerror.ingest.retract import retracted_doc_ids

    assert doc_id not in retracted_doc_ids(store.knowledge)


def test_an_unparseable_config_fails_the_job_rather_than_silently_not_refusing(store, program_root):
    """Fail-closed (D13): a program whose operator configured a refusal must
    not stop refusing because an unrelated typo made the file unreadable."""
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    (program_root / "trialerror.toml").write_text("[ingest.quality\nbroken", encoding="utf-8")
    result = _run_next(store)
    assert result["status"] == "failed"
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "registered"


def test_a_refusal_threshold_written_as_a_string_still_refuses(store, program_root):
    _write_config(program_root, '[ingest.quality]\nrefuse_below = { glued_token_rate_max = "0.2" }\n')
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "failed"


# ---------------------------------------------------------------------------
# fix pass V-1/V-3: the refusal holds against every re-derivation door
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["chunk", "embed", "index"])
def test_requeue_stage_refuses_a_quality_refused_document(store, program_root, kind):
    """V-1: `ingest rechunk`/`re-embed` would otherwise chunk, embed and
    index text this program declared unusable -- a working un-refuse."""
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, launch_id = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "failed"

    before = store.jobs.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    with pytest.raises(DocumentQualityRefusedError) as excinfo:
        pipeline.requeue_stage(store, doc_id=doc_id, kind=kind, created_by_launch=launch_id)
    assert "refuse_below" in str(excinfo.value)
    assert store.jobs.execute("SELECT COUNT(*) FROM job").fetchone()[0] == before


def test_a_refused_document_stays_unchunked_after_a_rechunk_attempt(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, launch_id = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    with pytest.raises(DocumentQualityRefusedError):
        pipeline.requeue_stage(store, doc_id=doc_id, kind="chunk", created_by_launch=launch_id)
    _drain(store)
    assert store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0] == 0
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "failed"


def test_the_guard_leaves_an_unrefused_document_alone(store, program_root):
    """The guard reads the refusal record AND document.status: a document
    that was never refused requeues exactly as before."""
    doc_id, launch_id = _ingest_text(store, program_root, CLEAN_TEXT)
    _drain(store)
    job = pipeline.requeue_stage(store, doc_id=doc_id, kind="chunk", created_by_launch=launch_id)
    assert job["state"] == "pending"


def test_re_measuring_the_text_is_the_way_out_of_a_refusal(store, program_root):
    """The documented escape: relax `refuse_below`, re-run the extraction
    stage (NOT guarded -- it re-measures), and the pipeline drains."""
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, launch_id = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "failed"

    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 1.0 }\n")
    pipeline.requeue_stage(store, doc_id=doc_id, kind="ocr", created_by_launch=launch_id)
    _drain(store)

    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "indexed"
    assert store.knowledge.execute("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0] > 0


def test_a_retired_refusal_no_longer_reads_as_the_pipeline_state(store, program_root):
    """V-3: the record is history and stays in the envelope; it stops being
    the STATE the moment document.status says something else."""
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, launch_id = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    assert document_pipeline(store, doc_id)["pipeline_state"] == "failed"

    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 1.0 }\n")
    pipeline.requeue_stage(store, doc_id=doc_id, kind="ocr", created_by_launch=launch_id)
    _drain(store)

    out = document_pipeline(store, doc_id)
    assert out["pipeline_state"] == "complete"
    assert "refused" not in out["reason"]
    # the record itself survives as history, readable in the same envelope
    assert out["quality_refusal"]["reasons"]


def test_the_guard_is_released_with_the_refusal(store, program_root):
    """Once the text measures acceptably the requeue door opens again --
    there is no separate flag to clear."""
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 0.2 }\n")
    doc_id, launch_id = _ingest_text(store, program_root, GLUED_TEXT)
    _run_next(store)
    _write_config(program_root, "[ingest.quality]\nrefuse_below = { glued_token_rate_max = 1.0 }\n")
    pipeline.requeue_stage(store, doc_id=doc_id, kind="ocr", created_by_launch=launch_id)
    _drain(store)
    job = pipeline.requeue_stage(store, doc_id=doc_id, kind="chunk", created_by_launch=launch_id)
    assert job["state"] == "pending"


# ---------------------------------------------------------------------------
# fix pass V-2: an unreadable refusal bound stops the job, not the document
# ---------------------------------------------------------------------------
def test_a_mistyped_refusal_bound_fails_the_job_instead_of_refusing_the_document(store, program_root):
    """V-2: the value used to fall back to the conservative WARN default,
    so `terminator_density_min = "eight"` refused real documents against
    1.0 -- a number nobody wrote."""
    _write_config(program_root, '[ingest.quality]\nrefuse_below = { terminator_density_min = "eight" }\n')
    doc_id, _launch = _ingest_text(store, program_root, CLEAN_TEXT)
    result = _run_next(store)

    assert result["status"] == "failed"
    doc = get(store, "document", pk_column="doc_id", pk_value=doc_id)
    assert doc["status"] == "registered"
    assert quality.quality_refusal_record(store.knowledge, doc_id) is None


def test_a_refuse_below_that_is_not_a_table_fails_the_job(store, program_root):
    _write_config(program_root, "[ingest.quality]\nrefuse_below = 0.35\n")
    doc_id, _launch = _ingest_text(store, program_root, GLUED_TEXT)
    assert _run_next(store)["status"] == "failed"
    assert get(store, "document", pk_column="doc_id", pk_value=doc_id)["status"] == "registered"
