"""``trialerror verify`` / ``trialerror prereg`` CLI groups -- argv parsing, envelope
shaping, and auto-discovery, driven end to end through ``trialerror.cli.main``
(same convention ``tests/test_cli_law.py`` uses).

**Argument ordering note:** ``--program-root``/``--platform-root`` are
registered on both the group parser and every action subparser (the
``trialerror/cli/law.py`` convention, replicated in ``trialerror/cli/verify.py``/
``trialerror/cli/prereg.py``) -- but argparse's ``_SubParsersAction`` parses the
trailing args into a FRESH namespace and merges it back over the parent's,
so a value given BEFORE the action name is silently overwritten by the
action subparser's own (unset) default. Every test below therefore places
``--program-root``/``--platform-root`` AFTER the action token, exactly as
``tests/test_cli_law.py`` already does for every one of its own real
(non-``no_action``) invocations -- the group-level registration exists
only so the ``no_action`` case (no subparser ever runs) can still resolve
``--program-root``.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from trialerror.cli import main
from trialerror.cli import prereg as cli_prereg
from trialerror.cli import verify as cli_verify
from trialerror.verify.verdicts import record_verdict

from tests._verify_fixtures import bootstrap_launch, build_small_corpus

_MATCHING_SENTENCE = "Distributed schedulers use retry budgets to bound tail latency during failover."
_MISMATCHED_SENTENCE = "Combat resolves through opposed rolls in this alternate system entirely."


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_group_names_and_help_registered():
    assert cli_verify.GROUP_NAME == "verify"
    assert cli_verify.HELP
    assert cli_prereg.GROUP_NAME == "prereg"
    assert cli_prereg.HELP


def test_verify_no_action_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(["verify", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "no_action"


def test_prereg_no_action_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(["prereg", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "no_action"


# ---------------------------------------------------------------------------
# prereg: commit / status / reveal
# ---------------------------------------------------------------------------


def test_prereg_commit_status_reveal_round_trip(program_root, platform_root, capsys):
    rc, env = _call(
        [
            "prereg", "commit", "--title", "cli test", "--procedure", "the real procedure",
            "--params", json.dumps({"k": 1}), "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    prereg_id = env["result"]["prereg_id"]
    assert env["result"]["status"] == "committed"

    rc, env = _call(["prereg", "status", "--id", prereg_id, "--program-root", str(program_root), "--platform-root", str(platform_root)], capsys)
    assert rc == 0
    assert env["result"]["status"] == "committed"

    rc, env = _call(["prereg", "reveal", "--id", prereg_id, "--program-root", str(program_root), "--platform-root", str(platform_root)], capsys)
    assert rc == 0
    assert env["result"]["status"] == "revealed"
    assert env["result"]["procedure"] == "the real procedure"


def test_prereg_commit_blank_procedure_is_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        ["prereg", "commit", "--title", "t", "--procedure", "   ", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "invalid_procedure"


def test_prereg_status_not_found_is_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        ["prereg", "status", "--id", "PREG-does-not-exist", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# verify citecheck
# ---------------------------------------------------------------------------


def test_verify_citecheck_over_a_text_file_mechanical_pass(store, program_root, platform_root, capsys):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)).fetchone())
    store.close()

    text_file = program_root / "artifact.md"
    text_file.write_text(f"{_MATCHING_SENTENCE} [[cite:{anchor['anchor_id']}]]", encoding="utf-8")

    rc, env = _call(
        ["verify", "citecheck", str(text_file), "--by-launch", launch_id, "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 0
    assert env["result"]["summary"]["overall"] == "PASS"
    assert env["result"]["summary"]["mechanical_pass"] == 1


def test_verify_citecheck_with_judgments_file_resolves_escalation(store, program_root, platform_root, capsys):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    anchor = dict(store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)).fetchone())
    store.close()

    text_file = program_root / "artifact2.md"
    text_file.write_text(f"{_MISMATCHED_SENTENCE} [[cite:{anchor['anchor_id']}]]", encoding="utf-8")
    judgments_file = program_root / "judgments.json"
    judgments_file.write_text(json.dumps({"CPR-1": {"label": "supported", "note": "agent confirmed"}}), encoding="utf-8")

    rc, env = _call(
        [
            "verify", "citecheck", str(text_file), "--by-launch", launch_id, "--sample-rate", "1",
            "--judgments-file", str(judgments_file), "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["pairs"][0]["status"] == "llm_pass"
    assert env["result"]["summary"]["overall"] == "PASS"


def test_verify_citecheck_unresolvable_subject_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        ["verify", "citecheck", "does-not-exist-anywhere", "--by-launch", "LNCH-x", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "citecheck_refused"


# ---------------------------------------------------------------------------
# verify hypothesis
# ---------------------------------------------------------------------------


def test_verify_hypothesis_with_judgments_file_records_a_verdict(store, program_root, platform_root, capsys):
    launch_id = bootstrap_launch(store)
    corpus = build_small_corpus(store, launch_id=launch_id)
    store.close()

    judgments = {cid: {"label": "explicit agreement"} for cid in (corpus["open_chunk_ids"][0], corpus["restricted_chunk_ids"][0])}
    judgments_file = program_root / "hyp_judgments.json"
    judgments_file.write_text(json.dumps(judgments), encoding="utf-8")

    rc, env = _call(
        [
            "verify", "hypothesis", "--text", "rulebooks describe combat and dice mechanics", "--query", "dice combat rules",
            "--by-launch", launch_id, "--mode", "vector", "--judgments-file", str(judgments_file),
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "supported"
    assert env["result"]["verdict"]["procedure"] == "contracrow"


def test_verify_hypothesis_requires_id_or_text(program_root, platform_root, capsys):
    # argparse's mutually-exclusive-group "required" enforcement raises
    # SystemExit(2) (via parser.error()) before our own handler ever runs --
    # confirm the parser itself refuses, distinct from our own error envelope.
    with pytest.raises(SystemExit) as exc_info:
        main(["verify", "hypothesis", "--by-launch", "LNCH-x", "--judgments-file", "x.json", "--program-root", str(program_root)])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# verify reproduce
# ---------------------------------------------------------------------------


def test_verify_reproduce_over_cli(store, program_root, platform_root, tmp_path, capsys):
    launch_id = bootstrap_launch(store)
    script = tmp_path / "repro.py"
    script.write_text("import sys\nsys.stdout.write('cli reproduction output')\n", encoding="utf-8")
    expected = hashlib.sha256(b"cli reproduction output").hexdigest()
    ref = json.dumps({"script": str(script), "args": [], "expected_sha256": expected})
    original = record_verdict(
        store, subject_kind="artifact", subject_id="ART-cli", procedure="citecheck", procedure_version="1",
        label="PASS", reproduction_ref=ref, issued_by_launch=launch_id,
    )
    store.close()

    rc, env = _call(
        ["verify", "reproduce", original["verdict_id"], "--by-launch", launch_id, "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "match"


def test_verify_reproduce_unknown_verdict_is_structured_error(program_root, platform_root, capsys):
    rc, env = _call(
        ["verify", "reproduce", "VRD-does-not-exist", "--by-launch", "LNCH-x", "--program-root", str(program_root), "--platform-root", str(platform_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "not_found"
