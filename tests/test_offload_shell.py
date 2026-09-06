"""Lane L0-C, C-unit ``shell_rejects_unknown_verb_and_bad_id``.

``deploy/sandbox/offload-shell.sh`` IS the security of the restricted DEV
worker key (design section 4): the key can run nothing else, so whatever
that script refuses, the key cannot do. Its rules therefore exist twice --
in POSIX shell where they run, and in
:mod:`trialerror.offload.shell` where they can be unit-tested anywhere and
are reused by the in-process transport.

ONE table drives both halves. The Python port is always checked; the real
script is additionally run under ``sh`` when one is available (it is, on
the Ubuntu sandbox and in Git-for-Windows), with ``TE_OFFLOAD_ROOT``
pointed at a tmp directory so nothing outside the test can be touched.
That is the "run the wrapper under sh with a fake env if possible, else a
pure-python port of its checks with the same table" the brief asks for --
here it is both.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from trialerror.offload.shell import (
    EXIT_BAD_ID,
    EXIT_BAD_VERB,
    EXIT_OK,
    VERBS,
    parse_command,
)

SHELL_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sandbox" / "offload-shell.sh"

#: (command, expected exit code, what the rule is for). Only commands whose
#: verdict is decided by PARSING are in this table -- an accepted verb's own
#: IO outcome is the protocol suite's business, not the wrapper's.
REFUSAL_TABLE: list[tuple[str | None, int, str]] = [
    (None, EXIT_BAD_VERB, "no command at all (an interactive login attempt)"),
    ("", EXIT_BAD_VERB, "empty command"),
    ("   ", EXIT_BAD_VERB, "whitespace-only command"),
    ("bash", EXIT_BAD_VERB, "a shell"),
    ("sh -c 'id'", EXIT_BAD_VERB, "a shell with an argument"),
    ("scp -t /tmp", EXIT_BAD_VERB, "scp (the classic forced-command escape)"),
    ("rsync --server", EXIT_BAD_VERB, "rsync server mode"),
    ("LIST", EXIT_BAD_VERB, "verbs are case-sensitive"),
    ("listen", EXIT_BAD_VERB, "a verb prefix is not a verb"),
    ("list extra", EXIT_BAD_VERB, "list takes no argument"),
    ("claim", EXIT_BAD_VERB, "claim needs an id"),
    ("claim a b", EXIT_BAD_VERB, "claim takes exactly one id"),
    ("claim .", EXIT_BAD_ID, "'.' would name the claim directory itself"),
    ("claim ..", EXIT_BAD_ID, "'..' escapes the queue"),
    ("claim ../../etc/passwd", EXIT_BAD_ID, "explicit traversal"),
    ("claim /etc/passwd", EXIT_BAD_ID, "absolute path as an id"),
    ("pull a/b", EXIT_BAD_ID, "a separator in the id"),
    ("pull a\\b", EXIT_BAD_ID, "a Windows separator in the id"),
    ("push a*", EXIT_BAD_ID, "a glob in the id"),
    ("publish a$b", EXIT_BAD_ID, "a shell variable in the id"),
    ("heartbeat a;id", EXIT_BAD_ID, "a command separator in the id"),
    ("return a|b", EXIT_BAD_ID, "a pipe in the id"),
    ("return a`id`", EXIT_BAD_ID, "a command substitution in the id"),
    # SEC-6: `sh` splits on space/tab/newline only, so a carriage return
    # (or a vertical tab, or U+00A0) is part of the WORD out there while
    # Python's bare str.split() used to treat it as a separator. Both halves
    # must now agree that these are one malformed id, not two words.
    ("claim a\rb", EXIT_BAD_ID, "a carriage return inside the id"),
    ("claim a\vb", EXIT_BAD_ID, "a vertical tab inside the id"),
    ("claim a\x0cb", EXIT_BAD_ID, "a form feed inside the id"),
    ("claim a\u00a0b", EXIT_BAD_ID, "a non-breaking space inside the id"),
    ("claim JOB-a\n", EXIT_OK, "a trailing newline is IFS, not part of the id"),
    ("list", EXIT_OK, "the one verb that takes no id"),
    ("claim JOB-ingest-DOC-1", EXIT_OK, "a well-formed id"),
    ("heartbeat JOB.embed_1-2", EXIT_OK, "every allowed character class"),
]


@pytest.mark.parametrize(
    "command,expected,why", REFUSAL_TABLE, ids=[t[2].replace(" ", "-")[:48] for t in REFUSAL_TABLE]
)
def test_python_port_matches_the_refusal_table(command, expected, why):
    parsed = parse_command(command)
    assert parsed.exit_code == expected, f"{why}: {command!r} -> {parsed}"
    assert parsed.ok is (expected == EXIT_OK)


def test_every_documented_verb_is_accepted_with_a_good_id():
    for verb in VERBS:
        command = verb if verb == "list" else f"{verb} JOB-1"
        assert parse_command(command).ok, verb


def test_the_python_port_never_returns_a_verb_outside_the_protocol():
    for command, expected, _why in REFUSAL_TABLE:
        parsed = parse_command(command)
        if parsed.ok:
            assert parsed.verb in VERBS


# ---------------------------------------------------------------------------
# the real script, under sh
# ---------------------------------------------------------------------------
sh_path = shutil.which("sh") or shutil.which("bash")
requires_sh = pytest.mark.skipif(
    sh_path is None or not SHELL_SCRIPT.is_file(),
    reason="no POSIX sh available to run deploy/sandbox/offload-shell.sh",
)


def _run_shell(command: str | None, offload_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TE_OFFLOAD_ROOT"] = offload_root.as_posix()
    env["TE_OFFLOAD_WORKER_ID"] = "dev"
    if command is None:
        env.pop("SSH_ORIGINAL_COMMAND", None)
    else:
        env["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(
        [sh_path, SHELL_SCRIPT.as_posix()],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@requires_sh
@pytest.mark.parametrize(
    "command,expected,why",
    [t for t in REFUSAL_TABLE if t[1] != EXIT_OK],
    ids=[t[2].replace(" ", "-")[:48] for t in REFUSAL_TABLE if t[1] != EXIT_OK],
)
def test_the_real_wrapper_refuses_the_same_table(tmp_path, command, expected, why):
    """The same refusals, executed by the script that actually guards the
    key. Every one of these exits non-zero BEFORE touching the filesystem,
    which is why an empty tmp root is enough of an environment."""
    proc = _run_shell(command, tmp_path / "offload")
    assert proc.returncode == expected, f"{why}: {command!r} -> rc={proc.returncode} {proc.stderr}"
    assert "offload-shell:" in proc.stderr


@requires_sh
def test_the_real_wrapper_lists_pending_jobs(tmp_path):
    """The one accepted verb with no side effects: it must agree with the
    Python side's own view of ``pending/``."""
    from trialerror.offload import protocol
    from tests._offload_fixtures import queue_one

    root = protocol.ensure_layout(tmp_path / "offload")
    queue_one(root, "JOB-b")
    queue_one(root, "JOB-a")

    proc = _run_shell("list", root)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["JOB-a", "JOB-b"] == protocol.list_pending(root)


@requires_sh
def test_the_real_wrapper_claims_and_returns_like_the_python_server(tmp_path):
    """A claim/return round trip through the SHELL, verified with the
    PYTHON readers -- the one test that proves the two implementations
    agree about the on-disk layout, not just about refusals."""
    from trialerror.offload import protocol
    from tests._offload_fixtures import queue_one

    root = protocol.ensure_layout(tmp_path / "offload")
    queue_one(root, "JOB-a", payload=b"body")

    proc = _run_shell("claim JOB-a", root)
    assert proc.returncode == 0, proc.stderr
    assert '"job_id": "JOB-a"' in proc.stdout  # the manifest is the claim's reply
    assert protocol.find_manifest(root, "JOB-a")[0] == "claimed"
    assert [c["worker_id"] for c in protocol.list_claims(root)] == ["dev"]

    # a second claim loses the race, exactly as the Python server does
    assert _run_shell("claim JOB-a", root).returncode == 4

    assert _run_shell("heartbeat JOB-a", root).returncode == 0
    assert _run_shell("return JOB-a", root).returncode == 0
    assert protocol.list_pending(root) == ["JOB-a"]
    assert (protocol.pending_dir(root) / "JOB-a" / "input.txt").read_bytes() == b"body"


def _run_shell_bytes(command: str, offload_root, stdin: bytes):
    """`_run_shell` for the one verb that reads stdin. Bytes in, bytes out:
    `push` is the only place the wrapper touches the stream at all."""
    env = dict(os.environ)
    env["TE_OFFLOAD_ROOT"] = Path(offload_root).as_posix()
    env["TE_OFFLOAD_WORKER_ID"] = "dev"
    env["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(
        [sh_path, SHELL_SCRIPT.as_posix()],
        capture_output=True,
        input=stdin,
        env=env,
        timeout=60,
    )


def _tar_bytes(files: dict, *, symlink: str | None = None) -> bytes:
    import io as _io
    import tarfile as _tarfile

    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = _tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, _io.BytesIO(data))
        if symlink is not None:
            info = _tarfile.TarInfo(name=symlink)
            info.type = _tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


def _claimed_root(tmp_path):
    from trialerror.offload import protocol
    from tests._offload_fixtures import queue_one

    root = protocol.ensure_layout(tmp_path / "offload")
    queue_one(root, "JOB-a")
    protocol.server_claim(root, "JOB-a", worker_id="dev")
    return root


@requires_sh
def test_the_real_wrapper_accepts_a_flat_push(tmp_path):
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    proc = _run_shell_bytes("push JOB-a", root, _tar_bytes({"pages.json": b"{}"}))
    assert proc.returncode == 0, proc.stderr
    assert (protocol.partial_dir(root) / "JOB-a" / "pages.json").read_bytes() == b"{}"
    # SEC-3: the stdin buffer is not left behind
    assert not (protocol.partial_dir(root) / "JOB-a.push-tmp.tar").exists()


@requires_sh
@pytest.mark.parametrize(
    "member",
    ["sub/pages.json", "../escape.json", "-rf"],
    ids=["nested-directory", "traversal", "a-name-that-looks-like-a-flag"],
)
def test_the_real_wrapper_refuses_a_pushed_member_by_name_before_extracting(tmp_path, member):
    """SEC-3: name-first. The old order extracted and then looked; a
    refusal now happens off the LISTING, and nothing is written at all."""
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    proc = _run_shell_bytes("push JOB-a", root, _tar_bytes({member: b"x"}))
    assert proc.returncode == 4, proc.stderr
    assert not (protocol.partial_dir(root) / "JOB-a").exists()
    assert not (protocol.partial_dir(root) / "JOB-a.push-tmp.tar").exists()


@requires_sh
def test_the_real_wrapper_refuses_a_pushed_symlink(tmp_path):
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    proc = _run_shell_bytes("push JOB-a", root, _tar_bytes({"pages.json": b"{}"}, symlink="secrets"))
    assert proc.returncode == 4, proc.stderr
    assert "non-regular" in proc.stderr.decode("utf-8", "replace")
    assert not (protocol.partial_dir(root) / "JOB-a").exists()


@requires_sh
def test_the_real_wrapper_refuses_an_over_large_push(tmp_path):
    """SEC-4: the verb FAILS past the cap; it does not truncate. A
    truncated tar would be a corrupt result checked against a manifest it
    can no longer satisfy."""
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    env_cap = "4096"
    proc = subprocess.run(
        [sh_path, SHELL_SCRIPT.as_posix()],
        capture_output=True,
        input=_tar_bytes({"pages.json": b"x" * 65536}),
        env={
            **os.environ,
            "TE_OFFLOAD_ROOT": Path(root).as_posix(),
            "TE_OFFLOAD_WORKER_ID": "dev",
            "TE_OFFLOAD_MAX_PUSH_BYTES": env_cap,
            "SSH_ORIGINAL_COMMAND": "push JOB-a",
        },
        timeout=60,
    )
    assert proc.returncode == 4
    assert b"cap" in proc.stderr
    assert not (protocol.partial_dir(root) / "JOB-a").exists()
    assert not (protocol.partial_dir(root) / "JOB-a.push-tmp.tar").exists()


@requires_sh
def test_the_real_wrapper_refuses_an_over_large_pull(tmp_path):
    root = _claimed_root(tmp_path)
    proc = subprocess.run(
        [sh_path, SHELL_SCRIPT.as_posix()],
        capture_output=True,
        env={
            **os.environ,
            "TE_OFFLOAD_ROOT": Path(root).as_posix(),
            "TE_OFFLOAD_WORKER_ID": "dev",
            "TE_OFFLOAD_MAX_PULL_BYTES": "1",
            "SSH_ORIGINAL_COMMAND": "pull JOB-a",
        },
        timeout=60,
    )
    assert proc.returncode == 4
    assert b"pull cap" in proc.stderr


@requires_sh
def test_the_real_wrapper_refuses_verbs_on_a_job_it_does_not_hold(tmp_path):
    from trialerror.offload import protocol
    from tests._offload_fixtures import queue_one

    root = protocol.ensure_layout(tmp_path / "offload")
    queue_one(root, "JOB-a")
    for command in ("pull JOB-a", "push JOB-a", "publish JOB-a", "return JOB-a", "heartbeat JOB-a"):
        proc = _run_shell(command, root)
        assert proc.returncode == 4, f"{command} -> {proc.returncode} {proc.stderr}"
