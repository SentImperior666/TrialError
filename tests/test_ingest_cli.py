"""Tests for the ``trialerror ingest`` CLI group (``trialerror/cli/ingest.py``)."""

from __future__ import annotations

import json

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.jobs.worker import run_one
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_group_name_and_help_registered():
    assert cli_ingest.GROUP_NAME == "ingest"
    assert cli_ingest.HELP


def test_cmd_add_source_and_add_end_to_end(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    store.close()  # the CLI handler opens its own store from program_root/platform_root

    args = _Args(
        program_root=str(program_root), platform_root=str(platform_root),
        kind="web", title="CLI Source", license_tier="open", acquisition_route="web",
        launch_id=launch_id, content_file=None, authors=None, year=None, url=None,
        rights_notes=None, request_state="delivered",
    )
    env = cli_ingest._cmd_add_source(args)
    assert env["ok"] is True
    source_id = env["result"]["source"]["source_id"]
    assert env["result"]["deduped"] is False

    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(raw_dir / "doc.html")

    add_args = _Args(
        program_root=str(program_root), platform_root=str(platform_root),
        source_id=source_id, path=str(doc_path), media_type=None, launch_id=launch_id, yes=False,
    )
    env2 = cli_ingest._cmd_add(add_args)
    assert env2["ok"] is True
    doc_id = env2["result"]["document"]["doc_id"]
    job_id = env2["result"]["job"]["job_id"]
    assert job_id.startswith("JOB-ingest-")

    # status command
    status_args = _Args(program_root=str(program_root), platform_root=str(platform_root), doc_id=doc_id)
    status_env = cli_ingest._cmd_status(status_args)
    assert status_env["ok"] is True
    assert status_env["result"]["document"]["doc_id"] == doc_id


def test_cmd_add_source_dedup_reports_true(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    store.close()
    kwargs = dict(
        program_root=str(program_root), platform_root=str(platform_root),
        kind="paper", title="Dup", license_tier="open", acquisition_route="web",
        launch_id=launch_id, content_file=None, authors=None, year=None, url=None,
        rights_notes=None, request_state="delivered",
    )

    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(b"identical content bytes")
        content_path = f.name
    try:
        args1 = _Args(**{**kwargs, "content_file": content_path})
        env1 = cli_ingest._cmd_add_source(args1)
        args2 = _Args(**{**kwargs, "content_file": content_path})
        env2 = cli_ingest._cmd_add_source(args2)
        assert env1["result"]["deduped"] is False
        assert env2["result"]["deduped"] is True
        assert env2["result"]["source"]["source_id"] == env1["result"]["source"]["source_id"]
    finally:
        Path(content_path).unlink(missing_ok=True)


def test_cmd_add_refuses_out_of_tree_path(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    source = None
    from trialerror.ingest import pipeline

    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    store.close()

    outside = program_root.parent / "outside_tree"
    outside.mkdir(parents=True, exist_ok=True)
    stray = write_html_fixture(outside / "manifest.html")

    args = _Args(
        program_root=str(program_root), platform_root=str(platform_root),
        source_id=source["source_id"], path=str(stray), media_type=None, launch_id=launch_id, yes=False,
    )
    env = cli_ingest._cmd_add(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "PathOutOfTreeError"


def test_cmd_doctor_reports_ingest_checks(store, program_root, platform_root):
    store.close()
    args = _Args(program_root=str(program_root), platform_root=str(platform_root))
    env = cli_ingest._cmd_doctor(args)
    assert env["ok"] is True
    names = {c["name"] for c in env["result"]["checks"]}
    assert {"chunker_missing", "chunker_outdated", "embedding_missing", "embedding_stale", "anchors_dangling", "anchor_spot_resolve"} <= names
    assert "anchors_dangling_total" in env["result"]


def test_cmd_request_transitions_and_requests_md(store, program_root, platform_root):
    from trialerror.ingest import pipeline

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="book", title="Wanted", license_tier="unknown", acquisition_route="web",
        registered_by_launch=launch_id, request_state="wanted",
    )
    store.close()

    args = _Args(
        program_root=str(program_root), platform_root=str(platform_root),
        source_id=source["source_id"], to_state="requested", launch_id=launch_id, note="test",
    )
    env = cli_ingest._cmd_request(args)
    assert env["ok"] is True
    assert env["result"]["source"]["request_state"] == "requested"

    md_args = _Args(program_root=str(program_root), platform_root=str(platform_root))
    md_env = cli_ingest._cmd_requests_md(md_args)
    assert md_env["ok"] is True
    assert (program_root / "requests" / "REQUESTS.md").is_file()


def test_cmd_requests_md_respects_configured_requests_path(store, program_root, platform_root):
    """the import-design notes (internal, not in this export) Sec 5 knob #4: the CLI actually loads
    trialerror.toml and threads it through -- not just trialerror.ingest.requests's
    own library-level ``config`` parameter."""
    from trialerror.ingest import pipeline

    launch_id = bootstrap_launch(store)
    pipeline.register_source(
        store, kind="book", title="Wanted", license_tier="unknown", acquisition_route="web",
        registered_by_launch=launch_id, request_state="wanted",
    )
    store.close()

    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "demo"\n\n[paths]\nrequests_path = "acquire/WANTED.md"\n', encoding="utf-8"
    )
    md_args = _Args(program_root=str(program_root), platform_root=str(platform_root))
    md_env = cli_ingest._cmd_requests_md(md_args)
    assert md_env["ok"] is True
    assert md_env["result"]["path"] == str(program_root / "acquire" / "WANTED.md")
    assert (program_root / "acquire" / "WANTED.md").is_file()
    assert not (program_root / "requests").exists()


def test_cmd_rechunk_and_reembed_enqueue_jobs(store, program_root, platform_root):
    from trialerror.ingest import pipeline

    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="web", title="S", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc_path = write_html_fixture(raw_dir / "doc.html")
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source["source_id"], raw_path=doc_path,
        created_by_launch=launch_id,
    )
    doc_id = result["document"]["doc_id"]
    for i in range(10):
        r = run_one(store, worker_id=f"w{i}")
        if r["status"] == "idle":
            break
    store.close()

    rechunk_args = _Args(program_root=str(program_root), platform_root=str(platform_root), doc_id=doc_id, launch_id=launch_id)
    env = cli_ingest._cmd_rechunk(rechunk_args)
    assert env["ok"] is True
    # schema-v2: chunk is a first-class job.kind (no more kind='custom' +
    # payload['handler'] wrapping) -- see pipeline.stage_job_kind_and_payload.
    assert env["result"]["job"]["kind"] == "chunk"
    assert "handler" not in json.loads(env["result"]["job"]["payload"])

    reembed_args = _Args(program_root=str(program_root), platform_root=str(platform_root), doc_id=doc_id, launch_id=launch_id)
    env2 = cli_ingest._cmd_reembed(reembed_args)
    assert env2["ok"] is True
    assert env2["result"]["job"]["kind"] == "embed"


def test_cmd_add_no_program_root_errors(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    args = _Args(
        program_root=None, platform_root=None, source_id="SRC-x", path=str(tmp_path / "x.html"),
        media_type=None, launch_id="LNCH-x", yes=False,
    )
    env = cli_ingest._cmd_add(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_program_root"
