import hashlib
import sys
from pathlib import Path

import trialerror.cli as cli_pkg
from trialerror.cli import build_parser, discover_groups, main
from trialerror.util.envelope import PROTOCOL_VERSION


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


FIXTURE_GROUP_SOURCE = '''
GROUP_NAME = "widget"
HELP = "fixture group for the autodiscovery test"


def register(subparsers):
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    parser.add_argument("--loud", action="store_true")
    parser.set_defaults(handler=run)
    return parser


def run(args):
    from trialerror.util.envelope import ok_envelope

    return ok_envelope("widget", result={"loud": args.loud})
'''


def test_fixture_cli_group_autoregisters_without_touching_shared_files(tmp_path, monkeypatch):
    init_path = Path(cli_pkg.__file__)
    before_hash = _hash(init_path)
    before_source = init_path.read_text(encoding="utf-8")

    fixture_dir = tmp_path / "extra_cli_group"
    fixture_dir.mkdir()
    (fixture_dir / "widget.py").write_text(FIXTURE_GROUP_SOURCE, encoding="utf-8")

    # Extend the REAL trialerror.cli package's search path with our fixture dir --
    # this is the only thing a "new lane" conceptually does (drop a file
    # somewhere trialerror/cli/ looks); nothing about __init__.py is edited.
    monkeypatch.setattr(cli_pkg, "__path__", list(cli_pkg.__path__) + [str(fixture_dir)])

    try:
        groups = discover_groups()
        names = {getattr(m, "GROUP_NAME", None) for m in groups}
        assert "widget" in names

        # And it's usable end to end through the normal CLI entry point.
        parser = build_parser(groups)
        args = parser.parse_args(["widget", "--loud"])
        env = args.handler(args)
        assert env == {
            "ok": True,
            "command": "widget",
            "protocolVersion": PROTOCOL_VERSION,
            "result": {"loud": True},
            "nextActions": [],
            "meta": {},
        }
    finally:
        sys.modules.pop("trialerror.cli.widget", None)

    after_hash = _hash(init_path)
    after_source = init_path.read_text(encoding="utf-8")
    assert after_hash == before_hash
    assert after_source == before_source


def test_real_discover_groups_finds_doctor():
    groups = discover_groups()
    names = {getattr(m, "GROUP_NAME", None) for m in groups}
    assert "doctor" in names


def test_parser_help_lists_doctor_group():
    parser = build_parser()
    help_text = parser.format_help()
    assert "doctor" in help_text


def test_main_no_args_help_output_mentions_doctor(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "doctor" in out
