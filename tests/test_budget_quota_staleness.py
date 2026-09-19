"""A plan-quota capture that has gone quiet, and what each surface does
about it.

Lane FB-3 item 8 (custodian observation (b), 2026-09-15). The capture is
written by the statusLine script, which Claude Code runs on a UI tick -- so
the reading goes stale exactly while the orchestrator is idle, which is also
when it is most likely to be consulted before booking. A number that WAS
true is the kind a reader trusts without checking its age.
"""

from __future__ import annotations

import json
import time

import pytest
from pathlib import Path

from trialerror.budget.quota import (
    DEFAULT_FRESH_WITHIN_S,
    booking_quota_reading,
    booking_refusal,
    quota_status,
    resolve_max_age_s,
    staleness,
)
from trialerror.stores import get
from trialerror.util.doctor import DoctorContext, clear_registry, discover_and_register_checks, run_checks

from tests._budget_fixtures import open_account_session


def _write_capture(quota_dir, *, age_s: float) -> dict:
    quota_dir.mkdir(parents=True, exist_ok=True)
    epoch = time.time() - age_s
    snap = {
        "epoch": epoch,
        "captured_ts": "2026-09-15T09:00:00.000Z",
        "session_id": "sess-test",
        "rate_limits": {
            "five_hour": {"used_percentage": 42.0, "resets_at": "2026-09-15T14:00:00Z"},
            "seven_day": {"used_percentage": 88.0, "resets_at": "2026-09-19T09:00:00Z"},
        },
    }
    (quota_dir / "latest.json").write_text(json.dumps(snap), encoding="utf-8")
    return snap


def _write_config(program_root, *, max_age_s: int | None) -> None:
    body = '[program]\nid = "PROG-test"\n'
    if max_age_s is not None:
        body += f"\n[budget]\nquota_max_age_s = {max_age_s}\n"
    (program_root / "trialerror.toml").write_text(body, encoding="utf-8")


def _run_check(name, **ctx_kwargs):
    clear_registry()
    discover_and_register_checks()
    return {r.name: r for r in run_checks(DoctorContext(**ctx_kwargs), only=[name])}[name]


# ---------------------------------------------------------------------------
# the bar
# ---------------------------------------------------------------------------


def test_the_default_bar_is_fifteen_minutes():
    assert resolve_max_age_s(None) == DEFAULT_FRESH_WITHIN_S == 900


def test_the_config_knob_moves_the_bar():
    assert resolve_max_age_s({"budget": {"quota_max_age_s": 60}}) == 60


def test_an_explicit_override_beats_the_knob():
    assert resolve_max_age_s({"budget": {"quota_max_age_s": 60}}, override=5) == 5


@pytest.mark.parametrize("value", ["900", 0, -1, True, None, {"nested": 1}])
def test_a_nonsense_knob_falls_back_to_the_default_rather_than_raising(value):
    """A typo in one knob must not make `budget book` unusable."""
    assert resolve_max_age_s({"budget": {"quota_max_age_s": value}}) == DEFAULT_FRESH_WITHIN_S


# ---------------------------------------------------------------------------
# absent is not stale
# ---------------------------------------------------------------------------


def test_no_capture_at_all_reads_as_absent_not_stale(tmp_path):
    reading = staleness(quota_status(str(tmp_path / "nothing-here")), max_age_s=900)
    assert reading["standing"] == "absent"
    assert reading["stale"] is False
    assert booking_refusal(reading) is None


def test_a_fresh_capture_is_fresh(tmp_path):
    _write_capture(tmp_path, age_s=10)
    reading = staleness(quota_status(str(tmp_path), fresh_within_s=900), max_age_s=900)
    assert reading["standing"] == "fresh"
    assert booking_refusal(reading) is None


def test_a_stale_capture_carries_its_age(tmp_path):
    _write_capture(tmp_path, age_s=4000)
    reading = staleness(quota_status(str(tmp_path), fresh_within_s=900), max_age_s=900)
    assert reading["standing"] == "stale"
    assert reading["age_s"] > 3900
    assert "--allow-stale-quota" in booking_refusal(reading)


# ---------------------------------------------------------------------------
# `budget quota`
# ---------------------------------------------------------------------------


def _cli(argv, program_root, platform_root):
    from trialerror.cli import main

    return main(["--program-root", str(program_root), "--platform-root", str(platform_root), "budget", *argv])


def test_budget_quota_reports_stale_with_the_age_and_names_the_capture(store, tmp_path, capsys):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    _write_config(store.program_root, max_age_s=None)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(["quota", "--quota-dir", str(quota_dir)], program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["result"]["fresh"] is False
    assert out["result"]["staleness"]["standing"] == "stale"
    assert out["result"]["staleness"]["age_s"] > 3900
    assert "statusline_capture.py" in out["result"]["note"]
    assert any(a["argv"][-1] == "quota_capture_stale" for a in out["nextActions"])


def test_budget_quota_reads_the_bar_from_the_programs_config(store, tmp_path, capsys):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=120)
    _write_config(store.program_root, max_age_s=60)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    _cli(["quota", "--quota-dir", str(quota_dir)], program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["staleness"]["max_age_s"] == 60
    assert out["result"]["fresh"] is False


def test_budget_quota_is_quiet_when_the_capture_is_fresh(store, tmp_path, capsys):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=5)
    _write_config(store.program_root, max_age_s=None)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    _cli(["quota", "--quota-dir", str(quota_dir)], program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["fresh"] is True
    assert out["nextActions"] == []


# ---------------------------------------------------------------------------
# the booking gate
# ---------------------------------------------------------------------------


def _book_argv(session_id, quota_dir, *extra):
    return [
        "book", "--session-id", session_id, "--program-id", "PROG-test", "--agent-kind", "lens",
        "--model-class", "mid", "--model", "sonnet", "--purpose", "mechanical",
        "--est-tokens", "100", "--quota-dir", str(quota_dir), *extra,
    ]


def test_booking_is_refused_on_a_stale_capture(store, tmp_path, capsys):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    _write_config(store.program_root, max_age_s=None)
    _account, session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(_book_argv(session_id, quota_dir), program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code != 0
    assert out["error"]["code"] == "stale_quota_capture"
    assert "--allow-stale-quota" in out["error"]["message"]
    assert out["error"]["details"]["age_s"] > 3900


def test_the_override_books_and_records_the_reading_it_overrode(store, tmp_path, capsys):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    _write_config(store.program_root, max_age_s=None)
    _account, session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(_book_argv(session_id, quota_dir, "--allow-stale-quota"), program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    launch_id = out["result"]["launch_id"]

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        attrs = json.loads(get(reopened, "launch", pk_column="launch_id", pk_value=launch_id)["attrs"])
    finally:
        reopened.close()
    assert attrs["allowed_stale_quota"]["standing"] == "stale"
    assert attrs["allowed_stale_quota"]["age_s"] > 3900
    assert attrs["allowed_stale_quota"]["max_age_s"] == 900


def test_a_fresh_capture_books_with_no_note_on_the_launch(store, tmp_path, capsys):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=5)
    _write_config(store.program_root, max_age_s=None)
    _account, session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(_book_argv(session_id, quota_dir), program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        row = get(reopened, "launch", pk_column="launch_id", pk_value=out["result"]["launch_id"])
    finally:
        reopened.close()
    assert row["attrs"] is None


def test_a_program_with_no_capture_at_all_books_normally(store, tmp_path, capsys):
    """An optional feed must not become mandatory by accident."""
    _write_config(store.program_root, max_age_s=None)
    _account, session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(_book_argv(session_id, tmp_path / "never-captured"), program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out


def test_the_mcp_book_tool_refuses_on_the_same_reading(store, tmp_path, monkeypatch):
    """The surface an orchestrator actually books through. A refusal that
    fired on only the CLI would be one nothing ever met."""
    from trialerror.mcp.ops import _tool_book_launch

    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    _write_config(store.program_root, max_age_s=None)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))
    _account, session_id = open_account_session(store)

    args = {
        "session_id": session_id, "program_id": "PROG-test", "agent_kind": "lens",
        "model_class": "mid", "model": "sonnet", "purpose": "mechanical", "est_tokens": 100,
    }
    refused = _tool_book_launch(args, store=store)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "stale_quota_capture"

    allowed = _tool_book_launch({**args, "allow_stale_quota": True}, store=store)
    assert allowed["ok"] is True
    attrs = json.loads(
        get(store, "launch", pk_column="launch_id", pk_value=allowed["result"]["launch_id"])["attrs"]
    )
    assert attrs["allowed_stale_quota"]["standing"] == "stale"


def test_the_booking_guard_reads_the_programs_own_bar(store, tmp_path, monkeypatch):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=120)
    _write_config(store.program_root, max_age_s=60)
    reading = booking_quota_reading(store.program_root, quota_dir=str(quota_dir))
    assert reading["max_age_s"] == 60
    assert reading["stale"] is True


# ---------------------------------------------------------------------------
# the doctor check
# ---------------------------------------------------------------------------


def test_quota_capture_stale_warns_on_an_old_capture(tmp_path, monkeypatch):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))
    result = _run_check("quota_capture_stale")
    assert result.status == "warn"
    assert result.details["max_age_s"] == 900
    assert result.details["age_s"] > 3900
    assert "--allow-stale-quota" in result.message


def test_quota_capture_stale_passes_on_a_fresh_capture(tmp_path, monkeypatch):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=5)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))
    result = _run_check("quota_capture_stale")
    assert result.status == "pass"


def test_quota_capture_stale_skips_when_nothing_was_ever_captured(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(tmp_path / "never"))
    result = _run_check("quota_capture_stale")
    assert result.status == "skip"
    assert "USER_SETUP.md" in result.message


def test_quota_capture_stale_uses_the_programs_bar_and_says_where_it_came_from(
    tmp_path, program_root, monkeypatch
):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=120)
    _write_config(program_root, max_age_s=60)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))

    result = _run_check("quota_capture_stale", program_root=program_root)
    assert result.status == "warn"
    assert result.details["max_age_s"] == 60
    assert result.details["bar_read_from"] == "this program's trialerror.toml"


def test_quota_capture_stale_says_when_it_had_no_config_to_read(tmp_path, monkeypatch):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=5)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))
    result = _run_check("quota_capture_stale")
    assert "no program root" in result.details["bar_read_from"]


# ---------------------------------------------------------------------------
# Fix pass V-1: the suite's own capture dir
# ---------------------------------------------------------------------------


def test_the_suite_never_reads_the_machines_own_quota_capture(tmp_path):
    """The gate this file tests reads a file on disk, so every OTHER test in
    the suite -- every booking test -- reads one too. ``tests/conftest.py``'s
    autouse ``_isolated_quota_dir`` points ``TRIALERROR_QUOTA_DIR`` at an
    empty per-test dir, so a booking test's verdict cannot depend on how long
    ago the machine running it last ticked its statusLine.

    Asserted rather than trusted: the fixture is invisible at the point of
    use, and the failure it prevents (four booking tests going red fifteen
    idle minutes after they went green, on an unchanged commit) reads like a
    flaky test rather than like a missing fixture."""
    import os

    quota_dir = os.environ.get("TRIALERROR_QUOTA_DIR")
    assert quota_dir, "TRIALERROR_QUOTA_DIR is unset: this test's booking siblings read ~/.trialerror/quota"
    # Under this test's own tmp_path root, i.e. per-test, not shared.
    # The autouse fixture allocates its dir from tmp_path_factory (a sibling of
    # this test's tmp_path under the same pytest base), never the machine's
    # own capture dir -- that is the property this test guards.
    assert str(tmp_path.parent) in quota_dir and "quota_capture" in quota_dir
    assert str(Path.home()) not in quota_dir
    # Empty: absent is not stale, which is the one reading that cannot age.
    assert booking_quota_reading(tmp_path)["standing"] == "absent"


# ---------------------------------------------------------------------------
# Fix pass V-2: whose fault is it?
# ---------------------------------------------------------------------------


def test_a_closed_session_is_reported_as_such_even_on_a_stale_capture(store, tmp_path, capsys):
    """The operator's own command is judged before the environment's reading.
    A booking from a closed session that came back `stale_quota_capture` sent
    the reader to the statusLine to fix a fault in their argv."""
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    _write_config(store.program_root, max_age_s=None)
    _account, session_id = open_account_session(store, status="closed")
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(_book_argv(session_id, quota_dir), program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code != 0
    assert out["error"]["code"] == "no_open_session"


def test_a_model_policy_violation_is_reported_as_such_even_on_a_stale_capture(
    store, tmp_path, capsys
):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    (store.program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n[models]\nmechanical = "top"\n', encoding="utf-8"
    )
    _account, session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    code = _cli(_book_argv(session_id, quota_dir), program_root, platform_root)
    out = json.loads(capsys.readouterr().out)
    assert code != 0
    assert out["error"]["code"] == "model_policy_violation"


def test_the_mcp_tool_reports_bad_input_before_the_capture(store, tmp_path, monkeypatch):
    """`est_tokens` is coerced before the gate: a malformed call is a
    malformed call, not a stale machine."""
    from trialerror.mcp.ops import _tool_book_launch

    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    _write_config(store.program_root, max_age_s=None)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))
    _account, session_id = open_account_session(store)

    args = {
        "session_id": session_id, "program_id": "PROG-test", "agent_kind": "lens",
        "model_class": "mid", "model": "sonnet", "purpose": "mechanical", "est_tokens": "lots",
    }
    with pytest.raises(ValueError):
        # `_wrap` is what turns this into the `bad_input` envelope; the point
        # here is that it is raised at all rather than swallowed by a gate
        # that fired first.
        _tool_book_launch(args, store=store)


def test_the_mcp_tool_reports_a_closed_session_before_the_capture(store, tmp_path, monkeypatch):
    from trialerror.mcp.ops import _tool_book_launch

    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=4000)
    _write_config(store.program_root, max_age_s=None)
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(quota_dir))
    _account, session_id = open_account_session(store, status="closed")

    refused = _tool_book_launch(
        {
            "session_id": session_id, "program_id": "PROG-test", "agent_kind": "lens",
            "model_class": "mid", "model": "sonnet", "purpose": "mechanical", "est_tokens": 100,
        },
        store=store,
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == "no_open_session"


# ---------------------------------------------------------------------------
# Fix pass V-3: one bar, every surface
# ---------------------------------------------------------------------------


def test_budget_check_reads_the_same_bar_as_quota_and_the_booking_gate(store, tmp_path, capsys):
    """One program, one config, one capture: `budget quota` said stale,
    `budget book` refused, and `budget check` -- the envelope an operator
    reads BEFORE booking -- said fresh, because it was left on the hardcoded
    900 s."""
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=300)
    _write_config(store.program_root, max_age_s=120)
    account_id, session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    assert _cli(["quota", "--quota-dir", str(quota_dir)], program_root, platform_root) == 0
    quota_out = json.loads(capsys.readouterr().out)
    assert quota_out["result"]["staleness"]["standing"] == "stale"

    assert _cli(
        ["check", "--account-id", account_id, "--quota-dir", str(quota_dir)], program_root, platform_root
    ) == 0
    check_out = json.loads(capsys.readouterr().out)["result"]
    assert check_out["quota"]["fresh"] is False
    assert check_out["quota"]["staleness"]["standing"] == "stale"
    assert check_out["quota"]["staleness"]["max_age_s"] == 120
    assert "stale" in check_out["summary"]

    code = _cli(_book_argv(session_id, quota_dir), program_root, platform_root)
    assert code != 0
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "stale_quota_capture"


def test_budget_check_fresh_within_s_still_wins_for_one_reading(store, tmp_path, capsys):
    quota_dir = tmp_path / "quota"
    _write_capture(quota_dir, age_s=300)
    _write_config(store.program_root, max_age_s=120)
    account_id, _session_id = open_account_session(store)
    program_root, platform_root = store.program_root, store.platform_root
    store.close()

    assert _cli(
        ["check", "--account-id", account_id, "--quota-dir", str(quota_dir), "--fresh-within-s", "3600"],
        program_root,
        platform_root,
    ) == 0
    out = json.loads(capsys.readouterr().out)["result"]
    assert out["quota"]["fresh"] is True
    assert out["quota"]["staleness"]["standing"] == "fresh"
