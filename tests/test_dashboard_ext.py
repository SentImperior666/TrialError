"""Tests for ``trialerror.dashboard.ext`` -- the extension-panel protocol
(C-0070: program-root ``trialerror_ext/panels/`` manifests + builders over the
read-only store) -- and ``trialerror.dashboard.checks`` (the ``ext_panels_valid``
doctor check it registers). Covers discovery/manifest validation, builder
success, every crash-isolation path (import error, signature error, a raise
inside ``build_panel``, a non-dict return), and the doctor check's three
statuses.
"""

from __future__ import annotations

import json
from pathlib import Path

from trialerror.dashboard import ext
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from tests._store_fixtures import populate_one_of_everything

_VALID_TOML = '[panel]\ntitle = "T"\nnav_group = "KNOW"\norder = 1\n'
_OK_BUILDER = "def build_panel(rostore, program_root):\n    return {\"status\": \"ok\", \"n\": 1}\n"


def _write_panel(root: Path, name: str, *, toml_text: str | None = _VALID_TOML, builder_text: str | None = _OK_BUILDER) -> Path:
    panel_dir = root / "trialerror_ext" / "panels" / name
    panel_dir.mkdir(parents=True, exist_ok=True)
    if toml_text is not None:
        (panel_dir / "panel.toml").write_text(toml_text, encoding="utf-8")
    if builder_text is not None:
        (panel_dir / "builder.py").write_text(builder_text, encoding="utf-8")
    return panel_dir


# ---------------------------------------------------------------------------
# discovery / manifest validation
# ---------------------------------------------------------------------------
def test_discover_returns_empty_list_for_no_program_root():
    assert ext.discover_ext_panels(None) == []


def test_discover_returns_empty_list_when_directory_absent(tmp_path):
    assert ext.discover_ext_panels(tmp_path) == []


def test_discover_finds_a_valid_panel_sorted_by_order(tmp_path):
    _write_panel(tmp_path, "second", toml_text='[panel]\ntitle = "Second"\nnav_group = "RUN"\norder = 2\n')
    _write_panel(tmp_path, "first", toml_text='[panel]\ntitle = "First"\nnav_group = "RUN"\norder = 1\n')
    entries = ext.discover_ext_panels(tmp_path)
    assert [e.name for e in entries] == ["first", "second"]
    assert entries[0].manifest_status == "ok"
    assert entries[0].manifest.title == "First"
    assert entries[0].manifest.nav_group == "RUN"


def test_discover_ignores_non_directory_entries(tmp_path):
    panels_root = tmp_path / "trialerror_ext" / "panels"
    panels_root.mkdir(parents=True)
    (panels_root / "stray.txt").write_text("not a panel", encoding="utf-8")
    assert ext.discover_ext_panels(tmp_path) == []


def test_manifest_missing_panel_toml(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", toml_text=None)
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest_status == "manifest_error"
    assert "not found" in entry.manifest_error


def test_manifest_invalid_toml_syntax(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", toml_text="not [ valid toml")
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest_status == "manifest_error"
    assert "invalid TOML" in entry.manifest_error


def test_manifest_missing_panel_table(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", toml_text="title = \"T\"\n")
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest_status == "manifest_error"
    assert "[panel]" in entry.manifest_error


def test_manifest_missing_required_field(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", toml_text='[panel]\ntitle = "T"\nnav_group = "KNOW"\n')
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest_status == "manifest_error"
    assert "order" in entry.manifest_error


def test_manifest_bad_nav_group(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", toml_text='[panel]\ntitle = "T"\nnav_group = "WEIRD"\norder = 1\n')
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest_status == "manifest_error"
    assert "nav_group" in entry.manifest_error


def test_manifest_bad_order_type(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", toml_text='[panel]\ntitle = "T"\nnav_group = "KNOW"\norder = "one"\n')
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest_status == "manifest_error"
    assert "order" in entry.manifest_error


def test_manifest_ok_but_builder_missing(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", builder_text=None)
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest_status == "manifest_error"
    assert "builder.py" in entry.manifest_error


def test_manifest_optional_fields_default(tmp_path):
    panel_dir = _write_panel(tmp_path, "p")
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest.description == ""
    assert entry.manifest.min_schema == []


def test_manifest_optional_fields_present(tmp_path):
    toml_text = (
        '[panel]\ntitle = "T"\nnav_group = "KNOW"\norder = 1\n'
        'description = "desc"\nmin_schema = ["record", "chunk"]\n'
    )
    panel_dir = _write_panel(tmp_path, "p", toml_text=toml_text)
    entry = ext.load_ext_panel_entry("p", panel_dir)
    assert entry.manifest.description == "desc"
    assert entry.manifest.min_schema == ["record", "chunk"]


def test_find_ext_panel_entry(tmp_path):
    _write_panel(tmp_path, "p")
    assert ext.find_ext_panel_entry(tmp_path, "p") is not None
    assert ext.find_ext_panel_entry(tmp_path, "nope") is None
    assert ext.find_ext_panel_entry(None, "p") is None


def test_list_ext_panels_shape(tmp_path):
    _write_panel(
        tmp_path, "p",
        toml_text='[panel]\ntitle = "T"\nnav_group = "RUN"\norder = 3\ndescription = "d"\n',
    )
    rows = ext.list_ext_panels(tmp_path)
    assert rows == [
        {
            "name": "p", "manifest_status": "ok", "title": "T",
            "nav_group": "RUN", "order": 3, "description": "d", "min_schema": [],
        }
    ]


def test_list_ext_panels_reports_error_row(tmp_path):
    _write_panel(tmp_path, "broken", toml_text="not [ valid")
    rows = ext.list_ext_panels(tmp_path)
    assert rows[0]["name"] == "broken"
    assert rows[0]["manifest_status"] == "manifest_error"
    assert "error" in rows[0]


# ---------------------------------------------------------------------------
# build_ext_panel -- success + every crash-isolation path
# ---------------------------------------------------------------------------
def _rostore(program_root, platform_root):
    return open_store_ro(program_root, platform_root=platform_root)


def test_build_ext_panel_success(program_root, platform_root):
    _write_panel(program_root, "p")
    entry = ext.find_ext_panel_entry(program_root, "p")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result == {"status": "ok", "n": 1}


def test_build_ext_panel_manifest_error_never_imports(program_root, platform_root):
    _write_panel(program_root, "p", toml_text="not [ valid")
    entry = ext.find_ext_panel_entry(program_root, "p")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result["status"] == "ext_error"
    assert "invalid TOML" in result["message"]


def test_build_ext_panel_import_error(program_root, platform_root):
    _write_panel(program_root, "p", builder_text="this is not ) valid python (\n")
    entry = ext.find_ext_panel_entry(program_root, "p")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result["status"] == "ext_error"
    assert "SyntaxError" in result["message"]


def test_build_ext_panel_missing_build_panel_fn(program_root, platform_root):
    _write_panel(program_root, "p", builder_text="x = 1\n")
    entry = ext.find_ext_panel_entry(program_root, "p")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result["status"] == "ext_error"
    assert "build_panel" in result["message"]


def test_build_ext_panel_wrong_signature(program_root, platform_root):
    _write_panel(program_root, "p", builder_text="def build_panel():\n    return {}\n")
    entry = ext.find_ext_panel_entry(program_root, "p")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result["status"] == "ext_error"
    assert "signature" in result["message"]


def test_build_ext_panel_raises_inside_build_panel(program_root, platform_root):
    _write_panel(
        program_root, "p",
        builder_text="def build_panel(rostore, program_root):\n    raise RuntimeError('boom')\n",
    )
    entry = ext.find_ext_panel_entry(program_root, "p")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result["status"] == "ext_error"
    assert "boom" in result["message"]


def test_build_ext_panel_returns_non_dict(program_root, platform_root):
    _write_panel(
        program_root, "p",
        builder_text="def build_panel(rostore, program_root):\n    return 'not a dict'\n",
    )
    entry = ext.find_ext_panel_entry(program_root, "p")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result["status"] == "ext_error"
    assert "dict" in result["message"]


def test_build_ext_panel_can_read_the_real_store(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    populate_one_of_everything(store)
    store.close()

    _write_panel(
        program_root, "jobcount",
        builder_text=(
            "def build_panel(rostore, program_root):\n"
            "    n = rostore.jobs.execute('SELECT COUNT(*) FROM job').fetchone()[0]\n"
            "    return {'status': 'ok', 'job_count': n}\n"
        ),
    )
    entry = ext.find_ext_panel_entry(program_root, "jobcount")
    rostore = _rostore(program_root, platform_root)
    try:
        result = ext.build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()
    assert result == {"status": "ok", "job_count": 1}


def test_build_all_ext_panels_isolates_one_broken_panel(program_root, platform_root):
    _write_panel(program_root, "good")
    _write_panel(program_root, "bad", builder_text="def build_panel(rostore, program_root):\n    raise ValueError('x')\n")
    rostore = _rostore(program_root, platform_root)
    try:
        results = ext.build_all_ext_panels(program_root, rostore)
    finally:
        rostore.close()
    assert results["good"] == {"status": "ok", "n": 1}
    assert results["bad"]["status"] == "ext_error"


def test_build_all_ext_panels_empty_when_none_declared(program_root, platform_root):
    rostore = _rostore(program_root, platform_root)
    try:
        results = ext.build_all_ext_panels(program_root, rostore)
    finally:
        rostore.close()
    assert results == {}


# ---------------------------------------------------------------------------
# check_ext_panel_stages (the three-stage validation the doctor check uses)
# ---------------------------------------------------------------------------
def test_check_ext_panel_stages_manifest_error(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", toml_text="not [ valid")
    entry = ext.load_ext_panel_entry("p", panel_dir)
    stage, message = ext.check_ext_panel_stages(entry)
    assert stage == "manifest_error"
    assert message


def test_check_ext_panel_stages_import_error(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", builder_text="not ) valid (\n")
    entry = ext.load_ext_panel_entry("p", panel_dir)
    stage, _ = ext.check_ext_panel_stages(entry)
    assert stage == "import_error"


def test_check_ext_panel_stages_signature_error(tmp_path):
    panel_dir = _write_panel(tmp_path, "p", builder_text="def build_panel(only_one):\n    return {}\n")
    entry = ext.load_ext_panel_entry("p", panel_dir)
    stage, _ = ext.check_ext_panel_stages(entry)
    assert stage == "signature_error"


def test_check_ext_panel_stages_ok(tmp_path):
    panel_dir = _write_panel(tmp_path, "p")
    entry = ext.load_ext_panel_entry("p", panel_dir)
    stage, _ = ext.check_ext_panel_stages(entry)
    assert stage == "ok"


# ---------------------------------------------------------------------------
# doctor check: ext_panels_valid
# ---------------------------------------------------------------------------
def _run_doctor(program_root=None):
    clear_registry()
    discover_and_register_checks()
    return {r.name: r for r in run_checks(DoctorContext(program_root=program_root), only=["ext_panels_valid"])}


def test_doctor_check_auto_discovered():
    results = _run_doctor()
    assert "ext_panels_valid" in results


def test_doctor_check_skips_with_no_program_root():
    result = _run_doctor(program_root=None)["ext_panels_valid"]
    assert result.status == "skip"


def test_doctor_check_skips_with_no_trialerror_ext_dir(tmp_path):
    result = _run_doctor(program_root=tmp_path)["ext_panels_valid"]
    assert result.status == "skip"


def test_doctor_check_passes_with_valid_panels(tmp_path):
    _write_panel(tmp_path, "p")
    result = _run_doctor(program_root=tmp_path)["ext_panels_valid"]
    assert result.status == "pass"
    assert result.details["panels"] == [{"name": "p", "stage": "ok", "message": result.details["panels"][0]["message"]}]


def test_doctor_check_warns_with_a_broken_panel_never_fails(tmp_path):
    _write_panel(tmp_path, "good")
    _write_panel(tmp_path, "bad", toml_text="not [ valid")
    result = _run_doctor(program_root=tmp_path)["ext_panels_valid"]
    assert result.status == "warn"  # never "fail" -- a broken extension panel is always a warn
    offender_names = {p["name"] for p in result.details["panels"] if p["stage"] != "ok"}
    assert offender_names == {"bad"}
