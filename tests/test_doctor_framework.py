from pathlib import Path

import pytest

from trialerror.util import doctor as doctor_fw


@pytest.fixture
def clean_registry():
    """Snapshot + restore the real registry so this test file's fixture
    checks never leak into other test files (which rely on the real
    trialerror.util.checks.license_audit being registered)."""
    before = doctor_fw.registered_checks()
    doctor_fw.clear_registry()
    try:
        yield
    finally:
        doctor_fw.clear_registry()
        for name, (category, fn) in before.items():
            doctor_fw.register_check(name, category=category)(fn)


def test_register_check_and_run(clean_registry):
    @doctor_fw.register_check("always_pass", category="fixture")
    def _check(ctx):
        return doctor_fw.CheckResult(
            name="always_pass", category="fixture", status="pass", message="ok"
        )

    ctx = doctor_fw.DoctorContext(repo_root=Path("."))
    results = doctor_fw.run_checks(ctx)

    assert len(results) == 1
    assert results[0].name == "always_pass"
    assert results[0].status == "pass"


def test_run_checks_only_filter(clean_registry):
    @doctor_fw.register_check("a", category="fixture")
    def _a(ctx):
        return doctor_fw.CheckResult(name="a", category="fixture", status="pass", message="a")

    @doctor_fw.register_check("b", category="fixture")
    def _b(ctx):
        return doctor_fw.CheckResult(name="b", category="fixture", status="pass", message="b")

    ctx = doctor_fw.DoctorContext(repo_root=Path("."))
    results = doctor_fw.run_checks(ctx, only=["b"])

    assert [r.name for r in results] == ["b"]


def test_run_checks_unknown_name_is_a_failure_not_a_crash(clean_registry):
    ctx = doctor_fw.DoctorContext(repo_root=Path("."))
    results = doctor_fw.run_checks(ctx, only=["does_not_exist"])
    assert len(results) == 1
    assert results[0].status == "fail"
    assert "does_not_exist" in results[0].message


def test_a_crashing_check_is_caught_as_a_failure(clean_registry):
    @doctor_fw.register_check("boom", category="fixture")
    def _boom(ctx):
        raise RuntimeError("kaboom")

    ctx = doctor_fw.DoctorContext(repo_root=Path("."))
    results = doctor_fw.run_checks(ctx)

    assert len(results) == 1
    assert results[0].status == "fail"
    assert "kaboom" in results[0].message


def test_discover_and_register_checks_finds_util_checks(clean_registry):
    imported = doctor_fw.discover_and_register_checks()
    assert "trialerror.util.checks" in imported
    assert "license_audit" in doctor_fw.registered_checks()


def test_discover_and_register_checks_is_idempotent_after_a_registry_clear(clean_registry):
    """trialerror.util.checks is very likely already imported (by an earlier
    test, or an earlier `trialerror doctor` call in a long-lived process) --
    discovery must still repopulate the registry, not silently no-op
    because the module object is cached in sys.modules."""
    doctor_fw.discover_and_register_checks()
    assert "license_audit" in doctor_fw.registered_checks()

    doctor_fw.clear_registry()
    assert doctor_fw.registered_checks() == {}

    doctor_fw.discover_and_register_checks()
    assert "license_audit" in doctor_fw.registered_checks()


def test_check_result_to_dict_shape(clean_registry):
    result = doctor_fw.CheckResult(
        name="x", category="cat", status="warn", message="msg", details={"k": "v"}
    )
    assert result.to_dict() == {
        "name": "x",
        "category": "cat",
        "status": "warn",
        "message": "msg",
        "details": {"k": "v"},
    }
