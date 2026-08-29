"""``trialerror program`` CLI surface: ``init`` scaffolds a fresh program root
(FX-16/FX-10, IMPL_REVIEW_VERDICT.md Tier 2 + docs-pass NB-2/SD-2) --
trialerror.toml + the design Section 3.2 per-program layout + the initial
migration over all four DBs (platform/ops/knowledge/jobs).

Also covers FX-12 (the ``--program-root``/``--platform-root`` global,
pre-subcommand placement standardized in ``trialerror/cli/__init__.py``): a
spot-check across a representative sample of groups from every one of the
three historically-conflicting placement conventions
(``docs/OPERATOR_GUIDE.md``'s former table), proving the SAME value
resolves regardless of where the flag is given.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from trialerror.cli import main
from trialerror.util.config import load_config


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_program_no_action_is_a_structured_error(capsys):
    rc, env = _call(["program"], capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "no_action"


def test_program_init_scaffolds_trialerror_toml_layout_and_migrated_stores(tmp_path, platform_root, capsys):
    dest = tmp_path / "my-program"
    rc, env = _call(
        ["program", "init", "demo", "--dir", str(dest), "--platform-root", str(platform_root)], capsys
    )
    assert rc == 0
    assert env["ok"] is True
    result = env["result"]
    assert result["program_id"] == "demo"
    assert result["program_root"] == str(dest)

    trialerror_toml = dest / "trialerror.toml"
    assert trialerror_toml.is_file()
    cfg = load_config(trialerror_toml)
    assert cfg.program_id == "demo"

    # design Section 3.2's per-program layout (minus stores/, checked
    # below, and .claude/settings.json, an explicit non-goal -- see
    # trialerror/cli/program.py's own module docstring).
    for name in ("raw", "archive", "memory", "law", "handoffs", "artifacts", "requests"):
        assert (dest / name).is_dir(), f"missing scaffolded dir: {name}"

    # the initial migration actually ran: every db file exists and its
    # PRAGMA user_version is past the fresh-file default of 0.
    stores_dir = dest / "stores"
    for db_name in ("ops.db", "knowledge.db", "jobs.db"):
        db_path = stores_dir / db_name
        assert db_path.is_file()
        conn = sqlite3.connect(db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version > 0, f"{db_name} PRAGMA user_version is still 0 -- migration did not run"
        finally:
            conn.close()

    assert any(na["argv"][:3] == ["trialerror", "session", "boot"] for na in env["nextActions"])


def test_program_init_writes_commented_paths_knobs(tmp_path, platform_root, capsys):
    """the import-design notes (internal, not in this export) Sec 5 knob #7: ``program init`` writes the
    commented-out ``[paths]`` defaults (a discoverability feature, not a
    behavior change -- every value stays commented, so a fresh scaffold's
    resolved paths are unaffected; see ``trialerror.util.config.
    resolve_configured_path``'s own defaults for the byte-identical
    fallback each commented line documents)."""
    dest = tmp_path / "my-program"
    rc, env = _call(
        ["program", "init", "demo", "--dir", str(dest), "--platform-root", str(platform_root)], capsys
    )
    assert rc == 0

    text = (dest / "trialerror.toml").read_text(encoding="utf-8")
    assert "# [paths]" in text
    for line in (
        '# stores_dir = "stores"',
        '# archive_dir = "archive"',
        '# law_digest_path = "law/LAW_DIGEST.md"',
        '# handoffs_dir = "handoffs"',
        '# requests_path = "requests/REQUESTS.md"',
        '# memory_dir = "memory"',
        '# ingest_roots = ["raw", "inbox"]',
    ):
        assert line in text, f"missing commented default: {line!r}"

    # the fresh scaffold itself is untouched by the new commented block --
    # `load_config` still parses it with an EMPTY paths table, matching
    # cfg.paths == {} for every pre-existing template consumer.
    cfg = load_config(dest / "trialerror.toml")
    assert cfg.paths == {}


def test_program_init_default_dir_is_cwd_slash_name(tmp_path, platform_root, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc, env = _call(["program", "init", "no-dir-flag", "--platform-root", str(platform_root)], capsys)
    assert rc == 0
    expected = (tmp_path / "no-dir-flag").resolve()
    assert env["result"]["program_root"] == str(expected)
    assert (expected / "trialerror.toml").is_file()


def test_program_init_refuses_to_overwrite_an_existing_scaffold(tmp_path, platform_root, capsys):
    dest = tmp_path / "existing"
    _call(["program", "init", "demo", "--dir", str(dest), "--platform-root", str(platform_root)], capsys)

    rc, env = _call(["program", "init", "demo", "--dir", str(dest), "--platform-root", str(platform_root)], capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "already_scaffolded"


def test_program_init_refuses_when_dir_is_an_existing_file(tmp_path, platform_root, capsys):
    dest = tmp_path / "a-file"
    dest.write_text("not a directory", encoding="utf-8")

    rc, env = _call(["program", "init", "demo", "--dir", str(dest), "--platform-root", str(platform_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "dir_is_a_file"


# ---------------------------------------------------------------------------
# FX-12: stale-nextAction-string spot check -- every refusal that used to
# name a nonexistent `trialerror program init` now names the real, runnable
# syntax (positional <name> included).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["artifact", "list"],
        ["gate", "open", "--artifact-id", "x"],
        ["law", "lookup"],
        ["memory", "search"],
        ["verify", "reproduce", "V-x", "--by-launch", "y"],  # _run_reproduce -> _open() directly (unlike
        # `verify citecheck`, which has its own redundant pre-check that
        # short-circuits WITHOUT a next_action -- a separate, narrower gap
        # than FX-16/FX-10's "stale string" scope, out of this fix's brief)
        ["prereg", "status", "--id", "x"],
        ["session", "status"],
        ["lens", "log", "--round-id", "r"],
    ],
)
def test_program_root_not_found_next_actions_name_the_real_init_syntax(argv, tmp_path, monkeypatch, capsys):
    # An isolated, trialerror.toml-free CWD (no --program-root given at all) so
    # every group's own find_program_root()-based discovery genuinely
    # fails, exercising the SAME refusal path each of these next_actions
    # lives on.
    monkeypatch.chdir(tmp_path)
    rc, env = _call(argv, capsys)
    assert rc == 1
    program_init_actions = [na for na in env["nextActions"] if na["argv"][:3] == ["trialerror", "program", "init"]]
    assert program_init_actions, f"expected a `trialerror program init` next_action, got {env['nextActions']!r}"
    for na in program_init_actions:
        # the fixed syntax carries a positional <name> -- the pre-fix
        # strings were exactly ["trialerror", "program", "init"], which is now
        # missing a required argument.
        assert len(na["argv"]) > 3, f"next_action still looks like the stale bare form: {na!r}"


# ---------------------------------------------------------------------------
# FX-12: global pre-subcommand placement, spot-checked across one group
# from each of the three historically-conflicting buckets.
# ---------------------------------------------------------------------------


def test_fx12_global_program_root_works_pre_subcommand_for_group_a_style(program_root, platform_root, capsys):
    # law: historically "both the group parser and every verb subparser --
    # must go AFTER the verb".
    rc, env = _call(
        ["--program-root", str(program_root), "--platform-root", str(platform_root), "law", "append", "--summary", "x"],
        capsys,
    )
    assert rc == 0
    assert env["ok"] is True


def test_fx12_global_program_root_works_pre_subcommand_for_group_b_style(program_root, platform_root, capsys):
    # events: historically "only the verb subparsers -- must go AFTER the
    # verb, an outright argparse error before it".
    rc, env = _call(
        [
            "--program-root", str(program_root), "--platform-root", str(platform_root),
            "events", "append", "--type", "boarded", "--payload", "{}",
        ],
        capsys,
    )
    assert rc == 0
    assert env["ok"] is True


def test_fx12_global_program_root_works_pre_subcommand_for_group_c_style(program_root, platform_root, capsys):
    # budget: historically "only the group parser -- must go BEFORE the
    # subcommand" (the one bucket the global flag's placement matches
    # exactly, but worth asserting explicitly since it now also flows
    # through the SUPPRESS-on-the-local-declaration mechanism).
    rc, env = _call(
        [
            "--program-root", str(program_root), "--platform-root", str(platform_root),
            "budget", "pools",
        ],
        capsys,
    )
    assert rc == 0
    assert env["ok"] is True


def test_fx12_post_verb_placement_still_wins_over_a_different_global_value(program_root, platform_root, tmp_path, capsys):
    """Back-compat: an explicit value given after the verb (each group's
    historical convention) still overrides a DIFFERENT global value given
    before the group -- the global only supplies a default."""
    decoy_root = tmp_path / "decoy-should-not-be-used"
    decoy_root.mkdir()
    rc, env = _call(
        [
            "--program-root", str(decoy_root),
            "law", "append", "--program-root", str(program_root), "--summary", "wins",
        ],
        capsys,
    )
    assert rc == 0
    assert env["ok"] is True
    # the decoy root never got a trialerror.toml/stores written to it by this call.
    assert not (decoy_root / "stores").exists()
