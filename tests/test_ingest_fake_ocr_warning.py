"""Lane FB-1 item F10a: a fake OCR backend is said out loud at add time.

The fake OCR backend produces plausible-looking text that came from nowhere.
A program that meant to configure a real one and mistyped the table name used
to learn this months later, from search quality. `ingest add` and `lit
acquire` now say it in the envelope -- and only in the envelope.
"""

from __future__ import annotations

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import pipeline
from trialerror.ingest.backends import (
    FAKE_STAGE_BACKEND_WARNING_CODE,
    fake_stage_backend_warning,
    resolve_stage_backend,
)

from tests._ingest_fixtures import bootstrap_launch, write_html_fixture, write_scanned_pdf_fixture


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        self.media_type = None
        self.yes = False
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# resolution + the warning body
# ---------------------------------------------------------------------------


def test_an_absent_table_reads_as_the_fake_backend():
    resolved = resolve_stage_backend({"ingest": {}}, "ocr")
    assert resolved == {
        "stage": "ocr",
        "backend": "fake",
        "table_absent": True,
        "fake": True,
        "require_real": False,
        "require_real_key": "[ingest] require_real_backends",
    }


def test_a_real_backend_warns_about_nothing():
    resolved = resolve_stage_backend({"ingest": {"ocr": {"backend": "offload"}}}, "ocr")
    assert resolved["fake"] is False
    assert fake_stage_backend_warning(resolved) is None


def test_the_warning_names_both_keys_and_the_table():
    warning = fake_stage_backend_warning(resolve_stage_backend({"ingest": {"ocr": {"backend": "fake"}}}, "ocr"))
    assert warning["code"] == FAKE_STAGE_BACKEND_WARNING_CODE
    assert warning["config_table"] == "[ingest.ocr]"
    assert "require_real_backends" in warning["message"]
    assert "[ingest.ocr]" in warning["message"]
    assert "require_real" in warning["message"]


def test_an_absent_table_says_so_rather_than_quoting_a_backend_nobody_wrote():
    warning = fake_stage_backend_warning(resolve_stage_backend({}, "ocr"))
    assert "is absent from trialerror.toml" in warning["message"]


def test_nothing_is_warned_when_there_is_no_stage_backend_at_all():
    assert fake_stage_backend_warning(None) is None
    assert fake_stage_backend_warning({}) is None


# ---------------------------------------------------------------------------
# add_document returns it, for an OCR stage only
# ---------------------------------------------------------------------------


def _source(store, launch_id):
    return pipeline.register_source(
        store, kind="paper", title="T", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )["source_id"]


def test_add_document_reports_the_stage_backend_for_an_ocr_route(store, program_root):
    launch_id = bootstrap_launch(store)
    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    result = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=_source(store, launch_id),
        raw_path=write_scanned_pdf_fixture(raw / "scan.pdf"),
        created_by_launch=launch_id,
        media_type="pdf-scan",
    )
    assert result["job"]["kind"] == "ocr"
    assert result["stage_backend"]["fake"] is True


def test_a_directly_normalizable_document_reports_no_stage_backend(store, program_root):
    launch_id = bootstrap_launch(store)
    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    result = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=_source(store, launch_id),
        raw_path=write_html_fixture(raw / "doc.html"),
        created_by_launch=launch_id,
    )
    assert result["job"]["kind"] == "normalize"
    assert "stage_backend" not in result


def test_a_real_backend_in_config_is_reported_as_real(store, program_root):
    launch_id = bootstrap_launch(store)
    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    result = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=_source(store, launch_id),
        raw_path=write_scanned_pdf_fixture(raw / "scan.pdf"),
        created_by_launch=launch_id,
        media_type="pdf-scan",
        config={"ingest": {"ocr": {"backend": "offload"}}},
    )
    assert result["stage_backend"]["fake"] is False
    assert fake_stage_backend_warning(result["stage_backend"]) is None


# ---------------------------------------------------------------------------
# the envelope, and nothing on stderr
# ---------------------------------------------------------------------------


def test_ingest_add_carries_the_warning_in_the_envelope_only(
    store, program_root, platform_root, capsys
):
    launch_id = bootstrap_launch(store)
    source_id = _source(store, launch_id)
    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    scan = write_scanned_pdf_fixture(raw / "scan.pdf")
    store.close()

    capsys.readouterr()  # discard anything the fixtures printed
    env = cli_ingest._cmd_add(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            source_id=source_id, path=str(scan), launch_id=launch_id, media_type="pdf-scan",
        )
    )
    captured = capsys.readouterr()

    assert env["ok"] is True
    assert len(env["warnings"]) == 1
    assert env["warnings"][0]["code"] == FAKE_STAGE_BACKEND_WARNING_CODE
    assert captured.err == ""
    assert captured.out == ""


def test_ingest_add_emits_no_warnings_key_at_all_on_a_real_backend(
    store, program_root, platform_root
):
    launch_id = bootstrap_launch(store)
    source_id = _source(store, launch_id)
    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    scan = write_scanned_pdf_fixture(raw / "scan.pdf")
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "p"\n\n[ingest.ocr]\nbackend = "offload"\n', encoding="utf-8"
    )
    store.close()

    env = cli_ingest._cmd_add(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            source_id=source_id, path=str(scan), launch_id=launch_id, media_type="pdf-scan",
        )
    )
    assert "warnings" not in env  # strictly additive


def test_a_normalize_route_adds_no_warning(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    source_id = _source(store, launch_id)
    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    html = write_html_fixture(raw / "doc.html")
    store.close()

    env = cli_ingest._cmd_add(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            source_id=source_id, path=str(html), launch_id=launch_id,
        )
    )
    assert "warnings" not in env


def test_lit_acquire_mirrors_the_same_warning(monkeypatch, tmp_path):
    from trialerror.cli import lit as cli_lit
    from trialerror.ingest.acquire import AcquireResult

    class _FakeStore:
        program_root = None

        def close(self):
            self.closed = True

    fake_store = _FakeStore()
    monkeypatch.setattr("trialerror.stores.store.open_store", lambda *a, **kw: fake_store)
    result = AcquireResult(
        outcome="acquired",
        source={"source_id": "SRC-1"},
        document={"doc_id": "DOC-1"},
        job={"job_id": "JOB-1", "kind": "ocr", "payload": "{}"},
        stage_backend=resolve_stage_backend({"ingest": {"ocr": {"backend": "fake"}}}, "ocr"),
    )
    monkeypatch.setattr("trialerror.ingest.acquire.acquire", lambda *a, **kw: result)

    class _LitArgs:
        program_root = None
        platform_root = None
        doi = "10.1/x"
        arxiv_id = None
        launch_id = "LNCH-1"
        yes = False

    args = _LitArgs()
    args.program_root = str(tmp_path)
    env = cli_lit._cmd_acquire(args)

    assert env["ok"] is True
    assert env["warnings"][0]["code"] == FAKE_STAGE_BACKEND_WARNING_CODE
    assert env["result"]["stage_backend"]["fake"] is True
