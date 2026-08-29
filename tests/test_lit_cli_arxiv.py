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

    args = parser.parse_args(["lit", "arxiv-semantic", "--q", "tabletop engines", "--k", "5"])
    assert args.lit_cmd == "arxiv-semantic"
    assert args.query == "tabletop engines"
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
    args = _Args(program_root=str(program_root), query="tabletop engines", k=5)
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

    args = _Args(program_root=str(program_root), query="tabletop engines", k=5)
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
