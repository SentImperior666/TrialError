"""``trialerror ingest retract`` -- the corpus's undo.

The failure this covers (seen live 2026-09-06): a document ingested through
a fake OCR stand-in put ~94k garbage elements and 624 chunks into a live
corpus and its search index, and nothing in the CLI could take them out.
Every verb was additive; ``reindex-fulltext`` rebuilt the index from a
``chunk`` table that still held the garbage.

Each document here goes through the REAL pipeline (add-source -> add ->
drain every job) so the rows being retracted are the rows ingest actually
produces, not hand-built stand-ins.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import pipeline
from trialerror.ingest.errors import DocumentNotFoundError, DocumentRetractedError
from trialerror.ingest.retract import (
    RETRACTED_DOCUMENT_STATUS,
    RETRACTION_REGISTER_KEY,
    RetractBlockedError,
    _delete_by_chunk_ids,
    is_retracted,
    retract_document,
    retracted_doc_ids,
    retraction_record,
)
from trialerror.jobs.worker import run_one
from trialerror.retrieve import tantivysearch
from trialerror.stores import paths as store_paths
from trialerror.stores.errors import XidTargetMissingError
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture, write_markdown_fixture


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        for k, v in kw.items():
            setattr(self, k, v)


def _drain(store, max_steps=12):
    for i in range(max_steps):
        if run_one(store, worker_id=f"w{i}")["status"] == "idle":
            return


def _ingest(store, program_root, *, launch_id, name="doc.html", writer=write_html_fixture, source_id=None):
    """One document, all the way to ``indexed``, through the real stages."""
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = writer(raw_dir / name)
    if source_id is None:
        source_id = pipeline.register_source(
            store, kind="paper", title=f"Fixture {name}", license_tier="open",
            acquisition_route="web", registered_by_launch=launch_id,
        )["source_id"]
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source_id, raw_path=path,
        created_by_launch=launch_id,
    )
    _drain(store)
    return result["document"]["doc_id"], source_id, path


def _counts(store, doc_id):
    q = store.knowledge.execute
    return {
        "elements": q("SELECT COUNT(*) FROM element WHERE doc_id=?", (doc_id,)).fetchone()[0],
        "chunks": q("SELECT COUNT(*) FROM chunk WHERE doc_id=?", (doc_id,)).fetchone()[0],
        "anchors": q("SELECT COUNT(*) FROM quote_anchor WHERE doc_id=?", (doc_id,)).fetchone()[0],
    }


# ---------------------------------------------------------------------------
# the core subtraction
# ---------------------------------------------------------------------------


def test_retract_removes_every_derived_row_and_keeps_the_document(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, source_id, _ = _ingest(store, program_root, launch_id=launch_id)

    before = _counts(store, doc_id)
    assert before["elements"] and before["chunks"] and before["anchors"]

    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="fake OCR stand-in")

    assert result["already_retracted"] is False
    assert result["removed"]["elements"] == before["elements"]
    assert result["removed"]["chunks"] == before["chunks"]
    assert result["removed"]["quote_anchors"] == before["anchors"]
    assert result["removed"]["embeddings"] >= 1
    assert result["removed"]["fts_rows"] == before["chunks"]
    assert _counts(store, doc_id) == {"elements": 0, "chunks": 0, "anchors": 0}

    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    assert doc["status"] == RETRACTED_DOCUMENT_STATUS
    assert doc["ocr_backend"] is None and doc["ocr_version"] is None
    assert store.knowledge.execute("SELECT COUNT(*) FROM source WHERE source_id=?", (source_id,)).fetchone()[0] == 1


def test_retract_removes_the_vector_index_rows(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    tables = [
        r[0]
        for r in store.knowledge.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vec_chunks__%'"
        ).fetchall()
    ]
    assert tables, "the index stage should have created a vector table"
    before = sum(store.knowledge.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)
    assert before

    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")

    after = sum(store.knowledge.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)
    assert after == 0
    assert result["removed"]["vec_rows"] == before


def test_retract_removes_the_derived_archive_text(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _, raw_path = _ingest(store, program_root, launch_id=launch_id)
    doc = dict(store.knowledge.execute("SELECT * FROM document WHERE doc_id=?", (doc_id,)).fetchone())
    archive = program_root / doc["rel_path"]
    assert archive.is_file()

    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")

    assert not archive.exists()
    assert doc["rel_path"] in result["files_removed"]
    assert raw_path.is_file(), "the RAW input is the operator's file, never the pipeline's to delete"


def test_retract_removes_a_derived_pdf_tree(store, program_root):
    """The djvu route leaves ``<archive_dir>/derived/<doc_id>/<doc_id>.pdf``
    behind; retraction has to take the whole per-document tree, not just
    the stream text."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    derived = program_root / "archive" / "derived" / doc_id
    derived.mkdir(parents=True, exist_ok=True)
    (derived / f"{doc_id}.pdf").write_bytes(b"%PDF-1.4 derived\n")

    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")

    assert not derived.exists()


def test_retract_writes_a_document_retracted_event_with_the_reason_and_counts(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)

    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="ingested through a fake backend")

    row = store.ops.execute(
        "SELECT * FROM event WHERE type = 'document_retracted' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["launch_id"] == launch_id
    payload = json.loads(row["payload"])
    assert payload["doc_id"] == doc_id
    assert payload["reason"] == "ingested through a fake backend"
    assert payload["removed"]["chunks"] >= 1


def test_retract_writes_a_retraction_record_readable_by_doc_id(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)

    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="why not")

    rows = store.knowledge.execute(
        "SELECT * FROM record WHERE register_key = ?", (RETRACTION_REGISTER_KEY,)
    ).fetchall()
    assert len(rows) == 1
    assert retracted_doc_ids(store.knowledge) == {doc_id}
    assert is_retracted(store.knowledge, doc_id)
    record = retraction_record(store.knowledge, doc_id)
    assert record["reason"] == "why not"
    assert record["launch_id"] == launch_id
    assert record["ts"]


# ---------------------------------------------------------------------------
# refusals and guards
# ---------------------------------------------------------------------------


def test_retract_refuses_an_unknown_launch_id(store, program_root):
    """The XID guard every other write verb applies. An unattributable
    corpus deletion is exactly the change that must be impossible."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)

    with pytest.raises(XidTargetMissingError):
        retract_document(store, doc_id=doc_id, launch_id="LNCH-nope", reason="x")

    assert _counts(store, doc_id)["chunks"] >= 1, "a refused retraction must remove nothing"


def test_retract_refuses_an_unknown_document(store, program_root):
    launch_id = bootstrap_launch(store)
    with pytest.raises(DocumentNotFoundError):
        retract_document(store, doc_id="DOC-nope", launch_id=launch_id, reason="x")


def test_the_cli_requires_a_launch_id_and_a_reason():
    import argparse

    parser = argparse.ArgumentParser()
    cli_ingest.register(parser.add_subparsers(dest="group"))
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest", "retract", "--doc-id", "DOC-1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest", "retract", "--doc-id", "DOC-1", "--launch-id", "L"])


def test_retract_is_refused_while_claims_are_anchored_in_the_document(store, program_root):
    """Extraction output outlives the document's own derived rows, and
    destroying it to satisfy a cleanup command is not this verb's call."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    anchor_id = store.knowledge.execute(
        "SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (doc_id,)
    ).fetchone()["anchor_id"]
    insert(
        store, "claim",
        {
            "claim_id": new_id("CLM"), "text": "an accepted finding", "kind": "finding",
            "anchor_id": anchor_id, "created_at": now(), "valid_at": now(),
            "created_by_launch": launch_id,
        },
    )

    with pytest.raises(RetractBlockedError) as excinfo:
        retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")
    assert "1 claim" in str(excinfo.value)
    assert _counts(store, doc_id)["chunks"] >= 1
    assert not is_retracted(store.knowledge, doc_id)


def test_retract_keeps_an_emb_row_another_document_still_uses(store, program_root):
    """``emb`` is hash-addressed so identical text costs one vector across
    documents. Retracting one sharer must not blind the other -- that would
    surface later as an embedding_missing warning and a silent recall hole."""
    launch_id = bootstrap_launch(store)
    doc_a, source_id, _ = _ingest(store, program_root, launch_id=launch_id, name="a.md", writer=write_markdown_fixture)
    doc_b, _, _ = _ingest(
        store, program_root, launch_id=launch_id, name="b.md", writer=write_markdown_fixture,
        source_id=source_id,
    )
    shared = {
        r["sha256"] for r in store.knowledge.execute("SELECT sha256 FROM chunk WHERE doc_id=?", (doc_a,))
    } & {r["sha256"] for r in store.knowledge.execute("SELECT sha256 FROM chunk WHERE doc_id=?", (doc_b,))}
    assert shared, "the two identical markdown fixtures should share chunk text"

    result = retract_document(store, doc_id=doc_a, launch_id=launch_id, reason="cleanup")

    assert result["removed"]["embeddings"] == 0, "every sha is still in use by the other document"
    for sha in shared:
        assert store.knowledge.execute(
            "SELECT COUNT(*) FROM emb WHERE chunk_sha256=?", (sha,)
        ).fetchone()[0] == 1
    assert _counts(store, doc_b)["chunks"] >= 1


# ---------------------------------------------------------------------------
# idempotency and re-ingest
# ---------------------------------------------------------------------------


def test_a_second_retract_reports_nothing_to_remove(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    first = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")
    assert first["removed"]["chunks"] >= 1

    second = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup again")

    assert second["already_retracted"] is True
    assert set(second["removed"].values()) == {0}
    assert second["files_removed"] == []
    assert (
        store.knowledge.execute(
            "SELECT COUNT(*) FROM record WHERE register_key = ?", (RETRACTION_REGISTER_KEY,)
        ).fetchone()[0]
        == 1
    ), "a repeat must not stack retraction records"


def test_adding_the_same_raw_file_again_creates_a_new_document(store, program_root):
    """The point of keeping the row: a retraction is not an erasure, so the
    re-ingest is a NEW document and the old one stays withdrawn."""
    launch_id = bootstrap_launch(store)
    doc_id, source_id, raw_path = _ingest(store, program_root, launch_id=launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")

    again = pipeline.add_document(
        store, program_root=program_root, source_id=source_id, raw_path=raw_path,
        created_by_launch=launch_id,
    )
    new_doc_id = again["document"]["doc_id"]
    _drain(store)

    assert new_doc_id != doc_id
    assert _counts(store, new_doc_id)["chunks"] >= 1
    assert is_retracted(store.knowledge, doc_id)
    assert not is_retracted(store.knowledge, new_doc_id)
    assert _counts(store, doc_id) == {"elements": 0, "chunks": 0, "anchors": 0}


# ---------------------------------------------------------------------------
# doctor and dashboard
# ---------------------------------------------------------------------------


def test_anchors_dangling_stays_green_after_a_retract(store, program_root):
    """The check the retraction could most plausibly have broken: deleting
    a document's elements while leaving its anchors would make every anchor
    dangle. Both halves of the aggregate must stay clean."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")
    store.knowledge.commit()
    store.close()

    env = cli_ingest._cmd_doctor(_Args(program_root=str(program_root), platform_root=None))
    assert env["ok"] is True, env
    assert env["result"]["anchors_dangling_total"] == 0
    by_name = {c["name"]: c for c in env["result"]["checks"]}
    assert by_name["anchors_dangling"]["status"] == "pass", by_name["anchors_dangling"]
    assert by_name["anchor_spot_resolve"]["status"] == "pass"


def test_the_chunker_checks_do_not_count_a_retracted_document(store, program_root):
    from trialerror.ingest.checks import check_chunker_missing, check_chunker_outdated

    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    missing = check_chunker_missing(ctx)
    assert missing.status == "pass"
    assert doc_id not in missing.details["doc_ids"]
    assert check_chunker_outdated(ctx).status == "pass"


def test_chunker_missing_ignores_a_partially_retracted_document(store, program_root):
    """The case the happy path cannot reach: elements survived, chunks did
    not. Without the explicit exclusion the doctor would keep proposing a
    rechunk of a document the operator deliberately withdrew."""
    from trialerror.ingest.checks import check_chunker_missing

    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")
    # simulate the half-finished shape (an older retraction, or a crash)
    with store.knowledge:
        store.knowledge.execute(
            "INSERT INTO element (element_id, doc_id, seq, type, text) VALUES (?,?,?,?,?)",
            (new_id("ELM"), doc_id, 0, "NarrativeText", "left behind"),
        )
    store.knowledge.commit()

    result = check_chunker_missing(DoctorContext(program_root=program_root))
    assert result.status == "pass", result.details


def test_fake_backend_rows_does_not_count_a_retracted_document(store, program_root):
    """The exact live scenario: the document was retracted BECAUSE it went
    through the fake backend. The check must stop reporting it."""
    from trialerror.offload.checks import check_fake_backend_rows
    from trialerror.stores.writer import update

    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"ocr_backend": "fake"})
    store.knowledge.commit()

    ctx = DoctorContext(program_root=program_root)
    assert check_fake_backend_rows(ctx).details["fake_ocr_documents"] == 1

    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="fake OCR")
    store.knowledge.commit()

    after = check_fake_backend_rows(ctx)
    # Nothing fake is left in the record at all -- the document's own
    # fake-model emb rows went with it -- so the check reports its clean
    # "pass" shape, which carries no per-category counts.
    assert after.status == "pass", after.message
    assert after.details.get("fake_ocr_documents", 0) == 0


def test_the_dashboard_corpus_panel_excludes_a_retracted_document(store, program_root, platform_root):
    from trialerror.dashboard.data import build_corpus_panel
    from trialerror.dashboard.store_ro import open_store_ro

    launch_id = bootstrap_launch(store)
    doc_a, source_id, _ = _ingest(store, program_root, launch_id=launch_id, name="a.html")
    doc_b, _, _ = _ingest(store, program_root, launch_id=launch_id, name="b.html", source_id=source_id)
    retract_document(store, doc_id=doc_a, launch_id=launch_id, reason="cleanup")
    store.knowledge.commit()
    store.close()

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = build_corpus_panel(rostore)
    finally:
        rostore.close()

    assert panel["counts"]["documents"] == 1
    assert panel["counts"]["retracted_documents"] == 1
    assert panel["document_status_counts"].get("retracted") == 1
    assert panel["document_status_counts"].get("indexed") == 1


# ---------------------------------------------------------------------------
# the full-text index
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_retract_removes_the_documents_chunks_from_the_tantivy_index(store, program_root):
    launch_id = bootstrap_launch(store)
    doc_a, source_id, _ = _ingest(store, program_root, launch_id=launch_id, name="a.html")
    doc_b, _, _ = _ingest(store, program_root, launch_id=launch_id, name="b.html", source_id=source_id)
    index_dir = store_paths.fulltext_index_path(program_root, {})
    doomed = [
        r["chunk_id"] for r in store.knowledge.execute("SELECT chunk_id FROM chunk WHERE doc_id=?", (doc_a,))
    ]
    kept = [
        r["chunk_id"] for r in store.knowledge.execute("SELECT chunk_id FROM chunk WHERE doc_id=?", (doc_b,))
    ]
    index = tantivysearch.open_fulltext_index(index_dir)
    assert index is not None, "the index stage should have built one"
    assert index.existing_chunk_ids(doomed) == set(doomed)

    result = retract_document(store, doc_id=doc_a, launch_id=launch_id, reason="cleanup")

    assert result["fulltext"]["action"] == "remove"
    assert result["fulltext"]["removed"] == len(doomed)
    reopened = tantivysearch.open_fulltext_index(index_dir)
    assert reopened.existing_chunk_ids(doomed) == set()
    assert reopened.existing_chunk_ids(kept) == set(kept)


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_the_fulltext_index_is_not_stale_after_a_retract(store, program_root):
    """The removal has to fold the ids back OUT of the sidecar fingerprint,
    or the doctor would report the index stale after every retraction and
    an operator would learn to ignore it."""
    from trialerror.retrieve.checks import check_fulltext_index_stale

    launch_id = bootstrap_launch(store)
    doc_a, source_id, _ = _ingest(store, program_root, launch_id=launch_id, name="a.html")
    _ingest(store, program_root, launch_id=launch_id, name="b.html", source_id=source_id)
    retract_document(store, doc_id=doc_a, launch_id=launch_id, reason="cleanup")
    store.knowledge.commit()

    assert check_fulltext_index_stale(DoctorContext(program_root=program_root)).status == "pass"


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_remove_chunks_is_idempotent(tmp_path):
    index_dir = tmp_path / "idx"
    tantivysearch.add_chunks(index_dir, [("CHK-1", "alpha beta"), ("CHK-2", "gamma delta")])
    first = tantivysearch.remove_chunks(index_dir, ["CHK-1"])
    assert first == {"removed": 1, "absent": 0, "chunk_count": 1}
    second = tantivysearch.remove_chunks(index_dir, ["CHK-1"])
    assert second == {"removed": 0, "absent": 1, "chunk_count": 1}
    meta_after = tantivysearch.read_meta(index_dir)
    assert meta_after["chunk_fingerprint"] == tantivysearch.chunk_fingerprint(["CHK-2"])


@pytest.mark.skipif(not tantivysearch.tantivy_available(), reason="tantivy-py not installed")
def test_remove_chunks_on_an_absent_index_is_a_no_op(tmp_path):
    assert tantivysearch.remove_chunks(tmp_path / "nothing-here", ["CHK-1"])["index"] == "absent"


def test_prune_index_is_a_no_op_without_chunk_ids(store, program_root):
    from trialerror.retrieve import lexical

    assert lexical.prune_index(store, [])["action"] == "skip"


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------


def test_cmd_retract_end_to_end(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    store.knowledge.commit()
    store.close()

    args = _Args(
        program_root=str(program_root), platform_root=str(platform_root),
        doc_id=doc_id, launch_id=launch_id, reason="ingested through a fake OCR stand-in",
    )
    env = cli_ingest._cmd_retract(args)

    assert env["ok"] is True, env
    assert env["result"]["doc_id"] == doc_id
    assert env["result"]["removed"]["chunks"] >= 1

    status_env = cli_ingest._cmd_status(
        _Args(program_root=str(program_root), platform_root=str(platform_root), doc_id=doc_id)
    )
    assert status_env["result"]["retracted"] is True
    assert status_env["result"]["retraction"]["reason"] == "ingested through a fake OCR stand-in"
    assert status_env["result"]["counts"] == {"elements": 0, "chunks": 0, "anchors": 0}


def test_cmd_retract_reports_an_unknown_document_as_an_envelope_not_a_traceback(
    store, program_root, platform_root
):
    launch_id = bootstrap_launch(store)
    store.close()
    env = cli_ingest._cmd_retract(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            doc_id="DOC-nope", launch_id=launch_id, reason="x",
        )
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "DocumentNotFoundError"


def test_cmd_retract_reports_a_blocked_retraction_as_its_own_code(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    anchor_id = store.knowledge.execute(
        "SELECT anchor_id FROM quote_anchor WHERE doc_id = ? LIMIT 1", (doc_id,)
    ).fetchone()["anchor_id"]
    insert(
        store, "claim",
        {
            "claim_id": new_id("CLM"), "text": "a finding", "kind": "finding", "anchor_id": anchor_id,
            "created_at": now(), "valid_at": now(), "created_by_launch": launch_id,
        },
    )
    store.knowledge.commit()
    store.close()

    env = cli_ingest._cmd_retract(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            doc_id=doc_id, launch_id=launch_id, reason="cleanup",
        )
    )
    assert env["ok"] is False
    assert env["error"]["code"] == "retract_blocked"


def test_retract_is_registered_as_an_ingest_subcommand():
    import argparse

    parser = argparse.ArgumentParser()
    cli_ingest.register(parser.add_subparsers(dest="group"))
    args = parser.parse_args(
        ["ingest", "retract", "--doc-id", "DOC-1", "--launch-id", "LNCH-1", "--reason", "because"]
    )
    assert args.handler is cli_ingest._cmd_retract
    assert args.reason == "because"


def test_retract_handles_nested_elements_whose_parent_is_in_the_same_document(store, program_root):
    """``element.parent_element`` is a SELF-FK: a document with nested
    elements (structured HTML, EPUB chapters) has parent and child in the
    same DELETE. SQLite happens to tolerate that within a single statement,
    so this is not what forced ``defer_foreign_keys`` -- but it IS the
    shape most likely to get retracted, and "we believe SQLite is fine with
    this" deserves an executable assertion rather than a belief."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    rows = [
        dict(r)
        for r in store.knowledge.execute("SELECT * FROM element WHERE doc_id=? ORDER BY seq", (doc_id,))
    ]
    assert len(rows) >= 2
    with store.knowledge:
        store.knowledge.execute(
            "UPDATE element SET parent_element = ? WHERE element_id = ?",
            (rows[0]["element_id"], rows[1]["element_id"]),
        )

    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")

    assert result["removed"]["elements"] == len(rows)
    assert _counts(store, doc_id)["elements"] == 0


# ---------------------------------------------------------------------------
# VERIFY V-2: more chunks than one statement can bind
# ---------------------------------------------------------------------------


def test_delete_by_chunk_ids_batches_past_the_sql_variable_ceiling():
    """One placeholder per chunk_id walks into SQLITE_LIMIT_VARIABLE_NUMBER
    (32,766 on a current build, 999 on an old one). The resulting
    ``sqlite3.OperationalError`` is not an ``IngestError`` or a
    ``StoreError``, so it escaped ``_cmd_retract``'s handlers and the CLI
    answered a TRACEBACK instead of an error envelope -- on the one verb
    that exists to clean up an ingest which went wrong at scale.

    The single-statement probe first, so this test cannot pass vacuously:
    it has to be measuring a ceiling that is really there."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chunk_fts (chunk_id TEXT)")
    ids = [f"CHK-{i:06d}" for i in range(40_000)]
    conn.executemany("INSERT INTO chunk_fts (chunk_id) VALUES (?)", [(c,) for c in ids])

    one_shot = "SELECT COUNT(*) FROM chunk_fts WHERE chunk_id IN (%s)" % ",".join("?" for _ in ids)
    try:
        conn.execute(one_shot, ids)
    except sqlite3.OperationalError as exc:
        assert "too many SQL variables" in str(exc)
    else:  # pragma: no cover - only on a build with a raised limit
        pytest.fail(
            "this SQLite build bound 40,000 variables in one statement; raise the count in this "
            "test so it keeps measuring the ceiling _delete_by_chunk_ids exists for"
        )

    assert _delete_by_chunk_ids(conn, "chunk_fts", ids) == 40_000
    assert conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0] == 0


def test_retract_counts_are_exact_when_the_chunk_ids_span_several_batches(
    store, program_root, monkeypatch
):
    """The batching wired into ``retract_document`` itself, not just the
    helper: with the batch size forced below the document's chunk count,
    every count still matches the measured deltas and every row still
    goes. Cheaper than ingesting 33,000 chunks, and it exercises the same
    loop -- including the rowcount summing, which is where a batched
    delete usually starts under-reporting."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    before = _counts(store, doc_id)
    assert before["chunks"] >= 2, "fixture must have more chunks than the forced batch size"

    monkeypatch.setattr("trialerror.ingest.retract._CHUNK_ID_BATCH", 1)
    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup at scale")

    assert result["removed"]["fts_rows"] == before["chunks"]
    assert result["removed"]["chunks"] == before["chunks"]
    assert _counts(store, doc_id) == {"elements": 0, "chunks": 0, "anchors": 0}
    assert (
        store.knowledge.execute("SELECT COUNT(*) FROM chunk_fts WHERE chunk_id LIKE ?", (f"%{doc_id}%",))
        .fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# VERIFY V-7: the row that dangles by design says so
# ---------------------------------------------------------------------------


def test_retract_flags_the_removed_path_the_surviving_row_points_at(store, program_root):
    """Both DjVu routes rewrite ``document.raw_path`` to the DERIVED pdf,
    which lives in the tree retraction removes -- so the surviving row
    references a file that is gone. That is deliberate (no anchors survive
    to check a citation against, and the operator's own source file is
    untouched and re-ingestable), but a reader of the ``document_retracted``
    event should not have to deduce it."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)

    derived = program_root / "archive" / "derived" / doc_id
    derived.mkdir(parents=True, exist_ok=True)
    derived_pdf = derived / f"{doc_id}.pdf"
    derived_pdf.write_bytes(b"%PDF-1.4 derived")
    with store.knowledge:
        store.knowledge.execute(
            "UPDATE document SET raw_path = ? WHERE doc_id = ?",
            (derived_pdf.relative_to(program_root).as_posix(), doc_id),
        )

    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")

    assert result["raw_path_removed"] == f"archive/derived/{doc_id}"
    assert result["raw_path_removed"] in result["files_removed"]
    assert not derived_pdf.exists()

    event = json.loads(
        store.ops.execute(
            "SELECT payload FROM event WHERE type = 'document_retracted' ORDER BY ts DESC LIMIT 1"
        ).fetchone()["payload"]
    )
    assert event["raw_path_removed"] == f"archive/derived/{doc_id}"


def test_retract_reports_no_dangling_raw_path_when_the_raw_file_is_the_operators(
    store, program_root
):
    """The ordinary case: ``raw_path`` is the operator's own input, which
    retraction never touches, so nothing dangles and the field says so."""
    launch_id = bootstrap_launch(store)
    doc_id, _, raw_path = _ingest(store, program_root, launch_id=launch_id)

    result = retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="cleanup")

    assert result["raw_path_removed"] is None
    assert raw_path.is_file()


# ---------------------------------------------------------------------------
# VERIFY V-3: the re-derive verbs cannot un-retract
#
# Every consumer of the retraction register READS it (doctor checks, the
# dashboard panel, ingest status). The writers did not: rechunk and
# re-embed both returned ok on a retracted document and, once drained,
# rewrote document.status from the retracted marker back to 'embedded' --
# a row reading retracted and embedded at once. A normalize requeue went
# further and re-derived elements from the raw file retraction keeps.
#
# Contained today only because the REGISTER, not the status column, is
# authoritative. It stops being contained the moment retracted_doc_ids()
# reads the column -- the disclosed follow-up to D2-1 -- at which point
# `ingest rechunk` becomes a working un-retract. So this guard is a
# precondition of that migration, not a follow-up to it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["chunk", "embed", "normalize"])
def test_requeue_stage_refuses_a_retracted_document(store, program_root, kind):
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="fake OCR stand-in")
    status_before = store.knowledge.execute(
        "SELECT status FROM document WHERE doc_id=?", (doc_id,)
    ).fetchone()[0]

    with pytest.raises(DocumentRetractedError) as excinfo:
        pipeline.requeue_stage(store, doc_id=doc_id, kind=kind, created_by_launch=launch_id)

    assert doc_id in str(excinfo.value)
    assert "ingest add" in str(excinfo.value), "the refusal must name the supported way back"
    _drain(store)
    assert _counts(store, doc_id) == {"elements": 0, "chunks": 0, "anchors": 0}
    assert (
        store.knowledge.execute("SELECT status FROM document WHERE doc_id=?", (doc_id,)).fetchone()[0]
        == status_before
    )
    assert is_retracted(store.knowledge, doc_id)


def test_requeue_stage_still_works_on_a_live_document(store, program_root):
    """The guard must not fire on the documents the verb exists for --
    ``rechunk``/``re-embed`` are the documented repair for four of the
    doctor's counts."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)

    job = pipeline.requeue_stage(store, doc_id=doc_id, kind="chunk", created_by_launch=launch_id)

    assert job["kind"] == "chunk"
    assert json.loads(job["payload"])["doc_id"] == doc_id


@pytest.mark.parametrize(
    "handler_name", ["_cmd_rechunk", "_cmd_reembed"]
)
def test_cmd_rechunk_and_reembed_answer_an_envelope_on_a_retracted_document(
    store, program_root, platform_root, handler_name
):
    """A structured refusal, not a traceback: the CLI's cross-cutting rule
    is that errors are content."""
    launch_id = bootstrap_launch(store)
    doc_id, _, _ = _ingest(store, program_root, launch_id=launch_id)
    retract_document(store, doc_id=doc_id, launch_id=launch_id, reason="fake OCR stand-in")
    store.knowledge.commit()
    store.close()

    env = getattr(cli_ingest, handler_name)(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            doc_id=doc_id, launch_id=launch_id,
        )
    )

    assert env["ok"] is False, env
    assert env["error"]["code"] == "DocumentRetractedError"
    assert doc_id in env["error"]["message"]


# ---------------------------------------------------------------------------
# VERIFY V-5: one answer to "how big is the corpus"
# ---------------------------------------------------------------------------


def test_corpus_stats_excludes_retracted_documents_like_the_dashboard_does(
    store, program_root
):
    """``trialerror query stats`` and the knowledge MCP server both read
    ``engine.corpus_stats``. It counted every ``document`` row, so the same
    operator question got a different answer depending on whether it was
    asked through the dashboard or the CLI."""
    from trialerror.retrieve import engine

    launch_id = bootstrap_launch(store)
    doc_a, source_id, _ = _ingest(store, program_root, launch_id=launch_id, name="a.html")
    doc_b, _, _ = _ingest(store, program_root, launch_id=launch_id, name="b.html", source_id=source_id)

    before = engine.corpus_stats(store)
    assert before["documents"] == 2
    assert before["retracted_documents"] == 0

    retract_document(store, doc_id=doc_a, launch_id=launch_id, reason="fake OCR stand-in")

    after = engine.corpus_stats(store)
    assert after["documents"] == 1, "the retracted document is out of the live corpus"
    assert after["retracted_documents"] == 1, "and is still reported, not silently dropped"
    assert after["sources"] == before["sources"], "the source row is untouched by a retraction"

    # The number an operator sees must not depend on which surface asked.
    from trialerror.dashboard.data import build_corpus_panel
    from trialerror.dashboard.store_ro import open_store_ro

    store.knowledge.commit()
    panel = build_corpus_panel(open_store_ro(program_root))
    assert panel["counts"]["documents"] == after["documents"]
    assert panel["counts"]["retracted_documents"] == after["retracted_documents"]
