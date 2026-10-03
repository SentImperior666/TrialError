"""``trialerror quota`` CLI group -- wiring tests. Calls each action's own
handler directly (same convention as ``tests/test_units_cli.py``): this file
is about the CLI plumbing (envelopes, argument resolution, config wiring),
not the underlying algorithms, which have their own dedicated test files
(``test_quota_rate_fit.py``, ``test_quota_forecast.py``,
``test_quota_monthly.py``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from trialerror.cli import quota as quota_cli

NOW_EPOCH = 2_000_020_000.0


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture()
def platform_root(tmp_path):
    return tmp_path / "platform"


def _write_history(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _capture_row(epoch, five_pct, five_resets, cost, session="S1", account="a"):
    return {
        "epoch": epoch, "captured_ts": "2026-09-27T00:00:00Z", "session_id": session,
        "session_cost_usd": cost, "account_label": account,
        "rate_limits": {"five_hour": {"used_percentage": five_pct, "resets_at": five_resets},
                        "seven_day": {"used_percentage": five_pct, "resets_at": 9_999_999_999}},
    }


def test_import_reports_rows_inserted(tmp_path, platform_root):
    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_capture_row(100, 40, 2_000_018_000, 1.0)])
    env = quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))
    assert env["ok"] is True
    assert env["result"]["rows_inserted"] == 1


def test_rate_fit_then_show_reports_the_stored_rate(tmp_path, platform_root):
    history = tmp_path / "rate_limits.jsonl"
    rows = [
        _capture_row(2_000_000_060, 5, 2_000_018_000, 0.0),
        _capture_row(2_000_017_900, 40, 2_000_018_000, 100.0),
    ]
    _write_history(history, rows)
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))

    fit_env = quota_cli._run_rate_fit(_ns(platform_root=str(platform_root), account_label="a"))
    assert fit_env["ok"] is True
    assert fit_env["result"]["five_hour"]["n_windows"] == 1

    show_env = quota_cli._run_rate_show(_ns(platform_root=str(platform_root), account_label="a"))
    assert show_env["ok"] is True
    assert "One dollar of work costs about" in show_env["result"]["text"]


def test_rate_show_with_no_fit_yet_says_so(platform_root):
    env = quota_cli._run_rate_show(_ns(platform_root=str(platform_root), account_label="nobody"))
    assert env["ok"] is True
    assert "no fit yet" in env["result"]["text"]


def test_rate_show_reports_no_usable_windows_with_reasons_when_the_fit_is_empty(tmp_path, platform_root):
    """Review B1: a fit with zero kept windows must say so in plain words,
    never present a fabricated 0.000 rate as if it were real."""
    history = tmp_path / "rate_limits.jsonl"
    # a complete window (two rows, a real reset) but no cost data at all --
    # every history file predates this lane's session_cost_usd field.
    _write_history(history, [
        _capture_row(2_000_000_060, 40, 2_000_018_000, None),
        _capture_row(2_000_017_900, 45, 2_000_018_000, None),
    ])
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))
    quota_cli._run_rate_fit(_ns(platform_root=str(platform_root), account_label="a"))

    env = quota_cli._run_rate_show(_ns(platform_root=str(platform_root), account_label="a"))
    assert env["ok"] is True
    text = env["result"]["text"]
    assert "no usable windows yet" in text
    assert "non_positive_dollars" in text
    assert "One dollar of work costs about" not in text
    assert env["result"]["five_hour"]["method"] == "no_estimate"


def test_forecast_refuses_with_rate_has_no_windows_and_echoes_n_windows_and_method(tmp_path, platform_root):
    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [
        _capture_row(2_000_000_060, 40, 2_000_018_000, None),
        _capture_row(2_000_017_900, 45, 2_000_018_000, None),
    ])
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))
    quota_cli._run_rate_fit(_ns(platform_root=str(platform_root), account_label="a"))

    env = quota_cli._run_forecast(_ns(
        platform_root=str(platform_root), program_root=None, account_label="a", window="five_hour",
        usd=50.0, tokens=None, current_pct=None,
    ))
    assert env["ok"] is False
    assert env["error"]["code"] == "rate_has_no_windows"
    assert env["error"]["details"]["n_windows"] == 0
    assert env["error"]["details"]["method"] == "no_estimate"


def test_forecast_usd_uses_the_stored_rate(tmp_path, platform_root):
    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_capture_row(2_000_000_060, 5, 2_000_018_000, 0.0), _capture_row(2_000_017_900, 40, 2_000_018_000, 100.0)])
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))
    quota_cli._run_rate_fit(_ns(platform_root=str(platform_root), account_label="a"))

    env = quota_cli._run_forecast(_ns(
        platform_root=str(platform_root), program_root=None, account_label="a", window="five_hour",
        usd=10.0, tokens=None, current_pct=60.0,
    ))
    assert env["ok"] is True
    assert env["result"]["points"] == pytest.approx(10.0 * 0.4, rel=0.5)  # points_per_usd ~= 40/100 = 0.4
    assert env["result"]["remaining"]["to_yield_80"] == pytest.approx(20.0)


def test_forecast_requires_exactly_one_of_usd_or_tokens(platform_root):
    env = quota_cli._run_forecast(_ns(
        platform_root=str(platform_root), program_root=None, account_label="a", window="five_hour",
        usd=None, tokens=None, current_pct=None,
    ))
    assert env["ok"] is False and env["error"]["code"] == "bad_input"


def test_forecast_without_a_rate_yet_is_a_named_error(platform_root):
    env = quota_cli._run_forecast(_ns(
        platform_root=str(platform_root), program_root=None, account_label="nobody", window="five_hour",
        usd=5.0, tokens=None, current_pct=None,
    ))
    assert env["ok"] is False and env["error"]["code"] == "no_rate"


def test_forecast_tokens_without_a_price_table_refuses_by_name(tmp_path, platform_root):
    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_capture_row(2_000_000_060, 5, 2_000_018_000, 0.0), _capture_row(2_000_017_900, 40, 2_000_018_000, 100.0)])
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))
    quota_cli._run_rate_fit(_ns(platform_root=str(platform_root), account_label="a"))

    program_root = tmp_path / "program_no_config"
    program_root.mkdir()
    env = quota_cli._run_forecast(_ns(
        platform_root=str(platform_root), program_root=str(program_root), account_label="a", window="five_hour",
        usd=None, tokens="claude-sonnet-5:input=1000", current_pct=None,
    ))
    assert env["ok"] is False and env["error"]["code"] == "no_price_for_model"


def test_month_with_no_limit_configured_says_so(tmp_path, platform_root):
    program_root = tmp_path / "program"
    program_root.mkdir()
    env = quota_cli._run_month(_ns(
        platform_root=str(platform_root), program_root=str(program_root), account_label=None,
        limit_usd=None, counts=None, month_start_day=None,
    ))
    assert env["ok"] is True
    assert env["result"]["spend"] is None


def test_month_with_cli_overrides_computes_spend_and_notices(tmp_path, platform_root):
    import time

    history = tmp_path / "rate_limits.jsonl"
    # `_run_month` computes "now" for real (it is not given a fixed clock),
    # so the fixture row must actually fall within the CURRENT month.
    _write_history(history, [_capture_row(time.time(), 5, 2_000_018_000, 5.0)])
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))

    program_root = tmp_path / "program"
    program_root.mkdir()
    env = quota_cli._run_month(_ns(
        platform_root=str(platform_root), program_root=str(program_root), account_label="a",
        limit_usd=10.0, counts="all", month_start_day=1,
    ))
    assert env["ok"] is True
    assert env["result"]["spend_usd"] == pytest.approx(5.0)
    assert env["result"]["limit_usd"] == 10.0


def test_month_crossing_80_percent_adds_a_packet_item_when_a_program_is_configured(tmp_path, platform_root):
    import time

    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_capture_row(time.time(), 5, 2_000_018_000, 85.0)])
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))

    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "test"\n', encoding="utf-8")

    env = quota_cli._run_month(_ns(
        platform_root=str(platform_root), program_root=str(program_root), account_label="a",
        limit_usd=100.0, counts="all", month_start_day=1,
    ))
    assert env["ok"] is True
    assert [n["level"] for n in env["result"]["new_notices"]] == [50, 80]
    assert "packet_item" in env["result"]
    assert env["result"]["packet_item"]["warnings"] == []


def test_month_crossing_80_percent_adds_no_packet_item_without_a_program_config(tmp_path, platform_root):
    import time

    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_capture_row(time.time(), 5, 2_000_018_000, 85.0)])
    quota_cli._run_import(_ns(platform_root=str(platform_root), history=str(history), host="dev"))

    program_root = tmp_path / "program_no_config"
    program_root.mkdir()  # no trialerror.toml here

    env = quota_cli._run_month(_ns(
        platform_root=str(platform_root), program_root=str(program_root), account_label="a",
        limit_usd=100.0, counts="all", month_start_day=1,
    ))
    assert env["ok"] is True
    assert "packet_item" not in env["result"]


def test_status_returns_the_composed_screen(tmp_path, platform_root):
    quota_dir = tmp_path / "quota"
    quota_dir.mkdir()
    env = quota_cli._run_status(_ns(
        platform_root=str(platform_root), program_root=None, account_label="a", quota_dir=str(quota_dir),
    ))
    assert env["ok"] is True
    for key in ("windows", "account", "rate", "month", "open_notices"):
        assert key in env["result"]


def test_status_has_a_plain_words_text_field_like_rate_show(tmp_path, platform_root):
    """Review N10: design Section 2.5 asks for "one screen of plain words";
    `rate show` already has a `text` field, so `status` needed one too."""
    quota_dir = tmp_path / "quota"
    quota_dir.mkdir()
    env = quota_cli._run_status(_ns(
        platform_root=str(platform_root), program_root=None, account_label="a", quota_dir=str(quota_dir),
    ))
    assert env["ok"] is True
    text = env["result"]["text"]
    assert "windows now:" in text
    assert "account:" in text
    assert "rate (five_hour):" in text and "rate (seven_day):" in text
    assert "month:" in text
    assert "open notices:" in text


def test_notify_with_no_notifier_configured_records_the_notice_as_unsent(tmp_path, platform_root, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(tmp_path / "probes"))
    from trialerror.hooks.probe_log import probes_dir
    from trialerror.quota.notify import LIMIT_HIT_FLAG_DIRNAME
    from trialerror.util.timeutil import now as now_ts

    flags = probes_dir() / LIMIT_HIT_FLAG_DIRNAME
    flags.mkdir(parents=True)
    (flags / "fresh_S1.flag").write_text(
        json.dumps({"ts": now_ts(), "session_id": "S1", "error_class": "rate_limit_error"}), encoding="utf-8",
    )

    program_root = tmp_path / "program_no_config"
    program_root.mkdir()
    env = quota_cli._run_notify(_ns(platform_root=str(platform_root), program_root=str(program_root), account_label="a"))
    assert env["ok"] is True
    result = env["result"]
    # E5: the flag is recorded and cleared with no push of its own -- L9's
    # combined credit-risk alert is what actually tries (and here fails,
    # with no sender configured) to push.
    assert result["processed"] == 1 and result["backlog_cleared"] == 0 and result["fresh_flags"] == 1
    assert result["credit_risk"]["fired"] == []
    assert result["credit_risk"]["failed"] == ["flag"]


def test_notify_pushes_through_a_configured_notify_cmd(tmp_path, platform_root, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(tmp_path / "probes"))
    from trialerror.hooks.probe_log import probes_dir
    from trialerror.quota.notify import LIMIT_HIT_FLAG_DIRNAME
    from trialerror.util.timeutil import now as now_ts

    flags = probes_dir() / LIMIT_HIT_FLAG_DIRNAME
    flags.mkdir(parents=True)
    (flags / "fresh_S1.flag").write_text(
        json.dumps({"ts": now_ts(), "session_id": "S1", "error_class": "rate_limit_error"}), encoding="utf-8",
    )

    marker = tmp_path / "pushed.txt"
    stub = tmp_path / "stub_notify.py"
    # E5: process_limit_hit_flags no longer pushes at all -- only L9's
    # combined credit-risk alert calls the notifier, so exactly one call is
    # expected (a regression back to a double push would still show here).
    stub.write_text(
        "import sys, pathlib\n"
        f"p = pathlib.Path(r'{marker}')\n"
        "prior = p.read_text(encoding='utf-8') if p.exists() else ''\n"
        "p.write_text(prior + '===\\n' + '|'.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    program_root = tmp_path / "program_with_config"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text(
        f'[program]\nid = "test"\n\n[packet]\nnotify_cmd = [{json.dumps(sys.executable)}, {json.dumps(str(stub))}]\n',
        encoding="utf-8",
    )

    env = quota_cli._run_notify(_ns(platform_root=str(platform_root), program_root=str(program_root), account_label="a"))
    assert env["ok"] is True
    result = env["result"]
    assert result["processed"] == 1 and result["backlog_cleared"] == 0 and result["fresh_flags"] == 1
    assert result["credit_risk"]["fired"] == ["flag"]
    assert marker.is_file()
    pushed_text = marker.read_text(encoding="utf-8")
    assert pushed_text.count("===") == 1  # only the credit-risk push actually called the notifier
    assert "Quota limit hit" not in pushed_text
    assert "Plan limit reached: work may be using the emergency reserve" in pushed_text


def test_s_b_the_packet_item_goes_to_the_programmes_own_store_with_no_packet_dir(tmp_path, platform_root, monkeypatch):
    """Review fix check S-b: E3 (amended) says the item goes to the
    programme's own packet store -- packet_settings(program_root) -- unless
    [quota] packet_dir overrides it. With no packet_dir at all (the
    sandbox's own case), the item must still land in <program>/packet/
    pending.jsonl and pass `packet add --strict`."""
    import json as _json

    from trialerror.packet.store import lint_item

    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(tmp_path / "probes"))
    from trialerror.hooks.probe_log import probes_dir
    from trialerror.quota.notify import LIMIT_HIT_FLAG_DIRNAME
    from trialerror.util.timeutil import now as now_ts

    flags = probes_dir() / LIMIT_HIT_FLAG_DIRNAME
    flags.mkdir(parents=True)
    (flags / "fresh_S1.flag").write_text(
        _json.dumps({"ts": now_ts(), "session_id": "S1", "error_class": "rate_limit_error"}), encoding="utf-8",
    )

    program_root = tmp_path / "program_no_packet_dir"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "test"\n', encoding="utf-8")

    env = quota_cli._run_notify(_ns(platform_root=str(platform_root), program_root=str(program_root), account_label="a"))
    assert env["ok"] is True
    result = env["result"]
    # no [packet] notify_cmd and no [packet] outbox here -- the push itself
    # is refused ("no sender configured"), but E3/E4 write the item at first
    # firing regardless of whether the push succeeds.
    assert result["credit_risk"]["failed"] == ["flag"]
    packet_items = result["credit_risk"]["packet_items"]
    assert len(packet_items) == 1 and "error" not in packet_items[0]

    pending = program_root / "packet" / "pending.jsonl"
    assert pending.is_file()
    rows = [_json.loads(ln) for ln in pending.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0]["what"] == "The plan's usage limit was reached on " + now_ts()[:10]
    assert lint_item(rows[0]) == []


def test_n_a_notify_still_refuses_a_harness_root_unlike_status_and_month(tmp_path, monkeypatch, platform_root):
    """Review fix check N-a: F1's own exempt list names `quota` read-only --
    status/month/forecast now pass refuse_harness=False (restored above, and
    the two prior tests' TRIALERROR_PROGRAM_ROOT workaround dropped with
    them). `notify` is the one verb that opens the packet/outbox store to
    route a real push, so it keeps the refusal."""
    import trialerror.util.config as config_mod
    from trialerror.util.config import ProgramRootIsHarnessError

    repo = tmp_path / "fake_checkout"
    (repo / "trialerror").mkdir(parents=True)
    (repo / "trialerror" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "trialerror.toml").write_text('[program]\nid = "fake"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "_HARNESS_PACKAGE_PARENT", repo)
    monkeypatch.delenv("TRIALERROR_PROGRAM_ROOT", raising=False)
    monkeypatch.chdir(repo)

    with pytest.raises(ProgramRootIsHarnessError):
        quota_cli._run_notify(_ns(platform_root=str(platform_root), program_root=None, account_label="a"))


# --- S5: CLI defaults must read TRIALERROR_ACCOUNT / TRIALERROR_QUOTA_DIR, not silently ignore them ----------------


def test_rate_fit_defaults_account_label_to_trialerror_account(monkeypatch):
    monkeypatch.setenv("TRIALERROR_ACCOUNT", "test-account")
    from trialerror.cli import build_parser

    ns = build_parser().parse_args(["quota", "rate", "fit"])
    assert ns.account_label == "test-account"


def test_rate_fit_defaults_account_label_to_empty_when_trialerror_account_unset(monkeypatch):
    monkeypatch.delenv("TRIALERROR_ACCOUNT", raising=False)
    from trialerror.cli import build_parser

    ns = build_parser().parse_args(["quota", "rate", "fit"])
    assert ns.account_label == ""


def test_status_and_forecast_and_notify_also_default_to_trialerror_account(monkeypatch):
    monkeypatch.setenv("TRIALERROR_ACCOUNT", "test-account")
    from trialerror.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["quota", "status"]).account_label == "test-account"
    assert parser.parse_args(["quota", "forecast", "--usd", "1"]).account_label == "test-account"
    assert parser.parse_args(["quota", "notify"]).account_label == "test-account"


def test_month_account_label_falls_back_to_trialerror_account_when_config_has_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_ACCOUNT", "test-account")
    cfg = quota_cli._monthly_config(_ns(
        account_label=None, limit_usd=None, counts=None, month_start_day=None, program_root=str(tmp_path),
    ))
    assert cfg["account_label"] == "test-account"


# --- S1: the headless-activity exclusion's real predicate -----------------------------------------------------------


def _unit_row(conn, *, unit_key, entrypoint, first_ts, last_ts):
    conn.execute(
        "INSERT INTO unit (unit_key, host, kind, session_id, project_slug, entrypoint, first_ts, last_ts, "
        "usage_source, extractor_version, scanned_ts) VALUES (?, 'dev', 'main', 'S', 'proj', ?, ?, ?, "
        "'transcript', 'units-1', '2026-09-27T00:00:00Z')",
        (unit_key, entrypoint, first_ts, last_ts),
    )
    conn.commit()


def test_headless_active_matches_claude_desktop_and_sdk_not_cli(platform_root):
    """Review S1: `cli` is the interactive terminal entrypoint (has a status
    line, per the review's real-transcript evidence) and must NOT exclude;
    `claude-desktop` (no status line) and `sdk-*` must."""
    conn = quota_cli._open_platform(_ns(platform_root=str(platform_root)))
    check = quota_cli._headless_active_fn(conn)
    window_start, window_end = 2_000_000_000, 2_000_018_000

    _unit_row(conn, unit_key="u-cli", entrypoint="cli", first_ts="2026-09-27T12:00:00Z", last_ts="2026-09-27T12:30:00Z")
    assert check(2_000_018_000, window_start, window_end) is False

    conn.execute("DELETE FROM unit")
    _unit_row(conn, unit_key="u-desktop", entrypoint="claude-desktop", first_ts="2026-09-27T12:00:00Z", last_ts="2026-09-27T12:30:00Z")
    # first_ts/last_ts are ISO wall-clock strings; window_start/end here are
    # epoch seconds that happen to land in 2033 -- align them for this check.
    import time

    start_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start))
    end_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_end))
    conn.execute("UPDATE unit SET first_ts = ?, last_ts = ?", (start_ts, end_ts))
    conn.commit()
    assert check(2_000_018_000, window_start, window_end) is True

    conn.execute("UPDATE unit SET entrypoint = 'sdk-python'")
    conn.commit()
    assert check(2_000_018_000, window_start, window_end) is True
    conn.close()


def test_headless_active_a_null_first_ts_excludes_nothing_not_everything(platform_root):
    """Review S1: `COALESCE(first_ts,'') <= end` made a NULL first_ts match
    every window ever (10 of 12 in the review's probe); a unit whose start
    is unknown must exclude nothing instead."""
    conn = quota_cli._open_platform(_ns(platform_root=str(platform_root)))
    check = quota_cli._headless_active_fn(conn)
    _unit_row(conn, unit_key="u-open", entrypoint="claude-desktop", first_ts=None, last_ts=None)

    assert check(1, 0, 4_000_000_000) is False  # a span that would swallow every window under the old query
    conn.close()


def test_headless_active_a_null_last_ts_is_treated_as_a_point_not_open_ended(platform_root):
    """Review S1: `last_ts IS NULL` (still open when scanned) must count as
    active only up to first_ts, not forever afterward."""
    conn = quota_cli._open_platform(_ns(platform_root=str(platform_root)))
    check = quota_cli._headless_active_fn(conn)
    _unit_row(conn, unit_key="u-openend", entrypoint="claude-desktop", first_ts="2026-09-27T12:00:00Z", last_ts=None)

    # a window whose span covers 2026-09-27T12:00:00Z: still active.
    import calendar
    import time

    first_epoch = calendar.timegm(time.strptime("2026-09-27T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    assert check(1, first_epoch - 100, first_epoch + 100) is True
    # a window entirely AFTER first_ts, with nothing pinning last_ts there
    # too: must not still count as active.
    assert check(1, first_epoch + 3600, first_epoch + 3700) is False
    conn.close()


def test_status_resolves_quota_dir_from_trialerror_quota_dir_env_var(tmp_path, platform_root, monkeypatch):
    """Review S5: on the sandbox, TRIALERROR_QUOTA_DIR=/workspace/platform/quota
    -- `status` must look there, not always at ~/.trialerror/quota."""
    real_quota_dir = tmp_path / "elsewhere" / "quota"
    real_quota_dir.mkdir(parents=True)
    (real_quota_dir / "accounts.json").write_text(
        json.dumps({"a": {"first_seen_ts": "2020-01-01T00:00:00Z", "last_seen_ts": "2020-01-01T00:00:00Z",
                           "n_rows": 10, "windows_seen": [1, 2, 3, 4]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRIALERROR_QUOTA_DIR", str(real_quota_dir))
    env = quota_cli._run_status(_ns(
        platform_root=str(platform_root), program_root=None, account_label="a", quota_dir=None,
    ))
    assert env["ok"] is True
    assert env["result"]["account"]["standing"] == "established"


# ---------------------------------------------------------------------------
# N6 (fix check): quota notify's local reading is labelled by --host /
# TRIALERROR_HOST / "local", never the literal "dev" (trap 5).
# ---------------------------------------------------------------------------


def test_notify_host_label_defaults_to_local(monkeypatch):
    monkeypatch.delenv("TRIALERROR_HOST", raising=False)
    assert quota_cli._notify_host_label(_ns(host=None)) == "local"


def test_notify_host_label_reads_trialerror_host_env(monkeypatch):
    monkeypatch.setenv("TRIALERROR_HOST", "sandbox")
    assert quota_cli._notify_host_label(_ns(host=None)) == "sandbox"


def test_notify_host_label_the_flag_wins_over_the_env_var(monkeypatch):
    monkeypatch.setenv("TRIALERROR_HOST", "sandbox")
    assert quota_cli._notify_host_label(_ns(host="dev")) == "dev"


def test_local_credit_risk_reading_labels_rows_with_the_given_host(tmp_path):
    history = tmp_path / "rate_limits.jsonl"
    _write_history(history, [_capture_row(NOW_EPOCH - 30, 40, NOW_EPOCH + 3600, 1.0, session="s1", account="a")])
    rd = quota_cli._local_credit_risk_reading(tmp_path, "a", NOW_EPOCH, "sandbox")
    assert rd is not None
    assert rd.src_host == "sandbox"


def test_notify_end_to_end_still_works_with_an_explicit_host(tmp_path, platform_root, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PROBES_DIR", str(tmp_path / "probes"))
    quota_dir = tmp_path / "quota"
    quota_dir.mkdir()
    history = quota_dir / "rate_limits.jsonl"
    _write_history(history, [_capture_row(NOW_EPOCH - 30, 40, NOW_EPOCH + 3600, 1.0, session="s1", account="a")])
    program_root = tmp_path / "program_no_config"
    program_root.mkdir()
    env = quota_cli._run_notify(
        _ns(platform_root=str(platform_root), program_root=str(program_root), account_label="a",
            quota_dir=str(quota_dir), host="sandbox")
    )
    assert env["ok"] is True
