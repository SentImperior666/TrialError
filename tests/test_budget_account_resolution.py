"""Lane FB-1 item F7: the account is knowable, and the binding limit is named.

``budget status`` no longer demands an account id the harness already knows
(``session.account_id``, bound at boot), the envelope carries headroom in the
unit a booking is written in, and ``budget check`` puts the pool and the
plan's own quota windows in one envelope.
"""

from __future__ import annotations

import json

from trialerror.budget.pools import book_launch, budget_status, create_pool
from trialerror.cli import budget as cli_budget
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._budget_fixtures import open_account_session


class _Args:
    def __init__(self, program_root, platform_root, **kw):
        self.program_root = str(program_root)
        self.platform_root = str(platform_root)
        self.account_id = None
        self.model_class = None
        self.quota_dir = None
        self.fresh_within_s = None
        for k, v in kw.items():
            setattr(self, k, v)


def _pool(store, account_id, *, cap=1000, multiplier=2.0, soft=90.0, hard=100.0, model_class="mid"):
    return create_pool(
        store,
        account_id=account_id,
        model_class=model_class,
        period="weekly",
        cap_tokens=cap,
        billed_multiplier=multiplier,
        soft_pct=soft,
        hard_pct=hard,
    )


# ---------------------------------------------------------------------------
# --account-id resolution
# ---------------------------------------------------------------------------


def test_status_without_a_flag_reads_the_open_sessions_account(store, program_root, platform_root):
    account_id, _session_id = open_account_session(store)
    _pool(store, account_id)
    store.close()

    env = cli_budget._run_status(_Args(program_root, platform_root))
    assert env["ok"] is True
    assert env["result"]["account_id"] == account_id
    assert env["result"]["account_resolved_from"] == "open session"


def test_status_with_the_flag_still_wins(store, program_root, platform_root):
    account_id, _ = open_account_session(store)
    other = new_id("ACC")
    insert(store, "account", {"account_id": other, "label": "second", "created_ts": now()})
    store.close()

    env = cli_budget._run_status(_Args(program_root, platform_root, account_id=other))
    assert env["result"]["account_id"] == other
    assert env["result"]["account_resolved_from"] == "--account-id"


def test_status_with_no_session_refuses_and_names_both_ways_out(store, program_root, platform_root):
    account_id, session_id = open_account_session(store, status="closed")
    store.close()

    env = cli_budget._run_status(_Args(program_root, platform_root))
    assert env["ok"] is False
    assert env["error"]["code"] == "no_open_session"
    assert "--account-id" in env["error"]["message"]
    assert "session boot" in env["error"]["message"]
    assert env["nextActions"][0]["argv"][:3] == ["trialerror", "session", "boot"]


def test_status_with_two_open_sessions_surfaces_the_refusal_rather_than_picking(
    store, program_root, platform_root
):
    account_id, first = open_account_session(store)
    second = new_id("SESS")
    insert(
        store,
        "session",
        {"session_id": second, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )
    store.close()

    env = cli_budget._run_status(_Args(program_root, platform_root))
    assert env["ok"] is False
    assert env["error"]["code"] == "multiple_open_sessions"
    assert "--account-id" in env["error"]["message"]


# ---------------------------------------------------------------------------
# the two new envelope keys
# ---------------------------------------------------------------------------


def test_visible_headroom_to_soft_is_the_soft_gap_divided_by_the_multiplier(store):
    account_id, session_id = open_account_session(store)
    _pool(store, account_id, cap=1000, multiplier=2.0, soft=90.0)
    result = budget_status(store, account_id=account_id)
    pool = result["pools"][0]
    # nothing spent: projected 0, soft cap 900 billed -> 450 visible tokens.
    assert pool["soft_cap"] == 900.0
    assert pool["visible_headroom_to_soft"] == 450.0
    assert pool["binding_limit"] == "soft"


def test_the_soft_headroom_floors_at_zero_once_the_soft_line_is_crossed(store):
    account_id, session_id = open_account_session(store)
    _pool(store, account_id, cap=1000, multiplier=2.0, soft=50.0, hard=200.0)
    book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=400,
    )
    pool = budget_status(store, account_id=account_id)["pools"][0]
    assert pool["over_soft"] is True
    assert pool["visible_headroom_to_soft"] == 0.0
    assert pool["binding_limit"] == "hard"
    # the hard cap is 2000 billed; 800 billed is committed -> 600 visible left
    assert pool["visible_headroom_to_binding_limit"] == 600.0


def test_binding_limit_names_the_tightest_pool_of_the_account(store):
    account_id, session_id = open_account_session(store)
    _pool(store, account_id, cap=1000, multiplier=2.0, model_class="mid")
    _pool(store, account_id, cap=100, multiplier=2.0, model_class="top")
    result = budget_status(store, account_id=account_id)
    assert result["binding_limit"]["model_class"] == "top"
    assert result["binding_limit"]["limit"] == "soft"
    assert result["binding_limit"]["visible_headroom_tokens"] == 45.0


def test_an_account_with_no_pool_reports_no_binding_limit_rather_than_a_number(store):
    account_id, session_id = open_account_session(store)
    result = budget_status(store, account_id=account_id)
    assert result["pools"] == []
    assert result["binding_limit"] is None


# ---------------------------------------------------------------------------
# budget check
# ---------------------------------------------------------------------------


def test_check_prints_both_halves_and_names_the_binding_limit(store, program_root, platform_root, tmp_path):
    account_id, session_id = open_account_session(store)
    _pool(store, account_id, cap=1000, multiplier=2.0)
    store.close()

    env = cli_budget._run_check(_Args(program_root, platform_root, quota_dir=str(tmp_path / "quota")))
    assert env["ok"] is True
    assert env["result"]["status"]["account_id"] == account_id
    assert "quota" in env["result"]
    assert "binding limit is the soft cap" in env["result"]["summary"]
    assert "450 visible tokens of headroom" in env["result"]["summary"]


def test_check_degrades_to_the_documented_unavailable_shape_with_no_capture(
    store, program_root, platform_root, tmp_path
):
    account_id, session_id = open_account_session(store)
    _pool(store, account_id)
    store.close()

    env = cli_budget._run_check(_Args(program_root, platform_root, quota_dir=str(tmp_path / "nothing-here")))
    assert env["ok"] is True
    quota = env["result"]["quota"]
    assert quota["available"] is False and quota["fresh"] is False
    assert quota["windows"] == {}
    assert "note" in quota
    assert "no plan-quota capture" in env["result"]["summary"]


def test_check_reads_a_real_capture_when_one_exists(store, program_root, platform_root, tmp_path):
    account_id, session_id = open_account_session(store)
    _pool(store, account_id)
    store.close()

    import time

    quota_dir = tmp_path / "quota"
    quota_dir.mkdir()
    (quota_dir / "latest.json").write_text(
        json.dumps(
            {
                "epoch": time.time(),
                "captured_ts": now(),
                "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": "later"}},
            }
        ),
        encoding="utf-8",
    )
    env = cli_budget._run_check(_Args(program_root, platform_root, quota_dir=str(quota_dir)))
    assert env["result"]["quota"]["available"] is True
    assert env["result"]["quota"]["windows"]["five_hour"]["used_percentage"] == 42
    assert "no plan-quota capture" not in env["result"]["summary"]


def test_check_adds_no_arithmetic_of_its_own(store, program_root, platform_root, tmp_path):
    """Composition, not a second implementation: the ``status`` half of
    ``check`` must be byte-identical to what ``status`` itself reports."""
    account_id, session_id = open_account_session(store)
    _pool(store, account_id)
    store.close()

    status_env = cli_budget._run_status(_Args(program_root, platform_root))
    check_env = cli_budget._run_check(_Args(program_root, platform_root, quota_dir=str(tmp_path / "q")))
    assert check_env["result"]["status"] == status_env["result"]


def test_check_inherits_the_same_account_refusals(store, program_root, platform_root, tmp_path):
    open_account_session(store, status="closed")
    store.close()
    env = cli_budget._run_check(_Args(program_root, platform_root, quota_dir=str(tmp_path / "q")))
    assert env["ok"] is False
    assert env["error"]["code"] == "no_open_session"
