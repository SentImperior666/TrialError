"""Lane FB-6 item 7: ``ingest add`` validates the route before it inserts.

Observed: ``--media-type text/markdown`` -- a real IANA media type, and not
one of this pipeline's route keys -- was refused AFTER the ``document`` row
was written, leaving a ``registered`` document with no job behind it. Nothing
would ever move it: doctor counted it, ``ingest status`` listed it, and the
only repair was a hand-written DELETE.

The route is resolved first now, and the refusal says what the keys are and
that they are this pipeline's own names rather than media types.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import pipeline
from trialerror.ingest.errors import UnsupportedMediaTypeError
from trialerror.jobs import ledger
from trialerror.stores.store import open_store

from tests._ingest_fixtures import bootstrap_launch, write_html_fixture


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture()
def registered_source(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="Route Fixture", license_tier="open",
        acquisition_route="web", registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = write_html_fixture(raw_dir / "doc.html")
    return {"launch_id": launch_id, "source_id": source["source_id"], "path": path}


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------


def test_the_route_keys_are_computed_from_the_dispatch_they_describe():
    assert pipeline.route_keys() == ["djvu", "epub", "html", "image", "md", "pdf-scan", "pdf-text"]
    assert pipeline.resolve_stage("md") == "normalize"
    assert pipeline.resolve_stage("pdf-text") == "normalize"
    assert pipeline.resolve_stage("pdf-scan") == "ocr"
    assert pipeline.resolve_stage("image") == "ocr"
    assert pipeline.resolve_stage("djvu") == "djvu"


def test_a_media_type_that_is_not_a_route_key_names_the_keys_and_the_extension_route():
    with pytest.raises(UnsupportedMediaTypeError) as exc:
        pipeline.resolve_stage("text/markdown")
    message = str(exc.value)
    assert "text/markdown" in message
    assert "'md'" in message
    assert "IANA" in message
    assert ".md" in message


# ---------------------------------------------------------------------------
# no orphan row
# ---------------------------------------------------------------------------


def test_a_refused_add_leaves_no_document_row(store, program_root, registered_source):
    before = store.knowledge.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"]
    with pytest.raises(UnsupportedMediaTypeError):
        pipeline.add_document(
            store,
            program_root=program_root,
            source_id=registered_source["source_id"],
            raw_path=registered_source["path"],
            created_by_launch=registered_source["launch_id"],
            media_type="text/markdown",
        )
    after = store.knowledge.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"]
    assert after == before == 0
    assert ledger.list_jobs(store) == [] or all(
        not j["job_id"].startswith("JOB-ingest-") for j in ledger.list_jobs(store)
    )


def test_the_cli_refuses_by_name_and_leaves_nothing_behind(
    store, program_root, platform_root, registered_source
):
    args = _Args(
        program_root=str(program_root), platform_root=str(platform_root),
        source_id=registered_source["source_id"], path=str(registered_source["path"]),
        media_type="text/markdown", launch_id=registered_source["launch_id"], yes=False,
    )
    store.close()
    env = cli_ingest._cmd_add(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "UnsupportedMediaTypeError"
    assert "text/markdown" in env["error"]["message"]

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        assert reopened.knowledge.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
    finally:
        reopened.close()


def test_an_accepted_route_still_writes_the_row_and_the_job(store, program_root, registered_source):
    result = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=registered_source["source_id"],
        raw_path=registered_source["path"],
        created_by_launch=registered_source["launch_id"],
    )
    assert result["document"]["media_type"] == "html"
    assert result["job"]["job_id"] == f"JOB-ingest-{result['document']['doc_id']}"
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1


def test_the_cost_gate_still_fires_before_the_row_too(store, program_root, registered_source):
    """The route check went in FRONT of the cost gate, which already refused
    before the insert -- neither may start writing."""
    with pytest.raises(ValueError):
        pipeline.add_document(
            store,
            program_root=program_root,
            source_id=registered_source["source_id"],
            raw_path=registered_source["path"],
            created_by_launch=registered_source["launch_id"],
            config={"ingest": {"cost_gate_page_threshold": -1}},
        )
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 0
