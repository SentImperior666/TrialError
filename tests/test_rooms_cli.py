"""``trialerror room`` CLI surface (``trialerror/cli/room.py``) — argv parsing +
AgentEnvelope wrapping around ``trialerror.rooms.api``. Mirrors
``tests/test_artifacts_cli.py``'s style: seed prerequisite rows via a
directly-opened+closed ``Store``, then drive everything else through
``trialerror.cli.main``.
"""

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
    """One account/session/launch + a 'room_theory_doc' template — closed
    before the CLI opens its own connection to the same WAL files (same
    pattern ``tests/test_artifacts_cli.py``'s ``seeded`` fixture uses)."""
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
    insert(store, "template", {"type_key": "room_theory_doc", "title": "Room Theory Doc", "version": "1", "path": "templates/room_theory_doc.md", "gated": 0})
    store.close()
    return platform_root, program_root, launch_id


def _pr(program_root) -> list[str]:
    return ["--program-root", str(program_root)]


def test_room_group_discovered():
    names = {getattr(m, "GROUP_NAME", None) for m in discover_groups()}
    assert "room" in names


def test_room_no_action_is_a_structured_error(program_root, platform_root):
    rc, env = _run_cli(["room", *_pr(program_root)])
    assert rc == 1
    assert env["error"]["code"] == "no_action"


def test_room_program_root_not_found(tmp_path, monkeypatch):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc, env = _run_cli(["room", "status", "--id", "ROOM-x"])
    assert rc == 1
    assert env["error"]["code"] == "program_root_not_found"


def _create_room(program_root, *, dps=None, participants="P1,P2") -> str:
    dps = dps or [{"prompt": "does it generalize?"}, {"prompt": "does it survive a hostile edit?"}]
    rc, env = _run_cli(
        ["room", "create", *_pr(program_root), "--topic", "cli room", "--dps", json.dumps(dps), "--participants", participants]
    )
    assert rc == 0, env
    return env["result"]["room_id"]


def test_room_create_ok_envelope(seeded):
    _platform_root, program_root, _launch_id = seeded
    room_id = _create_room(program_root)
    assert room_id.startswith("ROOM-")


def test_room_create_bad_participant_count_refused(seeded):
    _platform_root, program_root, _launch_id = seeded
    rc, env = _run_cli(
        ["room", "create", *_pr(program_root), "--topic", "t", "--dps", json.dumps([{"prompt": "p"}]), "--participants", "P1,P2,P3,P4"]
    )
    assert rc == 1
    assert env["error"]["code"] == "create_refused"


def test_room_status_not_found(seeded):
    _platform_root, program_root, _launch_id = seeded
    rc, env = _run_cli(["room", "status", *_pr(program_root), "--id", "ROOM-bogus"])
    assert rc == 1
    assert env["error"]["code"] == "not_found"


def test_room_full_cli_lifecycle(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)

    rc, env = _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body", "opening turn"])
    assert rc == 0, env
    assert env["result"]["seq"] == 1

    rc, env = _run_cli(["room", "status", *_pr(program_root), "--id", room_id])
    assert rc == 0
    assert env["result"]["turn_count"] == 1

    rc, env = _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--agreement-pct", "95", "--by-launch", launch_id])
    assert rc == 0, env
    assert env["result"]["converged"] is True

    rc, env = _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP2", "--agreement-pct", "95", "--by-launch", launch_id])
    assert rc == 0

    rc, env = _run_cli(["room", "converge-check", *_pr(program_root), "--id", room_id])
    assert rc == 0
    assert env["result"]["applied"] is False
    assert env["result"]["convergence"]["all_converged"] is True

    rc, env = _run_cli(["room", "converge-check", *_pr(program_root), "--id", room_id, "--apply", "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["applied"] is True
    assert env["result"]["room"]["state"] == "converged"

    rc, env = _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body", "too late"])
    assert rc == 1
    assert env["error"]["code"] == "post_refused"


def test_room_freeze_via_cli(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    rc, env = _run_cli(["room", "freeze", *_pr(program_root), "--id", room_id, "--reason", "deadlocked", "--by-launch", launch_id])
    assert rc == 0
    assert env["result"]["state"] == "frozen"


def test_room_export_via_cli(seeded, tmp_path):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body", "exported turn"])
    out_path = tmp_path / "doc.md"
    rc, env = _run_cli(["room", "export", *_pr(program_root), "--id", room_id, "--out", str(out_path)])
    assert rc == 0, env
    assert out_path.is_file()
    assert "exported turn" in out_path.read_text(encoding="utf-8")


def test_room_post_body_file(seeded, tmp_path):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    body_file = tmp_path / "body.txt"
    body_file.write_text("turn from a file", encoding="utf-8")
    rc, env = _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body-file", str(body_file)])
    assert rc == 0, env
    assert env["result"]["body"] == "turn from a file"


def test_room_converge_check_apply_without_by_launch_refused(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--agreement-pct", "95", "--by-launch", launch_id])
    _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP2", "--agreement-pct", "95", "--by-launch", launch_id])
    rc, env = _run_cli(["room", "converge-check", *_pr(program_root), "--id", room_id, "--apply"])
    assert rc == 1
    assert env["error"]["code"] == "by_launch_required"


# ---------------------------------------------------------------------------
# the framework procedure, from the CLI (stage C)
# ---------------------------------------------------------------------------


def _lens_launch_row(program_root, platform_root, *, lens_name: str) -> str:
    """A launch whose ``attrs`` declare a lens name, the way ``lens export``
    books one -- the seam the by-name NEITHER check reads."""
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
            "session_id": session_id, "agent_kind": "lens", "model_class": "top", "model": "sonnet",
            "purpose": "ideation", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
            "attrs": json.dumps({"lens_name": lens_name}),
        },
    )
    store.close()
    return launch_id


def _seed_consolidated_ideas(program_root, platform_root, *, launch_id: str, n: int = 4) -> list[str]:
    store = open_store(program_root, platform_root=platform_root)
    ids = []
    for i in range(n):
        idea_id = new_id("IDEA")
        insert(
            store, "idea",
            {
                "idea_id": idea_id, "round_id": "ROUND-cli", "author_launch": launch_id, "body": f"idea {i}",
                "status": "consolidated", "created_ts": now(),
                "tier": ("near", "far")[i % 2], "recipe_card": ("MISMATCH", "TRANSFER")[i % 2],
            },
        )
        ids.append(idea_id)
    store.close()
    return ids


def test_room_post_carries_a_turn_kind_and_refuses_closure_in_round_one(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    rc, env = _run_cli(
        ["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1",
         "--body", "q?", "--kind", "question"]
    )
    assert rc == 0, env
    assert env["result"]["kind"] == "question"

    rc, env = _run_cli(
        ["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP2",
         "--body", "done", "--kind", "closure"]
    )
    assert rc == 1
    assert env["error"]["code"] == "post_refused"
    assert "round 1" in env["error"]["message"]


def test_room_score_needs_a_label_or_a_number(seeded):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    rc, env = _run_cli(["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--by-launch", launch_id])
    assert rc == 1
    assert env["error"]["code"] == "nothing_to_record"


def test_room_stance_then_score_computes_the_number_and_refuses_a_supplied_one(seeded, tmp_path):
    platform_root, program_root, launch_id = seeded
    a1 = _lens_launch_row(program_root, platform_root, lens_name="lens_a")
    b1 = _lens_launch_row(program_root, platform_root, lens_name="lens_b")
    rc, env = _run_cli(
        ["room", "create", *_pr(program_root), "--topic", "cli framework room",
         "--dps", json.dumps([{"prompt": "p1"}, {"prompt": "p2"}]),
         "--participants", "lens_a,lens_b", "--rank-all", "--blind-first-turn", "--buster", "lens_b"]
    )
    assert rc == 0, env
    room_id = env["result"]["room_id"]

    stance_file = tmp_path / "stance.json"
    stance_file.write_text(
        json.dumps({
            "ranking": {"a": ["DP1", "DP2"], "b": ["DP1", "DP2"]},
            "stances": {"DP1": {"a": "yes", "b": "yes"}, "DP2": {"a": "yes", "b": "no"}},
        }),
        encoding="utf-8",
    )
    for participant, launch in (("lens_a", a1), ("lens_b", b1)):
        rc, env = _run_cli(
            ["room", "stance", *_pr(program_root), "--id", room_id, "--launch-id", launch,
             "--participant", participant, "--file", str(stance_file)]
        )
        assert rc == 0, env

    rc, env = _run_cli(
        ["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--label", "MEETS-BOTH",
         "--by-launch", launch_id]
    )
    assert rc == 0, env
    assert env["result"]["label"] == "MEETS-BOTH"
    assert env["result"]["agreement_pct"] == 100.0
    assert env["result"]["agreement_from"] == "structured_stances"

    rc, env = _run_cli(
        ["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP2", "--label", "FAILS",
         "--agreement-pct", "99", "--by-launch", launch_id]
    )
    assert rc == 1
    assert env["error"]["code"] == "score_refused"
    assert "not the judge's to supply" in env["error"]["message"]


def test_room_extracts_then_require_extracts_scoring(seeded, tmp_path):
    _platform_root, program_root, launch_id = seeded
    room_id = _create_room(program_root)
    _run_cli(["room", "post", *_pr(program_root), "--id", room_id, "--launch-id", launch_id, "--dp", "DP1", "--body", "p"])
    extracts_file = tmp_path / "extracts.json"
    extracts_file.write_text(
        json.dumps([{
            "seq": 1, "claim": "c", "anchors": ["DOC-1"], "stance_a": "yes", "stance_b": "unclear",
            "residual_disagreement": "none",
        }]),
        encoding="utf-8",
    )
    rc, env = _run_cli(
        ["room", "extracts", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--file", str(extracts_file),
         "--by-launch", launch_id]
    )
    assert rc == 0, env

    rc, env = _run_cli(
        ["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP1", "--agreement-pct", "95",
         "--by-launch", launch_id, "--require-extracts"]
    )
    assert rc == 0, env

    rc, env = _run_cli(
        ["room", "score", *_pr(program_root), "--id", room_id, "--dp", "DP2", "--agreement-pct", "95",
         "--by-launch", launch_id, "--require-extracts"]
    )
    assert rc == 1
    assert env["error"]["code"] == "score_refused"


def test_room_admission_order_from_the_rounds_consolidated_ideas(seeded):
    platform_root, program_root, launch_id = seeded
    ids = _seed_consolidated_ideas(program_root, platform_root, launch_id=launch_id, n=4)
    rc, env = _run_cli(
        ["room", "admission-order", *_pr(program_root), "--round-id", "ROUND-cli", "--seed", "seed-1",
         "--ideas-per-room", "2", "--rooms-per-batch", "1", "--no-enforce-batch-band"]
    )
    assert rc == 0, env
    assert sorted(env["result"]["order"]) == sorted(ids)
    assert len(env["result"]["hash"]) == 64
    assert len(env["result"]["rooms"]) == 2


def test_room_admission_order_with_nothing_consolidated(seeded):
    _platform_root, program_root, _launch_id = seeded
    rc, env = _run_cli(
        ["room", "admission-order", *_pr(program_root), "--round-id", "ROUND-empty", "--seed", "s"]
    )
    assert rc == 1
    assert env["error"]["code"] == "no_consolidated_ideas"


def test_room_admission_order_outside_the_charter_band(seeded):
    platform_root, program_root, launch_id = seeded
    _seed_consolidated_ideas(program_root, platform_root, launch_id=launch_id, n=4)
    rc, env = _run_cli(
        ["room", "admission-order", *_pr(program_root), "--round-id", "ROUND-cli", "--seed", "s",
         "--rooms-per-batch", "99"]
    )
    assert rc == 1
    assert env["error"]["code"] == "admission_order_refused"
