"""L8 part F, F1: ``main()`` turns :class:`ProgramRootIsHarnessError` (raised
by ``find_program_root()``, the one place the default program root is
resolved) into a clean error envelope instead of a traceback -- and an
explicit ``--program-root``/``TRIALERROR_PROGRAM_ROOT`` bypasses the refusal
entirely, because it was asked for.

``tests/test_config.py`` already covers ``find_program_root`` itself (the
checkout case, the worktree case, the env overrides, ``refuse_harness=False``
opt-out); this file covers the CLI wiring around it.
"""

from __future__ import annotations

import argparse
import json

import trialerror.cli as cli_mod
import trialerror.util.config as config_mod
from trialerror.cli import main
from trialerror.util.config import ProgramRootIsHarnessError


def test_main_turns_program_root_is_harness_into_an_error_envelope(capsys, monkeypatch):
    """A stub handler standing in for any of the ~18 CLI groups whose own
    ``_resolve_program_root`` falls back to ``find_program_root()`` -- what
    matters here is only that ``main()`` catches the exception at the one
    dispatch point every handler passes through (after argument parsing),
    not which group raised it. Patching a real group's own ``handler``
    default (set via ``set_defaults`` at registration) exercises that exact
    dispatch point end to end.
    """

    def _raising_handler(args):
        raise ProgramRootIsHarnessError(config_mod._HARNESS_PACKAGE_PARENT)

    def _subparsers_action(parser):
        return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

    parser = cli_mod.build_parser()
    session_parser = _subparsers_action(parser).choices["session"]
    status_parser = _subparsers_action(session_parser).choices["status"]
    status_parser.set_defaults(handler=_raising_handler)
    monkeypatch.setattr(cli_mod, "build_parser", lambda *a, **kw: parser)

    rc = main(["session", "status"])
    out = json.loads(capsys.readouterr().out.strip())

    assert rc == 1
    assert out["ok"] is False
    assert out["error"]["code"] == "program_root_is_harness"
    assert "TRIALERROR_PROGRAM_ROOT" in out["error"]["message"]


def test_explicit_program_root_bypasses_the_refusal(tmp_path, monkeypatch, capsys):
    """``--program-root`` pointing straight at the harness's own repo is
    honored, not refused: it was asked for. ``probes run``'s own
    ``_resolve_program_root`` (``trialerror/cli/probes.py``) checks
    ``args.program_root`` truthy BEFORE ever calling ``find_program_root()``,
    so the refusal code path is not reached at all -- this test proves that
    behavior end to end for a real group that DOES open a program store
    (unlike ``probes status``, which never resolves a program root at all).
    """
    repo = tmp_path / "fake_checkout"
    (repo / "trialerror").mkdir(parents=True)
    (repo / "trialerror" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "trialerror.toml").write_text(
        '[program]\nid = "fake"\n', encoding="utf-8"
    )
    monkeypatch.setattr(config_mod, "_HARNESS_PACKAGE_PARENT", repo)
    platform_root = tmp_path / "platform_root"

    rc = main(
        [
            "probes",
            "run",
            "--kind",
            "conformance",
            "--program-root",
            str(repo),
            "--platform-root",
            str(platform_root),
        ]
    )
    out = json.loads(capsys.readouterr().out.strip())
    # Whatever the outcome, it must not be the harness-fallback refusal --
    # this root was given explicitly.
    if not out["ok"]:
        assert out["error"].get("code") != "program_root_is_harness"
    assert rc in (0, 1)
