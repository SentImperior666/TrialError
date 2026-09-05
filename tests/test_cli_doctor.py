import json
from pathlib import Path

from trialerror.cli import main
from trialerror.stores.store import open_store

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cli_doctor_license_audit_fails_on_headerless_vendored_fixture(tmp_path, capsys):
    vroot = tmp_path / "vendored"
    item = vroot / "some-lib"
    item.mkdir(parents=True)
    (item / "adapted.py").write_text("print('no header')\n", encoding="utf-8")
    (vroot / "VENDORED.md").write_text("# manifest\n", encoding="utf-8")

    rc = main(["doctor", "--license-audit", "--vendored-root", str(vroot)])

    out = capsys.readouterr().out.strip()
    env = json.loads(out)

    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "doctor_checks_failed"
    checks = env["error"]["details"]["checks"]
    lic = next(c for c in checks if c["name"] == "license_audit")
    assert lic["status"] == "fail"
    assert "adapted.py" in " ".join(lic["details"]["offenders"])


def test_cli_doctor_license_audit_passes_on_well_headered_fixture(tmp_path, capsys):
    vroot = tmp_path / "vendored"
    item = vroot / "some-lib"
    item.mkdir(parents=True)
    (item / "adapted.py").write_text(
        "# upstream: https://example.com/x\n"
        "# commit: abc123\n"
        "# license: MIT\n"
        "# verified-by: build-M0\n"
        "# date: 2026-08-29\n"
        "print('fine')\n",
        encoding="utf-8",
    )
    (vroot / "VENDORED.md").write_text("# manifest\n", encoding="utf-8")

    rc = main(["doctor", "--license-audit", "--vendored-root", str(vroot)])

    out = capsys.readouterr().out.strip()
    env = json.loads(out)

    assert rc == 0
    assert env["ok"] is True
    checks = env["result"]["checks"]
    lic = next(c for c in checks if c["name"] == "license_audit")
    assert lic["status"] == "pass"


def test_cli_doctor_runs_all_registered_checks_by_default(tmp_path, capsys):
    rc = main(["doctor", "--vendored-root", str(tmp_path / "vendored")])
    out = capsys.readouterr().out.strip()
    env = json.loads(out)
    assert rc == 0
    names = {c["name"] for c in env["result"]["checks"]}
    assert "license_audit" in names


def test_cli_doctor_on_repo_own_vendored_dir_is_clean(capsys):
    """Sanity/regression: the real vendored/VENDORED.md this module ships
    must itself pass the audit it defines."""
    rc = main(["doctor", "--license-audit", "--repo-root", str(_REPO_ROOT)])
    out = capsys.readouterr().out.strip()
    env = json.loads(out)
    assert rc == 0, env
    assert env["ok"] is True


def test_cli_doctor_license_audit_ignores_pyc_under_vendored_pycache(tmp_path, capsys):
    """M15 regression test at the CLI layer (INTEGRATION_NOTES.md item 4 /
    M11 flag chip task_2fe5d707) -- see
    tests/test_license_audit_check.py for the unit-level version of this
    same fix. A ``.pyc`` under a vendored item's own ``__pycache__/`` must
    not fail the audit `trialerror doctor --license-audit` runs."""
    vroot = tmp_path / "vendored"
    item = vroot / "some-lib"
    item.mkdir(parents=True)
    (item / "adapted.py").write_text(
        "# upstream: https://example.com/x\n"
        "# commit: abc123\n"
        "# license: MIT\n"
        "# verified-by: build-M0\n"
        "# date: 2026-08-29\n"
        "print('fine')\n",
        encoding="utf-8",
    )
    pycache = item / "__pycache__"
    pycache.mkdir()
    (pycache / "adapted.cpython-312.pyc").write_bytes(b"\x00\x01\x02not a header")
    (vroot / "VENDORED.md").write_text("# manifest\n", encoding="utf-8")

    rc = main(["doctor", "--license-audit", "--vendored-root", str(vroot)])

    out = capsys.readouterr().out.strip()
    env = json.loads(out)

    assert rc == 0, env
    assert env["ok"] is True
    checks = env["result"]["checks"]
    lic = next(c for c in checks if c["name"] == "license_audit")
    assert lic["status"] == "pass", lic
    assert lic["details"]["offenders"] == []


def test_cli_doctor_program_root_flag_lets_program_scoped_checks_resolve(tmp_path, capsys, monkeypatch):
    """M15 fix (INTEGRATION_NOTES.md item 5): top-level `trialerror doctor` had
    no ``--program-root`` flag, so program-scoped checks (M1's
    ``store_schema_version``/``xid_dangling``, ...) always saw
    ``DoctorContext.program_root=None`` and silently reported ``skip``
    through the shared CLI, no matter how real the program scaffold was.
    Without the flag every present DB is unresolvable -> skip; with it,
    a real (migrated) program's 3 program-scoped DBs (platform is resolved
    independently) are found and checked.

    ``monkeypatch.chdir(tmp_path)`` below is load-bearing, same convention as
    ``tests/test_session_cli.py::test_session_program_root_not_found``: since
    the Sec 6 item 2 fix, an omitted ``--program-root`` now falls back to
    ``find_program_root()`` from CWD -- and the harness repo's own root
    carries a real (untracked, gitignored) ``trialerror.toml`` for local dev.
    Running this from the repo root unchanged would have the "before the
    flag" half accidentally resolve THAT real program instead of proving
    "no program root" behavior."""
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    monkeypatch.chdir(tmp_path)
    store = open_store(program_root, platform_root=platform_root)
    store.close()

    rc = main(["doctor", "--only", "store_schema_version", "--vendored-root", str(tmp_path / "vendored")])
    out = capsys.readouterr().out.strip()
    env = json.loads(out)
    assert rc == 0, env
    check = next(c for c in env["result"]["checks"] if c["name"] == "store_schema_version")
    assert check["details"]["ops"]["status"] == "skip"
    assert check["details"]["knowledge"]["status"] == "skip"
    assert check["details"]["jobs"]["status"] == "skip"

    rc2 = main([
        "doctor", "--only", "store_schema_version",
        "--vendored-root", str(tmp_path / "vendored"),
        "--program-root", str(program_root),
    ])
    out2 = capsys.readouterr().out.strip()
    env2 = json.loads(out2)
    assert rc2 == 0, env2
    check2 = next(c for c in env2["result"]["checks"] if c["name"] == "store_schema_version")
    for db_kind in ("platform", "ops", "knowledge", "jobs"):
        assert check2["details"][db_kind]["match"] is True, check2["details"][db_kind]


def test_cli_doctor_platform_root_flag_wins_over_a_mismatching_env_var(tmp_path, capsys, monkeypatch):
    """Regression for fix-accept (C-0064, task_c92b015f): ``trialerror doctor``
    had no ``--platform-root`` flag at all, and ``DoctorContext`` had no
    ``platform_root`` field for it to fill in even if it had -- so a
    platform-scoped check (here ``xid_dangling``) could ONLY ever see
    whatever ``TRIALERROR_PLATFORM_ROOT``/``~/.trialerror`` happened to resolve to,
    regardless of an explicit ``--platform-root`` flag. This is why a real
    machine's own ``~/.trialerror/platform.db`` could leak into `trialerror accept`
    even though the acceptance journey resolved its own scratch
    platform_root elsewhere.

    Deliberately points ``TRIALERROR_PLATFORM_ROOT`` at a MISMATCHING (empty)
    directory throughout, so the only way the second call below can find
    the real, migrated platform.db is via ``--platform-root`` itself -- an
    env-only resolution could not pass this test even by coincidence."""
    real_platform_root = tmp_path / "real_platform"
    mismatching_env_root = tmp_path / "mismatching_platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(mismatching_env_root))

    store = open_store(program_root, platform_root=real_platform_root)
    store.close()

    # without --platform-root: xid_dangling resolves "platform" via the
    # (mismatching, DB-less) env var and must skip -- not because the store
    # is broken, but because that DB file genuinely isn't there.
    rc = main([
        "doctor", "--only", "xid_dangling",
        "--vendored-root", str(tmp_path / "vendored"),
        "--program-root", str(program_root),
    ])
    out = capsys.readouterr().out.strip()
    env = json.loads(out)
    assert rc == 0, env
    check = next(c for c in env["result"]["checks"] if c["name"] == "xid_dangling")
    assert check["status"] == "skip", check
    assert "platform" in check["details"].get("missing", {}), check

    # with --platform-root pointing at the REAL, migrated platform.db: the
    # check must resolve it (and the store is clean, so it passes) even
    # though TRIALERROR_PLATFORM_ROOT still points at the mismatching directory.
    rc2 = main([
        "doctor", "--only", "xid_dangling",
        "--vendored-root", str(tmp_path / "vendored"),
        "--program-root", str(program_root),
        "--platform-root", str(real_platform_root),
    ])
    out2 = capsys.readouterr().out.strip()
    env2 = json.loads(out2)
    assert rc2 == 0, env2
    check2 = next(c for c in env2["result"]["checks"] if c["name"] == "xid_dangling")
    assert check2["status"] == "pass", check2
    assert check2["details"]["offenders"] == {}


def test_cli_doctor_program_root_defaults_to_find_program_root_from_cwd(tmp_path, capsys, monkeypatch):
    """LANE0_SANDBOX_RELOCATION_DESIGN.md Sec 6 item 2 (INTEGRATION_NOTES.md item 5
    follow-up): item 5 gave `trialerror doctor` a ``--program-root`` FLAG but no
    DEFAULT, so running the top-level command from inside a real, scaffolded
    program root with NO flag at all still left ``DoctorContext.program_root=None``
    and every program-scoped check silently skipped -- exactly as if no program
    existed. Every other CLI group (e.g. ``trialerror session boot``, via
    ``trialerror/cli/session.py``'s ``_resolve_program_root``) falls back to
    ``find_program_root()`` (walk up from CWD for ``trialerror.toml``) when the
    flag is omitted everywhere; doctor must do the same.

    Runs `trialerror doctor` (via ``main()``, no ``--program-root``) with CWD set
    to a nested subdirectory of a real, migrated program root -- proving both
    that the default now resolves at all, and that it walks UP (not just checks
    CWD itself) the same way ``find_program_root`` documents."""
    platform_root = tmp_path / "platform"
    program_root = tmp_path / "program"
    program_root.mkdir()
    # find_program_root() looks for the trialerror.toml FILE itself -- open_store()
    # alone (below) never writes one, so without this write, the walk-up from
    # nested_cwd would find no marker at all (or, run from an unisolated CWD,
    # the wrong one -- see the sibling test's docstring above).
    (program_root / "trialerror.toml").write_text('[program]\nid = "doctor-default-check"\n', encoding="utf-8")
    nested_cwd = program_root / "raw" / "some" / "nested" / "dir"
    nested_cwd.mkdir(parents=True)
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    store = open_store(program_root, platform_root=platform_root)
    store.close()

    monkeypatch.chdir(nested_cwd)

    rc = main(["doctor", "--only", "store_schema_version", "--vendored-root", str(tmp_path / "vendored")])
    out = capsys.readouterr().out.strip()
    env = json.loads(out)
    assert rc == 0, env
    check = next(c for c in env["result"]["checks"] if c["name"] == "store_schema_version")
    # Before the fix these three were {"status": "skip", ...} (program_root stayed
    # None) even though a real, migrated program sits right above CWD; a resolved
    # DB reports {"current_version", "expected_version", "match"} instead (no
    # "status" key at all -- trialerror/stores/checks.py:104 vs :113).
    for db_kind in ("ops", "knowledge", "jobs"):
        assert check["details"][db_kind].get("status") != "skip", check
        assert check["details"][db_kind]["match"] is True, check
