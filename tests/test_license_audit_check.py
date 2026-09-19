from pathlib import Path

from trialerror.util.checks import check_license_audit
from trialerror.util.doctor import DoctorContext

GOOD_HEADER_PY = """# upstream: https://github.com/example/project
# commit: 1a2b3c4
# license: MIT
# verified-by: build-M0
# date: 2026-08-29

print("hello")
"""

GOOD_HEADER_MD = """<!--
upstream: https://example.com/doc
commit: v1.2.3
license: Apache-2.0
verified-by: build-M0
date: 2026-08-29
-->

# Some vendored doc
"""

HEADERLESS_PY = """def hello():
    print("no header here at all")
"""


def _mk_vendored_root(tmp_path: Path, *, with_manifest: bool = True) -> Path:
    root = tmp_path / "vendored"
    root.mkdir()
    if with_manifest:
        (root / "VENDORED.md").write_text("# manifest\n", encoding="utf-8")
    return root


def test_fails_on_headerless_vendored_fixture(tmp_path):
    root = _mk_vendored_root(tmp_path)
    item = root / "some-lib"
    item.mkdir()
    (item / "adapted.py").write_text(HEADERLESS_PY, encoding="utf-8")

    ctx = DoctorContext(repo_root=tmp_path, vendored_root=root)
    result = check_license_audit(ctx)

    assert result.status == "fail"
    assert "adapted.py" in " ".join(result.details["offenders"])


def test_passes_when_every_file_has_the_header(tmp_path):
    root = _mk_vendored_root(tmp_path)
    item = root / "some-lib"
    item.mkdir()
    (item / "adapted.py").write_text(GOOD_HEADER_PY, encoding="utf-8")
    (item / "notes.md").write_text(GOOD_HEADER_MD, encoding="utf-8")

    ctx = DoctorContext(repo_root=tmp_path, vendored_root=root)
    result = check_license_audit(ctx)

    assert result.status == "pass", result.details
    assert result.details["offenders"] == []
    assert result.details["files_scanned"] == 2


def test_fails_when_manifest_missing_but_items_exist(tmp_path):
    root = _mk_vendored_root(tmp_path, with_manifest=False)
    item = root / "some-lib"
    item.mkdir()
    (item / "adapted.py").write_text(GOOD_HEADER_PY, encoding="utf-8")

    ctx = DoctorContext(repo_root=tmp_path, vendored_root=root)
    result = check_license_audit(ctx)

    assert result.status == "fail"
    assert result.details["manifest_present"] is False


def test_passes_when_no_vendored_dir_exists_at_all(tmp_path):
    ctx = DoctorContext(repo_root=tmp_path, vendored_root=tmp_path / "vendored")
    result = check_license_audit(ctx)
    assert result.status == "pass"


def test_passes_when_vendored_dir_exists_but_is_empty(tmp_path):
    root = _mk_vendored_root(tmp_path)
    ctx = DoctorContext(repo_root=tmp_path, vendored_root=root)
    result = check_license_audit(ctx)
    assert result.status == "pass"


def test_pyc_under_vendored_pycache_is_not_an_offender(tmp_path):
    """M15 regression test (INTEGRATION_NOTES.md item 4 / M11 flag chip
    task_2fe5d707): a compiled ``.pyc`` living under a vendored item's own
    ``__pycache__/`` (created the first time that module is imported) must
    NOT be scanned as if it were a headerless source file -- ``rglob``
    descends into ``__pycache__`` even though the directory entry itself
    is skipped, so the bug was a false positive on any vendored ``.py``
    that had actually been imported at least once."""
    root = _mk_vendored_root(tmp_path)
    item = root / "some-lib"
    item.mkdir()
    (item / "adapted.py").write_text(GOOD_HEADER_PY, encoding="utf-8")
    pycache = item / "__pycache__"
    pycache.mkdir()
    (pycache / "adapted.cpython-312.pyc").write_bytes(b"\x00\x01\x02not a valid header at all")

    ctx = DoctorContext(repo_root=tmp_path, vendored_root=root)
    result = check_license_audit(ctx)

    assert result.status == "pass", result.details
    assert result.details["offenders"] == []
    # only the real source file is counted -- the .pyc never enters the scan
    assert result.details["files_scanned"] == 1


def test_pyc_under_nested_subpackage_pycache_is_not_an_offender(tmp_path):
    """Same bug, one directory deeper -- a vendored item with its own
    subpackage (``some-lib/sub/__pycache__/...``) must be equally
    immune, not just a top-level ``__pycache__``."""
    root = _mk_vendored_root(tmp_path)
    item = root / "some-lib"
    sub = item / "sub"
    sub.mkdir(parents=True)
    (sub / "mod.py").write_text(GOOD_HEADER_PY, encoding="utf-8")
    pycache = sub / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-312.pyc").write_bytes(b"\x00\x01garbage")

    ctx = DoctorContext(repo_root=tmp_path, vendored_root=root)
    result = check_license_audit(ctx)

    assert result.status == "pass", result.details
    assert result.details["offenders"] == []
    assert result.details["files_scanned"] == 1


def test_partial_header_still_fails(tmp_path):
    """A file with SOME but not all five required fields must still fail —
    guards against a check that's satisfied by e.g. just a license line."""
    root = _mk_vendored_root(tmp_path)
    item = root / "some-lib"
    item.mkdir()
    (item / "partial.py").write_text(
        "# license: MIT\n# date: 2026-08-29\nprint('partial header only')\n",
        encoding="utf-8",
    )

    ctx = DoctorContext(repo_root=tmp_path, vendored_root=root)
    result = check_license_audit(ctx)

    assert result.status == "fail"
    assert "partial.py" in " ".join(result.details["offenders"])
