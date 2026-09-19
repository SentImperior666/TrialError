"""``trialerror artifact`` / ``trialerror gate`` CLI surfaces (``trialerror/cli/artifact.py``,
``trialerror/cli/gate.py``) — argv parsing + AgentEnvelope wrapping around
``trialerror.artifacts.registry``/``trialerror.artifacts.gates``. Mirrors
``tests/test_budget_cli.py``'s and ``tests/test_cli_law.py``'s style: seed
prerequisite rows via a directly-opened+closed ``Store``, then drive
everything else through ``trialerror.cli.main``."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from trialerror.cli import discover_groups, main
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


@pytest.fixture()
def seeded(program_root, platform_root):
    """One account/session/launch, plus a gated 'keystone' and an ungated
    'note' template — everything every CLI test in this file needs, seeded
    once via a directly-opened Store and closed before the CLI opens its
    own connection to the same files (SQLite-WAL persists across
    connections; same pattern ``tests/test_budget_cli.py`` uses).

    Reuses ``tests/conftest.py``'s ``program_root``/``platform_root``
    fixtures rather than rolling isolated tmp paths locally: ``platform_root``
    binds ``TRIALERROR_PLATFORM_ROOT`` via ``monkeypatch`` (see conftest), which
    is what makes the CLI's OWN ``open_store(root)`` call — invoked with no
    explicit ``platform_root=`` kwarg, exactly like ``trialerror/cli/law.py``'s
    ``_open_store`` — resolve to the SAME isolated platform.db this fixture
    seeds into, instead of a real developer's ``~/.trialerror``."""
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store, "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-test",
            "session_id": session_id, "agent_kind": "tester", "model_class": "top", "model": "sonnet",
            "purpose": "fixture", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
        },
    )
    insert(store, "template", {"type_key": "keystone", "title": "Keystone", "version": "1", "path": "templates/keystone.md", "gated": 1})
    insert(store, "template", {"type_key": "note", "title": "Note", "version": "1", "path": "templates/note.md", "gated": 0})
    store.close()
    return platform_root, program_root, launch_id


def _pr(program_root) -> list[str]:
    return ["--program-root", str(program_root)]


# ---- discovery / no-action --------------------------------------------------


def test_artifact_and_gate_groups_discovered():
    names = {getattr(m, "GROUP_NAME", None) for m in discover_groups()}
    assert {"artifact", "gate"} <= names


def test_artifact_no_action_is_a_structured_error(program_root, platform_root):
    rc, env = _run_cli(["artifact", *_pr(program_root)])
    assert rc == 1
    assert env["error"]["code"] == "no_action"


def test_gate_no_action_is_a_structured_error(program_root, platform_root):
    rc, env = _run_cli(["gate", *_pr(program_root)])
    assert rc == 1
    assert env["error"]["code"] == "no_action"


def test_artifact_program_root_not_found(tmp_path, monkeypatch):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc, env = _run_cli(["artifact", "list"])
    assert rc == 1
    assert env["error"]["code"] == "program_root_not_found"


# ---- artifact create / list / show -----------------------------------------


def test_artifact_create_ok_envelope(seeded):
    _platform_root, program_root, launch_id = seeded
    rc, env = _run_cli(
        [
            "artifact", "create", *_pr(program_root),
            "--type", "note", "--title", "My Note", "--path", "artifacts/n.md", "--sha256", "0" * 64,
            "--by-launch", launch_id, "--domain", "ingest",
        ]
    )
    assert rc == 0
    assert env["result"]["status"] == "draft"
    assert env["nextActions"][0]["argv"][:2] == ["trialerror", "gate"]


def test_artifact_create_refused_on_unknown_type(seeded):
    _platform_root, program_root, launch_id = seeded
    rc, env = _run_cli(
        [
            "artifact", "create", *_pr(program_root),
            "--type", "no-such-type", "--title", "x", "--path", "p", "--sha256", "0" * 64, "--by-launch", launch_id,
        ]
    )
    assert rc == 1
    assert env["error"]["code"] == "create_refused"


def test_artifact_list_and_show(seeded):
    _platform_root, program_root, launch_id = seeded
    _rc, create_env = _run_cli(
        ["artifact", "create", *_pr(program_root), "--type", "note", "--title", "n", "--path", "p", "--sha256", "0" * 64, "--by-launch", launch_id]
    )
    artifact_id = create_env["result"]["artifact_id"]

    rc, env = _run_cli(["artifact", "list", *_pr(program_root)])
    assert rc == 0
    assert env["result"]["count"] == 1

    rc, env = _run_cli(["artifact", "show", *_pr(program_root), "--id", artifact_id])
    assert rc == 0
    assert env["result"]["artifact_id"] == artifact_id

    rc, env = _run_cli(["artifact", "show", *_pr(program_root), "--id", "ART-nope"])
    assert rc == 1
    assert env["error"]["code"] == "not_found"


# ---- register: refusal + success -------------------------------------------


def test_artifact_register_refused_for_gated_type_without_gate(seeded):
    _platform_root, program_root, launch_id = seeded
    _rc, create_env = _run_cli(
        ["artifact", "create", *_pr(program_root), "--type", "keystone", "--title", "k", "--path", "p", "--sha256", "0" * 64, "--by-launch", launch_id]
    )
    artifact_id = create_env["result"]["artifact_id"]

    rc, env = _run_cli(["artifact", "register", *_pr(program_root), "--id", artifact_id, "--by-launch", launch_id])
    assert rc == 1
    assert env["error"]["code"] == "registration_refused"


def test_artifact_register_ungated_succeeds(seeded):
    _platform_root, program_root, launch_id = seeded
    _rc, create_env = _run_cli(
        ["artifact", "create", *_pr(program_root), "--type", "note", "--title", "n", "--path", "p", "--sha256", "0" * 64, "--by-launch", launch_id]
    )
    artifact_id = create_env["result"]["artifact_id"]

    rc, env = _run_cli(["artifact", "register", *_pr(program_root), "--id", artifact_id, "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["status"] == "registered"


# ---- full gate lifecycle through the CLI -----------------------------------


def test_full_gate_lifecycle_through_the_cli_ends_in_registration(seeded):
    _platform_root, program_root, launch_id = seeded
    pr = _pr(program_root)
    _rc, create_env = _run_cli(
        ["artifact", "create", *pr, "--type", "keystone", "--title", "k", "--path", "p", "--sha256", "0" * 64, "--by-launch", launch_id]
    )
    artifact_id = create_env["result"]["artifact_id"]

    rc, env = _run_cli(["gate", "open", *pr, "--artifact-id", artifact_id])
    assert rc == 0
    gate_id = env["result"]["gate_id"]
    assert env["result"]["state"] == "draft"

    rc, env = _run_cli(["gate", "submit", *pr, "--id", gate_id, "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["state"] == "submitted"

    edits = json.dumps([{"text": "fix the header", "blocking": True}])
    rc, env = _run_cli(
        [
            "gate", "verdict", *pr, "--id", gate_id, "--verdict", "PASS_WITH_EDITS",
            "--critic-launch", launch_id, "--edits", edits,
        ]
    )
    assert rc == 0
    assert env["result"]["state"] == "gated"
    edit_id = json.loads(env["result"]["edits"])[0]["edit_id"]

    # apply-union refused: the blocking edit isn't verified yet
    rc, env = _run_cli(["gate", "apply-union", *pr, "--id", gate_id, "--by-launch", launch_id])
    assert rc == 1
    assert env["error"]["code"] == "entry_condition_failed"

    rc, env = _run_cli(
        ["gate", "verify-edit", *pr, "--id", gate_id, "--edit-id", edit_id, "--by-launch", launch_id, "--verified-note", "fixed"]
    )
    assert rc == 0
    assert json.loads(env["result"]["edits"])[0]["verified"] is True

    rc, env = _run_cli(["gate", "apply-union", *pr, "--id", gate_id, "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["state"] == "union_applied"
    assert env["nextActions"][0]["argv"][:3] == ["trialerror", "artifact", "register"]

    rc, env = _run_cli(["artifact", "register", *pr, "--id", artifact_id, "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["status"] == "registered"


def test_gate_advance_refuses_illegal_transition_through_the_cli(seeded):
    _platform_root, program_root, launch_id = seeded
    pr = _pr(program_root)
    _rc, create_env = _run_cli(
        ["artifact", "create", *pr, "--type", "keystone", "--title", "k", "--path", "p", "--sha256", "0" * 64, "--by-launch", launch_id]
    )
    artifact_id = create_env["result"]["artifact_id"]
    _rc, open_env = _run_cli(["gate", "open", *pr, "--artifact-id", artifact_id])
    gate_id = open_env["result"]["gate_id"]

    rc, env = _run_cli(["gate", "advance", *pr, "--id", gate_id, "--to", "registered", "--by-launch", launch_id])
    assert rc == 1
    assert env["error"]["code"] == "transition_refused"


# ---- FX-9: `trialerror artifact templates` --------------------------------------


def test_artifact_templates_lists_12_builtins_unseeded(program_root, platform_root):
    rc, env = _run_cli(["artifact", "templates", *_pr(program_root)])
    assert rc == 0
    assert env["result"]["count"] == 12
    assert env["result"]["seeded_count"] == 0
    assert all(t["registered"] is False for t in env["result"]["templates"])
    # unseeded listing nudges the operator toward --seed.
    assert env["nextActions"][0]["argv"] == ["trialerror", "artifact", "templates", "--seed"]


def test_artifact_templates_seed_is_idempotent_through_the_cli(program_root, platform_root):
    rc, env = _run_cli(["artifact", "templates", "--seed", *_pr(program_root)])
    assert rc == 0
    assert env["result"]["seeded_count"] == 12
    assert all(t["registered"] is True for t in env["result"]["templates"])
    assert env["nextActions"] == []

    # second --seed: every row already exists, nothing new inserted.
    rc, env = _run_cli(["artifact", "templates", "--seed", *_pr(program_root)])
    assert rc == 0
    assert env["result"]["seeded_count"] == 0
    assert env["result"]["count"] == 12


def test_artifact_templates_seeded_type_key_is_immediately_usable_for_create(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store, "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-test",
            "session_id": session_id, "agent_kind": "tester", "model_class": "top", "model": "sonnet",
            "purpose": "fixture", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
        },
    )
    store.close()
    pr = _pr(program_root)

    rc, _env = _run_cli(["artifact", "templates", "--seed", *pr])
    assert rc == 0

    rc, env = _run_cli(
        ["artifact", "create", *pr, "--type", "methods-note", "--title", "t", "--path", "p", "--sha256", "0" * 64, "--by-launch", launch_id]
    )
    assert rc == 0
    assert env["result"]["type"] == "methods-note"
