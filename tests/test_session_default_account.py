"""Lane F-1 item G: ``[session] default_account``.

F14's rule is that account attribution is never GUESSED: with more than one
account registered and no ``--account`` given, boot refuses. That rule is
right, and it leaves one gap -- the SessionStart hook takes no arguments, so
on a program with two accounts it refused at every boot, every session,
forever.

The key closes it without weakening the rule: a written declaration, checked
against the register each time it is read. The refusal still stands for a
program that has not made one.
"""

from __future__ import annotations

from trialerror.sessions.lifecycle import DEFAULT_ACCOUNT_CONFIG_KEY, boot_session, resolve_account_for_boot
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _account(store, label: str) -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": label, "created_ts": now()})
    return account_id


def _write_key(program_root, account_id: str | None) -> None:
    body = '[program]\nid = "PROG-test"\n'
    if account_id is not None:
        body += f'\n[session]\ndefault_account = "{account_id}"\n'
    (program_root / "trialerror.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# the gap, and the key that closes it
# ---------------------------------------------------------------------------


def test_two_accounts_and_no_key_still_refuses(store, program_root):
    _account(store, "first")
    _account(store, "second")
    _write_key(program_root, None)
    resolution = resolve_account_for_boot(store)
    assert resolution.ok is False
    assert resolution.code == "account_required"
    assert DEFAULT_ACCOUNT_CONFIG_KEY in resolution.message, "the refusal names the way out"


def test_two_accounts_and_the_key_boots_on_the_named_one(store, program_root):
    _first = _account(store, "first")
    second = _account(store, "second")
    _write_key(program_root, second)
    resolution = resolve_account_for_boot(store)
    assert resolution.ok is True
    assert resolution.account_id == second
    assert resolution.code == "config_default"


def test_an_explicit_account_still_wins_over_the_key(store, program_root):
    first = _account(store, "first")
    second = _account(store, "second")
    _write_key(program_root, second)
    resolution = resolve_account_for_boot(store, account_id=first)
    assert resolution.account_id == first
    assert resolution.code == "given"


def test_a_key_naming_an_unknown_account_is_refused(store, program_root):
    _account(store, "first")
    _account(store, "second")
    _write_key(program_root, "ACC-does-not-exist")
    resolution = resolve_account_for_boot(store)
    assert resolution.ok is False
    assert resolution.code == "unknown_account"
    assert "ACC-does-not-exist" in resolution.message


def test_a_stale_key_never_bricks_a_single_account_program(store, program_root):
    """Lane F-1 item G as written: the key is consulted only when more than one
    account is registered (verifier V-6, orchestrator's call). A single-account
    program keeps its F14 default, and the stale key is named in the message so
    it is not quietly wrong either."""
    _account(store, "only")
    _write_key(program_root, "ACC-retired")
    resolution = resolve_account_for_boot(store)
    assert resolution.ok is True and resolution.code == "single_account_default"
    assert "ACC-retired" in resolution.message and "ignored" in resolution.message


def test_no_accounts_at_all_still_asks_for_the_bootstrap(store, program_root):
    _write_key(program_root, "ACC-anything")
    resolution = resolve_account_for_boot(store)
    assert resolution.code == "no_accounts"


def test_a_passed_config_is_used_instead_of_the_file(store, program_root):
    _account(store, "first")
    second = _account(store, "second")
    _write_key(program_root, None)
    resolution = resolve_account_for_boot(store, config={"session": {"default_account": second}})
    assert resolution.account_id == second and resolution.code == "config_default"


def test_a_malformed_config_file_is_no_key_rather_than_an_exception(store, program_root):
    _account(store, "only")
    (program_root / "trialerror.toml").write_text("this is not [ valid toml", encoding="utf-8")
    resolution = resolve_account_for_boot(store)
    assert resolution.ok is True and resolution.code == "single_account_default"


# ---------------------------------------------------------------------------
# through boot, which is where the hook meets it
# ---------------------------------------------------------------------------


def test_boot_with_no_arguments_inherits_the_key(store, program_root):
    """Exactly what the SessionStart hook does: ``boot_session(store,
    reuse_open=True)``, no config, no account."""
    _account(store, "first")
    second = _account(store, "second")
    _write_key(program_root, second)
    result = boot_session(store, reuse_open=True)
    assert result.ok is True
    assert result.account_id == second


def test_boot_with_no_arguments_and_no_key_refuses_as_before(store, program_root):
    _account(store, "first")
    _account(store, "second")
    _write_key(program_root, None)
    result = boot_session(store, reuse_open=True)
    assert result.ok is False and result.code == "account_required"
