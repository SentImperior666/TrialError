"""``trialerror extract`` CLI group -- argv parsing, envelope shaping, and
auto-discovery, driven end to end through ``trialerror.cli.main`` (the
``tests/test_verify_cli.py`` convention -- ``--program-root``/
``--platform-root`` placed AFTER the action token, since argparse's
``_SubParsersAction`` parses trailing args into a fresh namespace that
overrides a same-named parent default)."""

from __future__ import annotations

import json

from trialerror.cli import extract as cli_extract
from trialerror.cli import main

from tests._retrieve_fixtures import build_small_corpus

from .test_ingest_extract import _fake_judge, _open_chunk_id


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_group_name_and_help_registered():
    assert cli_extract.GROUP_NAME == "extract"
    assert cli_extract.HELP


def test_no_action_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(["extract", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "no_action"


def test_run_review_accept_reject_status_round_trip(store, program_root, platform_root, capsys, tmp_path):
    corpus = build_small_corpus(store)
    store.close()  # the CLI opens its own Store per invocation

    judge = _fake_judge(corpus)
    chunk_id = _open_chunk_id(corpus)
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(json.dumps({chunk_id: judge({"chunk_id": chunk_id})}), encoding="utf-8")

    rc, env = _call(
        [
            "extract", "run", "--doc-id", corpus["open_doc_id"], "--judgments-file", str(judgments_path),
            "--by-launch", corpus["launch_id"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["chunks_processed"] == 1
    assert env["result"]["entities_queued"] == 2

    rc, env = _call(["extract", "review", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert len(env["result"]["candidates"]) == 4
    entity_candidate = next(c for c in env["result"]["candidates"] if c["payload"]["kind"] == "entity")
    record_id = entity_candidate["record_id"]

    rc, env = _call(["extract", "accept", "--id", record_id, "--by-launch", corpus["launch_id"], "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert env["result"]["kind"] == "entity"

    rc, env = _call(["extract", "status", "--program-root", str(program_root)], capsys)
    assert rc == 0
    assert env["result"]["candidates"]["entity"]["accepted"] == 1
    assert env["result"]["candidates"]["entity"]["pending"] == 1


def test_reject_round_trip(store, program_root, platform_root, capsys, tmp_path):
    corpus = build_small_corpus(store)
    store.close()

    judge = _fake_judge(corpus)
    chunk_id = _open_chunk_id(corpus)
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(json.dumps({chunk_id: judge({"chunk_id": chunk_id})}), encoding="utf-8")
    _call(
        [
            "extract", "run", "--doc-id", corpus["open_doc_id"], "--judgments-file", str(judgments_path),
            "--by-launch", corpus["launch_id"], "--program-root", str(program_root),
        ],
        capsys,
    )
    rc, env = _call(["extract", "review", "--kind", "claim", "--program-root", str(program_root)], capsys)
    assert rc == 0
    record_id = env["result"]["candidates"][0]["record_id"]

    rc, env = _call(
        ["extract", "reject", "--id", record_id, "--by-launch", corpus["launch_id"], "--reason", "cli test", "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0
    assert env["result"]["status"] == "rejected"


def test_accept_unknown_id_is_a_structured_error(store, program_root, platform_root, capsys):
    build_small_corpus(store)
    store.close()
    rc, env = _call(
        ["extract", "accept", "--id", "RCD-does-not-exist", "--by-launch", "LNCH-x", "--program-root", str(program_root)], capsys
    )
    assert rc == 1
    assert env["error"]["code"] == "accept_refused"
