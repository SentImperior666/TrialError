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
import re
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
    # C-0097 FIX V-11: the two names the claim directory reserves for a
    # worker's own status files. All three implementations of the id rule refuse
    # them -- this port, protocol.validate_job_id, and the wrapper's own case.
    ("claim CONTROL", EXIT_BAD_ID, "CONTROL is the control file's reserved name"),
    ("heartbeat JOB-a.progress", EXIT_BAD_ID, "a .progress id would shadow a status file"),
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


def _run_shell(
    command: str | None, offload_root: Path, **extra_env: str
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TE_OFFLOAD_ROOT"] = offload_root.as_posix()
    env["TE_OFFLOAD_WORKER_ID"] = "dev"
    env.update(extra_env)
    if command is None:
        env.pop("SSH_ORIGINAL_COMMAND", None)
    else:
        env["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(
        [sh_path, SHELL_SCRIPT.as_posix()],
        capture_output=True,
        text=True,
        # C-0097: `heartbeat` now reads an OPTIONAL payload from stdin. An empty
        # pipe is exactly what an ssh client that sends nothing produces;
        # inheriting pytest's own stdin would make the verb's behaviour depend
        # on how the suite happened to be invoked.
        input="",
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


# ===========================================================================
# C-0097 -- the control word and the progress payload, on the REAL wrapper
#
# Acceptance B of docs/reviews/WORKER_CONTROL_DESIGN.md. The whole control
# channel is a reply on a verb that already existed plus an optional stdin
# payload on the same verb, so these tests are where the design's claim "the
# seven-verb contract of the key is unchanged" is actually checked against the
# script that guards the key.
# ===========================================================================
def _ts(offset_s: float = 0.0) -> str:
    import datetime as _dt

    moment = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=offset_s)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_control(root, request: str, *, ts: str | None = None, worker_id: str = "dev") -> None:
    from trialerror.offload import protocol

    protocol.write_json(
        protocol.control_path(root, worker_id),
        {
            "schema": protocol.CONTROL_SCHEMA,
            "request": request,
            "by_launch": "LNCH-test",
            "ts": ts or _ts(),
            "job_id": None,
        },
    )


@requires_sh
def test_the_seven_verbs_are_still_exactly_seven():
    """C-0097 D8, asserted rather than trusted: the control channel must not
    have grown a verb. The script's own ``case`` is the source of truth, and the
    Python port's ``VERBS`` is pinned to it by the table above -- this checks
    the SCRIPT's text, so a new verb added only out there still fails here."""
    text = SHELL_SCRIPT.read_text(encoding="utf-8")
    accepted = re.search(r"\n    claim \| pull \| push \| publish \| return \| heartbeat\)", text)
    assert accepted, "the wrapper's verb case no longer lists exactly the six id-taking verbs"
    assert set(VERBS) == {"list", "claim", "pull", "push", "publish", "return", "heartbeat"}
    for forbidden in ("worker-control", "worker-status", "pause)", "stop)", "kill"):
        assert f"\n    {forbidden}" not in text, f"the wrapper grew a {forbidden!r} verb"


@requires_sh
def test_the_real_wrapper_prints_none_when_there_is_no_control_request(tmp_path):
    """The reply word is a WORD, never a blank line: a worker that read an
    empty reply could not tell "no request" from "the reply was lost"."""
    root = _claimed_root(tmp_path)
    proc = _run_shell("heartbeat JOB-a", root)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "none"


@requires_sh
@pytest.mark.parametrize("request_word", ["pause", "resume", "stop"])
def test_the_real_wrapper_prints_the_current_control_word(tmp_path, request_word):
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    _write_control(root, request_word)
    proc = _run_shell("heartbeat JOB-a", root)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == request_word
    # …and the stamp was still written: the reply is an addition, not a swap.
    stamp = protocol.claimed_dir(root) / "dev" / f"JOB-a{protocol.HEARTBEAT_SUFFIX}"
    assert stamp.is_file() and stamp.read_text(encoding="utf-8").strip()


@requires_sh
def test_the_real_wrapper_ignores_a_stale_control_request_and_says_so(tmp_path):
    """D1. Ignored on stdout (so the worker does nothing), reported on stderr
    (so the reason is not invisible) -- and the PYTHON reader sees the same
    word, which is what keeps the two halves one implementation."""
    from trialerror.offload.control import control_word

    root = _claimed_root(tmp_path)
    _write_control(root, "stop", ts="2020-01-01T00:00:00.000Z")
    proc = _run_shell("heartbeat JOB-a", root)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "none"
    assert "stale" in proc.stderr
    assert control_word(root, "dev") == "none"


@requires_sh
def test_the_real_wrapper_honours_the_ttl_knob(tmp_path):
    """The TTL is deployment-settable on both sides, so the acceptance for
    "stale is ignored" does not have to wait an hour."""
    root = _claimed_root(tmp_path)
    _write_control(root, "pause", ts=_ts(-120))
    assert _run_shell("heartbeat JOB-a", root).stdout.strip() == "pause"
    fresh = _run_shell("heartbeat JOB-a", root, TE_OFFLOAD_CONTROL_TTL_S="60")
    assert fresh.stdout.strip() == "none"
    assert "stale" in fresh.stderr


@requires_sh
def test_the_real_wrapper_ignores_an_unparseable_control_request(tmp_path):
    """Acting on a half-written file would be worse than ignoring it."""
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    path = protocol.control_path(root, "dev")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"request": "pau', encoding="utf-8")
    assert _run_shell("heartbeat JOB-a", root).stdout.strip() == "none"
    _write_control(root, "kill")
    assert _run_shell("heartbeat JOB-a", root).stdout.strip() == "none"


@requires_sh
def test_the_real_wrapper_stores_a_heartbeat_payload(tmp_path):
    """D3. The wrapper writes the BYTES it was handed -- it has no JSON parser
    -- so this also pins the filename the dashboard and the doctor read."""
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    payload = b'{"worker_id":"dev","state":"running","units_done":3,"units_total":9,"unit":"chunk"}'
    proc = _run_shell_bytes("heartbeat JOB-a", root, payload)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().strip() == "none"
    stored = protocol.progress_path(root, "dev", "JOB-a")
    assert stored.read_bytes() == payload
    # the temp buffer is not left behind
    assert not (protocol.claimed_dir(root) / "dev" / ".JOB-a.progress-tmp").exists()


@requires_sh
def test_the_real_wrapper_accepts_the_idle_slot_without_a_claim(tmp_path):
    """The one id ``claimed_or_die`` does not apply to (D3): a worker with
    nothing claimed still has to be able to say "I am here and idle", and there
    is no manifest to check ownership against. The id is a fixed prefix plus
    THIS key's own worker id, so the hole is into a directory the key owns."""
    from trialerror.offload import protocol

    root = protocol.ensure_layout(tmp_path / "offload")
    payload = b'{"worker_id":"dev","state":"idle","units_done":0}'
    proc = _run_shell_bytes("heartbeat WORKER-dev", root, payload)
    assert proc.returncode == 0, proc.stderr
    assert protocol.progress_path(root, "dev", "WORKER-dev").read_bytes() == payload
    # …and it is NOT a claim: the queue still says nobody holds anything.
    assert protocol.list_claims(root) == []
    assert protocol.counts(root)["claimed"] == 0
    # any OTHER unclaimed id is still refused
    assert _run_shell_bytes("heartbeat WORKER-other", root, payload).returncode == 4
    assert _run_shell_bytes("heartbeat JOB-nope", root, payload).returncode == 4


@requires_sh
def test_the_real_wrapper_refuses_an_over_large_heartbeat_payload(tmp_path):
    """D3's cap, with the same posture as SEC-4's transfer caps: the verb FAILS
    rather than storing a truncated status, AND the stamp is left untouched --
    a status file the sandbox cannot trust must not also cost the job its
    claim."""
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    stamp = protocol.claimed_dir(root) / "dev" / f"JOB-a{protocol.HEARTBEAT_SUFFIX}"
    before = stamp.read_text(encoding="utf-8")
    proc = _run_shell_bytes("heartbeat JOB-a", root, b'{"last_error":"' + b"x" * 5000 + b'"}')
    assert proc.returncode == 4
    assert b"cap" in proc.stderr
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()
    assert stamp.read_text(encoding="utf-8") == before


@requires_sh
@pytest.mark.parametrize(
    "payload,expect",
    [
        (b"[1,2]", b"one JSON object"),
        (b'"a string"', b"one JSON object"),
        (b"not json at all", b"one JSON object"),
        (b'{"nope":1}', b"unknown key"),
        (b'{"worker_id":"dev","units":1}', b"unknown key"),
    ],
    ids=["array", "string", "garbage", "unknown-key", "near-miss-key"],
)
def test_the_real_wrapper_refuses_a_malformed_heartbeat_payload(tmp_path, payload, expect):
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    stamp = protocol.claimed_dir(root) / "dev" / f"JOB-a{protocol.HEARTBEAT_SUFFIX}"
    before = stamp.read_text(encoding="utf-8")
    proc = _run_shell_bytes("heartbeat JOB-a", root, payload)
    assert proc.returncode == 4, proc.stderr
    assert expect in proc.stderr
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()
    assert stamp.read_text(encoding="utf-8") == before


@requires_sh
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b'{"worker_id":"dev","state":"paused","units_done":1,"settings":{"batch_size":4,"model_key":"k","resident_backends":"embed"}}',
        b'{\n  "worker_id": "dev",\n  "state": "running"\n}\n',
        b'{"worker_id":"dev","rm -rf /":1}',
        b"[1,2]",
        b'{"nope":1}',
        b'{"last_error":"' + b"x" * 5000 + b'"}',
        # FIX V-8: the shapes the two halves used to disagree about. A blank
        # payload was "no payload" to the port (``bytes.strip()``) and "must be
        # one JSON object" to the wrapper; a lone vertical tab was the same
        # disagreement in the other direction, because ``tr -d ' \t\r\n``
        # does not strip ``\v``. Four bytes, one rule, both halves.
        b"   ",
        b"\n\n",
        b"\x0b",
    ],
    ids=[
        "empty", "compact", "pretty-printed", "non-identifier-key", "array", "unknown-key",
        "over-cap", "blank", "newlines-only", "vertical-tab",
    ],
)
def test_the_payload_verdict_is_the_same_in_both_halves(tmp_path, payload):
    """ONE rule, two implementations -- the same discipline the refusal table
    above applies to the verb parser, now applied to the payload gate. The
    Python port is the oracle the in-process transport uses; the script is what
    runs on the queue host. They must agree on every shape, including the two
    the rule deliberately ACCEPTS (an empty payload, a key with a space in it
    that is not a key token to either side)."""
    from trialerror.offload.shell import progress_payload_refusal

    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    python_refuses = progress_payload_refusal(payload) is not None
    shell_refuses = _run_shell_bytes("heartbeat JOB-a", root, payload).returncode != 0
    assert python_refuses == shell_refuses, (
        f"the two halves disagree about {payload[:40]!r}: python_refuses={python_refuses}, "
        f"shell_refuses={shell_refuses}"
    )
    # …and they agree about whether a FILE results, not only about the exit code:
    # a payload that is not a payload must leave nothing behind on either side
    # (FIX V-8 -- the port used to write an empty progress file the row then
    # reported as state `unknown`).
    wrote = protocol.progress_path(root, "dev", "JOB-a").exists()
    if not python_refuses:
        protocol.progress_path(root, "dev", "JOB-a").unlink(missing_ok=True)
        protocol.server_heartbeat(root, "JOB-a", worker_id="dev", progress=payload)
        assert protocol.progress_path(root, "dev", "JOB-a").exists() == wrote, (
            f"the two halves disagree about STORING {payload[:40]!r}"
        )


@requires_sh
def test_publish_and_return_clear_the_progress_file(tmp_path):
    """A progress file left behind past its claim would keep a finished job on
    the dashboard as a live worker row until it aged into ``lost``. Both exits
    from a claim drop it; the per-worker CONTROL.json deliberately survives,
    because a pause is a standing instruction to the WORKER."""
    from trialerror.offload import protocol

    root = _claimed_root(tmp_path)
    _write_control(root, "pause")
    payload = b'{"worker_id":"dev","state":"running"}'
    assert _run_shell_bytes("heartbeat JOB-a", root, payload).returncode == 0
    assert protocol.progress_path(root, "dev", "JOB-a").is_file()
    assert _run_shell("return JOB-a", root).returncode == 0
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()
    assert protocol.control_path(root, "dev").is_file()

    protocol.server_claim(root, "JOB-a", worker_id="dev")
    assert _run_shell_bytes("heartbeat JOB-a", root, payload).returncode == 0
    assert _run_shell_bytes("push JOB-a", root, _tar_bytes({"pages.json": b"{}"})).returncode == 0
    assert _run_shell("publish JOB-a", root).returncode == 0
    assert not protocol.progress_path(root, "dev", "JOB-a").exists()
    assert protocol.control_path(root, "dev").is_file()
