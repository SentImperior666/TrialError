"""M4's doctor checks: ``law_digest_lockstep``, ``law_chain_integrity``,
``law_pin_format``. Auto-discovery (no import needed) plus planted-fixture
adversarial cases, mirroring ``tests/test_stores_checks.py``'s convention.
"""

from __future__ import annotations

from trialerror.law.service import RENDERED_PATH, append_ruling
from trialerror.stores import insert, update
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _run(names, program_root):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root)
    results = run_checks(ctx, only=names)
    return {r.name: r for r in results}


def test_law_checks_are_auto_discovered_without_import():
    clear_registry()
    discover_and_register_checks()
    from trialerror.util.doctor import registered_checks

    names = set(registered_checks())
    assert {"law_digest_lockstep", "law_chain_integrity", "law_pin_format"} <= names


# ---- law_digest_lockstep ---------------------------------------------------


def test_law_digest_lockstep_skips_when_ops_db_absent(tmp_path, platform_root):
    results = _run(["law_digest_lockstep"], tmp_path / "never_initialized")
    assert results["law_digest_lockstep"].status == "skip"


def test_law_digest_lockstep_skips_when_no_ruling_ever_appended(store, program_root):
    results = _run(["law_digest_lockstep"], program_root)
    assert results["law_digest_lockstep"].status == "skip"


def test_law_digest_lockstep_passes_on_clean_store(store, program_root):
    append_ruling(store, summary="clean ruling")
    results = _run(["law_digest_lockstep"], program_root)
    r = results["law_digest_lockstep"]
    assert r.status == "pass"
    assert r.details["db_lockstep_ok"] is True
    assert r.details["file_lockstep_ok"] is True


def test_law_digest_lockstep_catches_ledger_tampered_after_append(store, program_root):
    result = append_ruling(store, summary="original")
    with store.ops:
        store.ops.execute(
            "UPDATE ruling SET summary = ? WHERE ruling_id = ?", ("TAMPERED", result.ruling_id)
        )
    results = _run(["law_digest_lockstep"], program_root)
    r = results["law_digest_lockstep"]
    assert r.status == "fail"
    assert r.details["db_lockstep_ok"] is False


def test_law_digest_lockstep_catches_missing_rendered_file(store, program_root):
    append_ruling(store, summary="original")
    (store.program_root / RENDERED_PATH).unlink()
    results = _run(["law_digest_lockstep"], program_root)
    r = results["law_digest_lockstep"]
    assert r.status == "fail"
    assert r.details["file_exists"] is False


def test_law_digest_lockstep_catches_hand_edited_rendered_file(store, program_root):
    append_ruling(store, summary="original")
    path = store.program_root / RENDERED_PATH
    path.write_text(path.read_text(encoding="utf-8") + "\nhand-edited addition\n", encoding="utf-8")
    results = _run(["law_digest_lockstep"], program_root)
    r = results["law_digest_lockstep"]
    assert r.status == "fail"
    assert r.details["file_lockstep_ok"] is False
    assert r.details["db_lockstep_ok"] is True  # the DB side is still fine


# ---- law_chain_integrity ----------------------------------------------------


def test_law_chain_integrity_passes_on_clean_store(store, program_root):
    append_ruling(store, summary="one")
    append_ruling(store, summary="two")
    results = _run(["law_chain_integrity"], program_root)
    r = results["law_chain_integrity"]
    assert r.status == "pass"
    assert r.details["ok"] is True


def test_law_chain_integrity_catches_planted_tampering(store, program_root):
    result = append_ruling(store, summary="one")
    append_ruling(store, summary="two")
    with store.ops:
        store.ops.execute(
            "UPDATE ruling SET summary = ? WHERE ruling_id = ?", ("TAMPERED", result.ruling_id)
        )
    results = _run(["law_chain_integrity"], program_root)
    r = results["law_chain_integrity"]
    assert r.status == "fail"
    assert r.details["first_break_ruling_id"] == result.ruling_id


# ---- law_pin_format ----------------------------------------------------------


def test_law_pin_format_passes_with_no_recorded_pins(store, program_root):
    results = _run(["law_pin_format"], program_root)
    r = results["law_pin_format"]
    assert r.status == "pass"
    assert r.details["offenders"] == {}


def test_law_pin_format_passes_with_a_well_formed_pin(store, program_root):
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "acct", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    update(store, "session", pk_column="session_id", pk_value=session_id, changes={"boot_pin_version": "v3@2026-08-29"})

    results = _run(["law_pin_format"], program_root)
    r = results["law_pin_format"]
    assert r.status == "pass"


def test_law_pin_format_catches_malformed_pin(store, program_root):
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "acct", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    update(store, "session", pk_column="session_id", pk_value=session_id, changes={"boot_pin_version": "garbage"})

    results = _run(["law_pin_format"], program_root)
    r = results["law_pin_format"]
    assert r.status == "fail"
    assert session_id in r.details["offenders"]
