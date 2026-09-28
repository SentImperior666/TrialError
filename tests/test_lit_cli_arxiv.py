"""Tests for the ``trialerror lit arxiv-index build`` / ``trialerror lit
arxiv-semantic`` CLI subcommands (``trialerror/cli/lit.py``, build-arxiv-kaggle-index
session). Mirrors ``tests/test_litapi_cli.py``'s own two-tier convention:
one wiring test against the REAL argparse parser (subcommand/flag
presence), and direct ``_cmd_*(args)`` calls with a hand-built namespace
for the actual logic (avoids the top-level parser's
``--program-root``/``--platform-root`` ``SUPPRESS``-default plumbing,
matching every other handler test in that file)."""

from __future__ import annotations

import argparse
import json

import pytest

from trialerror.arxiv_index.encoder import FakeQueryEncoder
from trialerror.cli import lit as cli_lit
from trialerror.util.envelope import PROTOCOL_VERSION
from tests._arxiv_index_fixtures import write_small_fixture_zip


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_register_wires_arxiv_index_build_subcommand():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    args = parser.parse_args(["lit", "arxiv-index", "build", "--zip", "x.zip", "--dims", "8"])
    assert args.arxiv_index_cmd == "build"
    assert args.zip_path == "x.zip"
    assert args.dims == 8
    assert args.detach is False


def test_register_wires_arxiv_semantic_subcommand():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    args = parser.parse_args(["lit", "arxiv-semantic", "--q", "distributed systems", "--k", "5"])
    assert args.lit_cmd == "arxiv-semantic"
    assert args.query == "distributed systems"
    assert args.k == 5


def test_cmd_arxiv_index_build_no_program_root_is_error_envelope(monkeypatch):
    monkeypatch.setattr(cli_lit, "_resolve_program_root", lambda args: None)
    args = _Args(zip_path="x.zip", db_path=None, dims=None, batch_size=None, member_glob=None, min_free_gb=None, job_id=None, launch_id=None, detach=False)
    env = cli_lit._cmd_arxiv_index_build(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_program_root"


def test_cmd_arxiv_index_build_zip_not_found_is_error_envelope(tmp_path):
    args = _Args(
        program_root=str(tmp_path), zip_path=str(tmp_path / "missing.zip"), db_path=None, dims=None,
        batch_size=None, member_glob=None, min_free_gb=None, job_id=None, launch_id=None, detach=False,
    )
    env = cli_lit._cmd_arxiv_index_build(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "zip_not_found"


def test_cmd_arxiv_index_build_happy_path_foreground(tmp_path):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=10, dims=8)
    program_root = tmp_path / "program"
    program_root.mkdir()
    args = _Args(
        program_root=str(program_root), platform_root=str(tmp_path / "platform"),
        zip_path=str(zip_path), db_path=None, dims=8, batch_size=4, member_glob=None,
        min_free_gb=0.001, job_id=None, launch_id=None, detach=False,
    )
    env = cli_lit._cmd_arxiv_index_build(args)
    assert env["ok"] is True, env
    assert env["result"]["status"] == "complete"
    assert env["result"]["checkpoint"]["rows_ingested"] == 10
    assert env["protocolVersion"] == PROTOCOL_VERSION


def test_cmd_arxiv_index_build_same_zip_reuses_same_job_id(tmp_path):
    """Re-running the same command (no --job-id given) must resolve to the
    SAME deterministic job id -- the whole point of the default being
    derived from the zip's own resolved path (docs/USER_SETUP.md §3e:
    "re-run the exact same command to resume")."""
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=5, dims=8)
    program_root = tmp_path / "program"
    program_root.mkdir()
    args = _Args(
        program_root=str(program_root), platform_root=str(tmp_path / "platform"),
        zip_path=str(zip_path), db_path=None, dims=8, batch_size=4, member_glob=None,
        min_free_gb=0.001, job_id=None, launch_id=None, detach=False,
    )
    env1 = cli_lit._cmd_arxiv_index_build(args)
    env2 = cli_lit._cmd_arxiv_index_build(args)
    assert env1["result"]["job_id"] == env2["result"]["job_id"]
    assert env1["result"]["status"] == "complete"
    # a second run against an already-terminal job reports that honestly
    # rather than crashing on NotClaimableError -- ledger.claim_or_create
    # refuses to reclaim a 'complete' job (terminal state).
    assert env2["result"]["status"] == "already-complete"
    assert env2["result"]["checkpoint"]["rows_ingested"] == 5


def test_cmd_arxiv_semantic_index_not_built_is_error_envelope(tmp_path):
    program_root = tmp_path / "program"
    program_root.mkdir()
    args = _Args(program_root=str(program_root), query="distributed systems", k=5)
    env = cli_lit._cmd_arxiv_semantic(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "index_not_built"


def test_cmd_arxiv_semantic_no_api_key_is_error_envelope(tmp_path):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=5, dims=8)
    program_root = tmp_path / "program"
    program_root.mkdir()
    build_args = _Args(
        program_root=str(program_root), platform_root=str(tmp_path / "platform"),
        zip_path=str(zip_path), db_path=None, dims=8, batch_size=4, member_glob=None,
        min_free_gb=0.001, job_id=None, launch_id=None, detach=False,
    )
    cli_lit._cmd_arxiv_index_build(build_args)

    args = _Args(program_root=str(program_root), query="distributed systems", k=5)
    env = cli_lit._cmd_arxiv_semantic(args)
    assert env["ok"] is False
    assert env["error"]["code"] == "no_api_key"


def test_cmd_arxiv_semantic_happy_path_with_fake_encoder(tmp_path, monkeypatch):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=8, dims=8)
    program_root = tmp_path / "program"
    program_root.mkdir()
    build_args = _Args(
        program_root=str(program_root), platform_root=str(tmp_path / "platform"),
        zip_path=str(zip_path), db_path=None, dims=8, batch_size=4, member_glob=None,
        min_free_gb=0.001, job_id=None, launch_id=None, detach=False,
    )
    cli_lit._cmd_arxiv_index_build(build_args)

    fake = FakeQueryEncoder(dims=8)
    monkeypatch.setattr(cli_lit, "_build_query_encoder", lambda litapi_cfg, program_root: fake)

    args = _Args(program_root=str(program_root), query="synthetic paper 3", k=3)
    env = cli_lit._cmd_arxiv_semantic(args)
    assert env["ok"] is True, env
    assert env["result"]["k"] == 3
    assert len(env["result"]["results"]) == 3
    assert env["result"]["estimated_cost_usd"] > 0
    assert all("arxiv_id" in r and "title" in r and "score" in r for r in env["result"]["results"])


# ---------------------------------------------------------------------------
# lane FB-acq item 3: --q-file (many queries, ONE pass) and the timing block
# ---------------------------------------------------------------------------


def _built_program(tmp_path, monkeypatch, *, n=8, dims=8):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=n, dims=dims)
    program_root = tmp_path / "program"
    program_root.mkdir()
    cli_lit._cmd_arxiv_index_build(
        _Args(
            program_root=str(program_root), platform_root=str(tmp_path / "platform"),
            zip_path=str(zip_path), db_path=None, dims=dims, batch_size=4, member_glob=None,
            min_free_gb=0.001, job_id=None, launch_id=None, detach=False,
        )
    )
    monkeypatch.setattr(
        cli_lit, "_build_query_encoder", lambda litapi_cfg, program_root: FakeQueryEncoder(dims=dims)
    )
    return program_root


def test_register_wires_q_file_and_refuses_both_forms_at_once():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    cli_lit.register(subparsers)

    args = parser.parse_args(["lit", "arxiv-semantic", "--q-file", "queries.txt"])
    assert args.q_file == "queries.txt"
    assert args.query is None

    with pytest.raises(SystemExit):
        parser.parse_args(["lit", "arxiv-semantic"])  # neither
    with pytest.raises(SystemExit):
        parser.parse_args(["lit", "arxiv-semantic", "--q", "x", "--q-file", "queries.txt"])  # both


def test_cmd_arxiv_semantic_q_file_answers_every_query_in_one_pass(tmp_path, monkeypatch):
    """FAILS BEFORE this lane: --q-file did not exist, and three queries meant
    three full scans of an index that has no approximate structure to fall back
    on."""
    program_root = _built_program(tmp_path, monkeypatch)
    q_file = tmp_path / "queries.txt"
    q_file.write_text("  synthetic paper 3  \n\nsynthetic paper 5\nsynthetic paper 7\n", encoding="utf-8")

    env = cli_lit._cmd_arxiv_semantic(
        _Args(program_root=str(program_root), query=None, q_file=str(q_file), k=3)
    )

    assert env["ok"] is True, env
    assert env["result"]["n_queries"] == 3
    assert env["result"]["scan_mode"] == "single_pass"
    assert env["result"]["passes"] == 1
    assert env["result"]["rows_scanned"] == 8
    assert env["result"]["backend"] in ("sqlite_vec", "fallback")
    # file order is answer order, and whitespace/blank lines are handled
    assert [q["query"] for q in env["result"]["queries"]] == [
        "synthetic paper 3", "synthetic paper 5", "synthetic paper 7",
    ]
    assert all(len(q["results"]) == 3 for q in env["result"]["queries"])
    assert env["result"]["estimated_cost_usd"] > 0
    assert set(env["result"]["timing"]) == {"open_s", "encode_s", "search_s", "total_s"}


def test_q_file_answers_match_the_single_q_form_query_for_query(tmp_path, monkeypatch):
    program_root = _built_program(tmp_path, monkeypatch)
    queries = ["synthetic paper 2", "synthetic paper 6"]
    q_file = tmp_path / "queries.txt"
    q_file.write_text("\n".join(queries), encoding="utf-8")

    batch = cli_lit._cmd_arxiv_semantic(
        _Args(program_root=str(program_root), query=None, q_file=str(q_file), k=3)
    )
    singles = [
        cli_lit._cmd_arxiv_semantic(_Args(program_root=str(program_root), query=q, q_file=None, k=3))
        for q in queries
    ]

    for block, single in zip(batch["result"]["queries"], singles):
        assert block["results"] == single["result"]["results"]


def test_the_single_q_result_keeps_its_shape_and_gains_only_timing(tmp_path, monkeypatch):
    program_root = _built_program(tmp_path, monkeypatch)

    env = cli_lit._cmd_arxiv_semantic(
        _Args(program_root=str(program_root), query="synthetic paper 3", q_file=None, k=3)
    )

    assert list(env["result"]) == ["query", "k", "estimated_cost_usd", "results", "timing"]
    assert env["result"]["query"] == "synthetic paper 3"
    assert len(env["result"]["results"]) == 3


def test_q_file_warns_when_numpy_is_off_and_every_query_was_its_own_scan(tmp_path, monkeypatch):
    program_root = _built_program(tmp_path, monkeypatch)
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "example"\n\n[retrieve]\nnumpy_fastpath = "off"\n', encoding="utf-8"
    )
    q_file = tmp_path / "queries.txt"
    q_file.write_text("synthetic paper 1\nsynthetic paper 4\n", encoding="utf-8")

    env = cli_lit._cmd_arxiv_semantic(
        _Args(program_root=str(program_root), query=None, q_file=str(q_file), k=2)
    )

    assert env["ok"] is True, env
    assert env["result"]["scan_mode"] == "per_query"
    assert env["result"]["passes"] == 2
    assert len(env["warnings"]) == 1
    assert "ran as 2 full scans" in json.dumps(env["warnings"])


def test_q_file_refusals(tmp_path, monkeypatch):
    program_root = _built_program(tmp_path, monkeypatch)

    missing = cli_lit._cmd_arxiv_semantic(
        _Args(program_root=str(program_root), query=None, q_file=str(tmp_path / "nope.txt"), k=3)
    )
    assert missing["error"]["code"] == "q_file_not_found"

    blank = tmp_path / "blank.txt"
    blank.write_text("\n   \n\n", encoding="utf-8")
    empty = cli_lit._cmd_arxiv_semantic(
        _Args(program_root=str(program_root), query=None, q_file=str(blank), k=3)
    )
    assert empty["error"]["code"] == "q_file_empty"

    from trialerror.arxiv_index.query import MAX_BATCH_QUERIES

    too_many = tmp_path / "many.txt"
    too_many.write_text("\n".join(f"query {i}" for i in range(MAX_BATCH_QUERIES + 1)), encoding="utf-8")
    refused = cli_lit._cmd_arxiv_semantic(
        _Args(program_root=str(program_root), query=None, q_file=str(too_many), k=3)
    )
    assert refused["error"]["code"] == "too_many_queries"
    assert str(MAX_BATCH_QUERIES) in refused["error"]["message"]
