"""Round 6: the bounded auth grace, and ``vastai ssh-probe``.

The canary of 2026-09-19 was told ``Permission denied (publickey)`` by a host
whose sshd had completed an auth exchange, and the account's key list read back
``registered`` an hour later. Both readings are true only if something other
than registration refused the key -- most cheaply explained by the instance not
having the account's keys in place yet. The ported code believed the first
refusal ("retrying only bills"), which was right while an unregistered key was
the likely cause and is not any more, now that the pre-flight asks the account
for free BEFORE the create.

So: ``wait_reachable`` retries a refusal for a bounded time AND a bounded
number of attempts, and ``vastai ssh-probe`` buys the discriminating answer for
a few cents -- the cheapest offer this root admits, ssh, ``nvidia-smi -L``,
destroy -- while keeping only an allow-list of ``ssh -v`` lines.

Everything here runs on fakes: no vast.ai call, no ssh, no GPU, no key
material. The fake shell records every command it is asked to run, which is how
"the probe uploads nothing" is a test and not a claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import trialerror.cli.vastai as cli_vastai
from tests._vastai_fakes import (  # noqa: F401 - fixtures
    FakeVast,
    default_offers,
    dev_toml,
    isolated_state,
    make_offer,
    network_tripwire,
    toml_text,
    write_key,
)
from trialerror.cli import build_parser
from trialerror.vastai import shell as sh
from trialerror.vastai import sshprobe
from trialerror.vastai.api import VastClient

def _pub_line(seed: bytes, comment: str = "te-vastai test") -> str:
    """A well-formed openssh public-key line over a synthetic blob. No real key
    material is read or written anywhere in this file."""
    import base64
    import hashlib

    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + hashlib.sha256(seed).digest()
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} {comment}"


DENIED = "root@ssh2.vast.ai: Permission denied (publickey)."
#: A verbose transcript of the live refusal, line for line as OpenSSH writes it
#: (the paths and fingerprints are synthetic).
VERBOSE_DENIED = """OpenSSH_10.2p1, OpenSSL 3.5.4
debug1: Connecting to ssh2.vast.ai [203.0.113.7] port 12345.
debug1: Connection established.
debug1: identity file C:/keys/vastai_ed25519 type 3
debug1: Local version string SSH-2.0-OpenSSH_10.2
debug1: Remote protocol version 2.0, remote software version OpenSSH_8.9p1 Ubuntu-3ubuntu0.13
debug1: Server host key: ssh-ed25519 SHA256:dGVzdGhvc3RrZXlmaW5nZXJwcmludGZha2VmYWtlZmFrZWU
debug1: Offering public key: C:/keys/vastai_ed25519 ED25519 SHA256:7SHgFt7ZuTpLxgPVlBXpBuFGimmm05EyE9GJJjUO00g explicit
debug1: Authentications that can continue: publickey
debug1: No more authentication methods to try.
root@ssh2.vast.ai: Permission denied (publickey).
"""


# ---------------------------------------------------------------------------
# the filter
# ---------------------------------------------------------------------------
def test_the_filter_keeps_the_diagnostic_lines_and_drops_the_rest():
    kept, dropped = sshprobe.sanitize_verbose(VERBOSE_DENIED)
    joined = "\n".join(kept)
    for wanted in ("Connecting to", "identity file", "Local version string", "Remote protocol version",
                   "Server host key", "Offering public key", "Authentications that can continue",
                   "Permission denied"):
        assert wanted in joined, wanted
    assert dropped >= 1, "the version banner is not on the allow-list"
    assert "OpenSSL" not in joined


def test_a_line_carrying_a_key_blob_is_dropped_even_when_it_matches():
    blob = "A" * 80
    kept, dropped = sshprobe.sanitize_verbose(f"debug1: Offering public key: ssh-ed25519 {blob}\n")
    assert kept == []
    assert dropped == 1


def test_a_fingerprint_is_short_enough_to_survive():
    line = "debug1: Offering public key: C:/k ED25519 SHA256:7SHgFt7ZuTpLxgPVlBXpBuFGimmm05EyE9GJJjUO00g explicit"
    kept, _dropped = sshprobe.sanitize_verbose(line)
    assert kept == [line]


def test_the_filter_reports_what_it_dropped_so_it_cannot_hide_a_failure():
    kept, dropped = sshprobe.sanitize_verbose("something entirely unexpected\nand another line\n")
    assert kept == []
    assert dropped == 2


# ---------------------------------------------------------------------------
# the bounded auth grace in wait_reachable
# ---------------------------------------------------------------------------
class ScriptedShell:
    """A shell that answers ``run`` from a script of ``(rc, stdout, stderr)``,
    repeating the last entry. Records every command it was asked to run.

    It answers at most ``max_runs`` of them and then raises: every test here
    needs a handful, and a caller that asks for more has lost its bound. That
    is what the bounds below are FOR, so a mutant that removes one must fail
    fast rather than hang: with the attempt bound removed,
    ``test_a_stuck_clock_still_terminates`` used to spin forever."""

    def __init__(self, script, *, gpu=(0, b"GPU 0: NVIDIA GeForce RTX 3090 (UUID: GPU-f4ke)\n", b""),
                 max_runs=20, **kwargs):
        self.script = list(script)
        self.gpu = gpu
        self.cmds: list[str] = []
        self.max_runs = int(max_runs)
        self.kwargs = kwargs
        self._i = 0

    def run(self, cmd, *, stdin_bytes=None, timeout_s):
        if len(self.cmds) >= self.max_runs:
            raise RuntimeError(
                f"ScriptedShell was asked for more than {self.max_runs} commands ({cmd!r}): the caller is "
                "retrying without a bound"
            )
        self.cmds.append(cmd)
        if cmd != "true":
            return self.gpu
        step = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        return step


def _clock():
    """A clock that advances one second per reading (so a grace can elapse)."""
    state = {"t": 0.0}

    def now() -> float:
        state["t"] += 1.0
        return state["t"]

    return now


def _wait(shell, **kwargs):
    kwargs.setdefault("check", lambda: None)
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("clock", _clock())
    kwargs.setdefault("timeout_s", 600.0)
    return sh.wait_reachable(shell, **kwargs)


def test_without_a_grace_the_first_refusal_raises_as_the_ported_code_did():
    shell = ScriptedShell([(255, b"", DENIED.encode())])
    with pytest.raises(sh.IdentityRejected) as exc:
        _wait(shell)
    assert shell.cmds == ["true"], "the ported behaviour: exactly one attempt"
    assert "attempt(s) over" not in str(exc.value)


def test_a_refusal_that_clears_on_a_later_attempt_is_not_a_refusal():
    shell = ScriptedShell([(255, b"", DENIED.encode()), (0, b"", b"")])
    _wait(shell, auth_grace_s=90.0)
    assert shell.cmds == ["true", "true"]


def test_a_refusal_that_never_clears_raises_after_the_bound_and_says_so():
    shell = ScriptedShell([(255, b"", DENIED.encode())])
    with pytest.raises(sh.IdentityRejected) as exc:
        _wait(shell, auth_grace_s=90.0)
    assert len(shell.cmds) == sh.SSH_AUTH_ATTEMPTS
    assert "3 attempt(s) over" in str(exc.value)


def test_the_grace_is_bounded_by_time_as_well_as_by_attempts():
    shell = ScriptedShell([(255, b"", DENIED.encode())])
    with pytest.raises(sh.IdentityRejected):
        _wait(shell, auth_grace_s=0.5, auth_attempts=99)
    assert len(shell.cmds) == 2, "one refusal, then the grace is already spent"


def test_a_stuck_clock_still_terminates():
    shell = ScriptedShell([(255, b"", DENIED.encode())])
    with pytest.raises(sh.IdentityRejected):
        _wait(shell, auth_grace_s=1e9, clock=lambda: 1.0)
    assert len(shell.cmds) == sh.SSH_AUTH_ATTEMPTS


def test_the_scripted_shell_answers_only_a_bounded_number_of_commands():
    """With the attempt bound removed from ``wait_reachable``, the test
    above used to HANG -- which CI reports as a stuck job, not as the failure it
    is. The fake shell stops answering instead, so that mutant FAILS."""
    shell = ScriptedShell([(255, b"", DENIED.encode())], max_runs=4)
    for _ in range(4):
        shell.run("true", timeout_s=1.0)
    with pytest.raises(RuntimeError) as exc:
        shell.run("true", timeout_s=1.0)
    assert "more than 4" in str(exc.value)
    assert len(shell.cmds) == 4, "the command it refused to answer is not recorded as answered"


def test_the_lease_check_still_governs_every_attempt():
    shell = ScriptedShell([(255, b"", DENIED.encode())])
    calls = {"n": 0}

    def check() -> None:
        calls["n"] += 1

    with pytest.raises(sh.IdentityRejected):
        _wait(shell, check=check, auth_grace_s=90.0)
    assert calls["n"] == sh.SSH_AUTH_ATTEMPTS


def test_a_connection_that_is_not_an_auth_answer_is_still_retried_to_the_timeout():
    shell = ScriptedShell([(255, b"", b"ssh: connect to host h port 1: Connection refused")])
    with pytest.raises(sh.ConnectionLost) as exc:
        _wait(shell, timeout_s=3.0, auth_grace_s=90.0)
    assert "not reachable after" in str(exc.value)
    assert len(shell.cmds) > 1


def test_the_default_shell_carries_the_grace_so_the_backend_gets_it(tmp_path):
    shell = sh.SshShell(host="h", port=22, identity_path=tmp_path / "id", known_hosts=tmp_path / "kh")
    assert shell.auth_grace_s == sh.SSH_AUTH_GRACE_S > 0


# ---------------------------------------------------------------------------
# -v is opt-in and changes nothing else
# ---------------------------------------------------------------------------
def test_the_hardened_argv_is_quiet_by_default(tmp_path):
    argv = sh.SshShell(host="h", port=22, identity_path=tmp_path / "id", known_hosts=tmp_path / "kh").argv("true")
    assert "-v" not in argv
    assert "LogLevel=ERROR" in argv
    assert "LogLevel=DEBUG1" not in argv


def test_verbose_adds_v_and_raises_the_log_level_and_keeps_the_hardening(tmp_path):
    quiet = sh.SshShell(host="h", port=22, identity_path=tmp_path / "id", known_hosts=tmp_path / "kh").argv("true")
    loud = sh.SshShell(host="h", port=22, identity_path=tmp_path / "id", known_hosts=tmp_path / "kh",
                       verbose=True).argv("true")
    assert "-v" in loud
    assert "LogLevel=DEBUG1" in loud and "LogLevel=ERROR" not in loud
    for option in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "ForwardAgent=no"):
        assert option in loud, option
    assert [a for a in quiet if a != "LogLevel=ERROR"] == [a for a in loud if a not in ("-v", "LogLevel=DEBUG1")]


# ---------------------------------------------------------------------------
# run_ssh_probe
# ---------------------------------------------------------------------------
def _probe(shell, **kwargs):
    kwargs.setdefault("check", lambda: None)
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("clock", _clock())
    kwargs.setdefault("timeout_s", 600.0)
    return sshprobe.run_ssh_probe(shell, **kwargs)


def test_a_probe_that_gets_in_reports_the_card_and_uploads_nothing():
    shell = ScriptedShell([(0, b"", b"")])
    out = _probe(shell)
    assert out.authenticated and out.verdict == "authenticated"
    assert out.gpu and "RTX 3090" in out.gpu
    assert shell.cmds == ["true", "nvidia-smi -L"], "a probe runs these two commands and nothing else"
    assert "first attempt" in out.reading()


def test_a_probe_that_gets_in_after_a_refusal_names_the_race():
    shell = ScriptedShell([(255, b"", VERBOSE_DENIED.encode()), (0, b"", b"")])
    out = _probe(shell, auth_grace_s=90.0)
    assert out.authenticated
    assert out.denials == 1
    assert "RACE" in out.reading()
    assert out.attempts[0].lines, "the refused attempt's evidence is kept"


def test_a_probe_that_is_always_refused_says_it_is_not_a_race():
    shell = ScriptedShell([(255, b"", VERBOSE_DENIED.encode())])
    out = _probe(shell, auth_grace_s=90.0)
    assert not out.authenticated
    assert out.verdict == "identity-rejected"
    assert out.denials == sh.SSH_AUTH_ATTEMPTS
    assert "NOT a race" in out.reading()
    assert any("Offering public key" in line for a in out.attempts for line in a.lines)
    assert shell.cmds == ["true"] * sh.SSH_AUTH_ATTEMPTS, "nothing is run on a host that refused"


def test_a_probe_that_never_connects_is_not_an_auth_verdict():
    shell = ScriptedShell([(255, b"", b"kex_exchange_identification: Connection closed by remote host")])
    out = _probe(shell, timeout_s=3.0)
    assert out.verdict == "connection-lost"
    assert not out.authenticated


def test_a_card_check_that_fails_is_reported_without_unsaying_the_auth():
    shell = ScriptedShell([(0, b"", b"")], gpu=(127, b"", b"bash: nvidia-smi: command not found"))
    out = _probe(shell)
    assert out.authenticated, "ssh worked; the card is a separate question"
    assert out.error_kind == "gpu-check-failed"


def test_the_error_text_of_a_refusal_passes_the_same_allow_list():
    """``shell.wait_reachable`` embeds ``stderr[-400:]`` verbatim in the
    message it raises, and that message becomes ``ProbeOutcome.error`` -- the
    field an operator reads first, the envelope carries and ``--log-file``
    records. It must pass the allow-list this module's contract rests on."""
    blob = "D" * 90
    stderr = f"debug1: Offering public key: x {blob}\n{DENIED}\n"
    out = _probe(ScriptedShell([(255, b"", stderr.encode())]), auth_grace_s=90.0)
    assert out.verdict == "identity-rejected"
    assert out.error and blob not in out.error, "the raw ssh tail reached the error field"
    assert "Permission denied" in out.error, "the allow-listed line still says what happened"
    assert blob not in json.dumps(out.as_dict())


def test_an_error_with_no_allow_listed_line_is_named_rather_than_quoted():
    """The same invariant where the filter keeps nothing: the class and the
    count of dropped lines stand in, so a filtered-away message is still
    visible AS one (the module's own rule for its kept lines)."""
    out = _probe(ScriptedShell([(255, b"", b"ssh: a message no entry matches\n")]), timeout_s=3.0)
    assert out.verdict == "connection-lost"
    assert out.error and "no entry matches" not in out.error
    assert out.error.startswith("ConnectionLost") and "dropped" in out.error


def test_the_probe_log_carries_the_kept_lines_and_never_a_blob():
    lines: list[str] = []
    blob = "B" * 90
    shell = ScriptedShell([(255, b"", f"debug1: Offering public key: x {blob}\n{DENIED}".encode()), (0, b"", b"")])
    _probe(shell, auth_grace_s=90.0, log=lines.append)
    text = "\n".join(lines)
    assert "Permission denied" in text
    assert blob not in text


# ---------------------------------------------------------------------------
# the CLI: a dry run costs nothing, --rent rents once and destroys
# ---------------------------------------------------------------------------
def _root(tmp_path, **kwargs) -> Path:
    root = tmp_path / "devroot"
    write_key(root / "keys")
    (root / "keys" / "vastai_ed25519").write_text("not a key: the probe never opens it\n", encoding="utf-8")
    (root / "trialerror.toml").write_text(toml_text(dev_toml(**kwargs)), encoding="utf-8")
    return root


def _fake_client(monkeypatch, fake: FakeVast) -> None:
    monkeypatch.setattr(cli_vastai, "_client_factory", lambda key_path: VastClient(key_path, http=fake.http))


def _fake_shell(monkeypatch, script, **gpu):
    made: list[ScriptedShell] = []

    def factory(**kwargs):
        shell = ScriptedShell(script, **({**gpu, **kwargs}))
        made.append(shell)
        return shell

    monkeypatch.setattr(cli_vastai, "_shell_factory", factory)
    monkeypatch.setattr(cli_vastai, "_probe_sleep", lambda _s: None)
    return made


def _run(*argv: str) -> dict:
    args = build_parser().parse_args(["vastai", *argv])
    return args.handler(args)


@pytest.fixture()
def probe_env(tmp_path, monkeypatch, isolated_state, network_tripwire):  # noqa: F811
    root = _root(tmp_path)
    fake = FakeVast()
    _fake_client(monkeypatch, fake)
    return root, fake


def test_without_rent_nothing_is_created_and_the_plan_is_priced(probe_env):
    root, fake = probe_env
    env = _run("ssh-probe", "--backend-config-root", str(root))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["rented"] is False and result["verdict"] == "would-rent"
    assert fake.instances == {}, "a dry run rents nothing"
    assert not [c for c in fake.calls if c[0] in ("PUT", "DELETE")]
    assert result["worst_usd"] > 0 and result["ttl_s"] > 0
    assert result["uploads"].startswith("nothing")


def test_rent_without_a_ceiling_is_refused_before_anything_is_created(probe_env):
    root, fake = probe_env
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent")
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_arguments"
    assert fake.instances == {}


def test_a_ceiling_the_probe_would_cross_is_a_named_refusal(probe_env):
    root, fake = probe_env
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "0.0001")
    assert env["ok"] is False
    assert env["error"]["code"] == "cap-probe"
    assert "crosses the probe cap" in env["error"]["message"]
    assert fake.instances == {}


def test_an_account_that_does_not_hold_the_key_is_refused_for_free(tmp_path, monkeypatch, isolated_state,  # noqa: F811
                                                                  network_tripwire):  # noqa: F811
    root = _root(tmp_path)
    (root / "keys" / "vastai_ed25519.pub").write_text(_pub_line(b"the configured identity") + "\n", encoding="utf-8")
    fake = FakeVast()
    fake.account_ssh_keys = [_pub_line(b"somebody else's key")]
    _fake_client(monkeypatch, fake)
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00")
    assert env["ok"] is False
    assert env["error"]["code"] == "key-missing"
    assert "Nothing was rented" in env["error"]["message"]
    assert fake.instances == {}


def test_a_probe_that_authenticates_rents_once_destroys_and_records_it(probe_env, monkeypatch):
    root, fake = probe_env
    made = _fake_shell(monkeypatch, [(0, b"", b"")])
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00", "--json")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["verdict"] == "authenticated"
    assert result["rented"] is True and result["destroyed"] is True and result["destroy_confirmed"] is True
    assert fake.instances == {}, "the instance is destroyed on the way out"
    assert result["estimated_cost_usd"] >= 0
    assert made and made[0].kwargs["verbose"] is True, "the probe asks for -v"
    assert made[0].cmds == ["true", "nvidia-smi -L"]

    from trialerror.vastai.ledger import Ledger

    rows = Ledger().read().rows
    kinds = [r["kind"] for r in rows]
    assert kinds == ["intent", "outcome"]
    assert rows[0]["probe"] == "ssh" and rows[0]["shipped"] is False
    assert rows[0]["bytes"] == 0, "a probe sends no bytes, and the row says so"
    assert rows[0]["sha256"] == sshprobe.NOTHING_SHA256
    assert rows[1]["result"] == "returned" and rows[1]["probe_verdict"] == "authenticated"
    assert rows[1]["ssh_authenticated"] is True
    assert rows[1]["destroyed"] is True


def test_a_probe_that_is_refused_still_destroys_and_the_row_says_so(probe_env, monkeypatch):
    root, fake = probe_env
    _fake_shell(monkeypatch, [(255, b"", VERBOSE_DENIED.encode())])
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00", "--json")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["verdict"] == "identity-rejected"
    assert result["destroyed"] is True and fake.instances == {}
    # The destroy is CONFIRMED by reading the instance list back, not merely
    # requested -- on this path as on the authenticated one.
    assert result["destroy_confirmed"] is True
    assert result["probe"]["denials"] == sh.SSH_AUTH_ATTEMPTS
    assert "NOT a race" in result["reading"]

    from trialerror.vastai.ledger import Ledger

    outcome = [r for r in Ledger().read().rows if r["kind"] == "outcome"][0]
    assert outcome["destroy_confirmed"], "the row carries WHEN the destroy was confirmed"
    assert outcome["result"] == "returned" and outcome["probe_verdict"] == "identity-rejected"
    assert outcome["ssh_authenticated"] is False
    assert outcome["shipped"] is False


def test_an_instance_that_never_runs_is_an_envelope_and_not_a_traceback(probe_env, monkeypatch):
    """``lease.wait_ready`` raises ``HostFailure`` when the instance
    enters ``exited`` (or never reaches ``running`` inside the startup window).
    The instance was always destroyed and the outcome row always written -- the
    operator just got a traceback instead of the envelope that says so."""
    root, fake = probe_env
    fake.exit_on_boot = True
    made = _fake_shell(monkeypatch, [(0, b"", b"")])
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00", "--json")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["verdict"] == "host-failure"
    assert result["rented"] is True
    assert result["destroyed"] is True and result["destroy_confirmed"] is True
    assert fake.instances == {}, "the instance is destroyed on the way out"
    assert result["probe"] is None and "host-failure" in result["reading"]
    assert [c for shell in made for c in shell.cmds] == [], "no ssh is attempted on a host that never ran"
    assert any("host-failure" in line or "before it was ready" in line for line in result["log"])

    from trialerror.vastai.ledger import Ledger

    outcome = [r for r in Ledger().read().rows if r["kind"] == "outcome"][0]
    assert outcome["probe_verdict"] == "host-failure" and outcome["shipped"] is False
    assert outcome["ssh_authenticated"] is False and outcome["destroyed"] is True


def test_the_high_tier_needs_its_own_approval_before_the_probe_rents(tmp_path, monkeypatch,  # noqa: F811
                                                                    isolated_state, network_tripwire):  # noqa: F811
    """The guard is on the renting path of this verb too, and it
    refuses before ANY vast.ai call -- so the most expensive rentals stay
    behind the operator's own approval on the probe as on `run`."""
    root = _root(tmp_path, vastai={"tier": "high"})
    fake = FakeVast()
    _fake_client(monkeypatch, fake)
    # A fake shell nothing should reach: it is here so that a tree whose guard
    # has been removed FAILS these assertions instead of attempting an ssh.
    made = _fake_shell(monkeypatch, [(0, b"", b"")])
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00")
    assert env["ok"] is False
    assert made == [], "no shell is even built"
    # The guard's own reason code, not the handler's default: which condition
    # failed is the operator's next action.
    assert env["error"]["code"] == "approval-missing"
    assert "approve-high" in json.dumps(env)
    assert fake.instances == {}
    assert fake.calls == [], "nothing is even asked of vast.ai: the refusal is free"


def test_the_log_file_is_written_while_it_runs_and_carries_no_blob(probe_env, monkeypatch, tmp_path):
    root, _fake = probe_env
    blob = "C" * 90
    _fake_shell(monkeypatch, [(255, b"", f"debug1: Offering public key: x {blob}\n{DENIED}".encode()), (0, b"", b"")])
    log = tmp_path / "live" / "probe.log"
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00", "--log-file", str(log))
    assert env["ok"] is True, env
    text = log.read_text(encoding="utf-8")
    assert "ssh attempt 1" in text and "Permission denied" in text
    assert blob not in text
    assert env["result"]["verdict"] == "authenticated"


def test_a_log_file_that_cannot_be_written_is_refused_before_anything_is_rented(probe_env, tmp_path):
    root, fake = probe_env
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory\n", encoding="utf-8")
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00",
               "--log-file", str(blocker / "probe.log"))
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_arguments"
    assert fake.instances == {}


def test_the_probe_rents_only_a_host_this_roots_egress_policy_admits(tmp_path, monkeypatch, isolated_state,  # noqa: F811
                                                                    network_tripwire):  # noqa: F811
    """The same ``offer_refusals`` the canary uses: a policy that requires a
    datacentre host refuses a market of non-datacentre offers, and the probe
    rents nothing."""
    root = _root(tmp_path, egress={"require_datacenter": True})
    offers = [make_offer(i, hosting_type=0) for i in (1, 2)]
    for offer in offers:
        offer.pop("datacenter", None)
    fake = FakeVast(offers, ignore_filters=True)
    _fake_client(monkeypatch, fake)
    env = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00")
    assert env["ok"] is False
    assert env["error"]["code"] == "no-offer"
    assert fake.instances == {}


def test_the_probe_takes_the_cheapest_admitted_offer(tmp_path, monkeypatch, isolated_state,  # noqa: F811
                                                     network_tripwire):  # noqa: F811
    root = _root(tmp_path)
    dear = make_offer(1, dph_base=0.40)
    cheap = make_offer(2, dph_base=0.10)
    fake = FakeVast([dear, cheap], ignore_filters=True)
    _fake_client(monkeypatch, fake)
    env = _run("ssh-probe", "--backend-config-root", str(root), "--json")
    assert env["result"]["offer"]["offer_id"] == 2


def test_the_json_result_keeps_every_attempt_and_the_short_form_does_not(probe_env, monkeypatch):
    root, _fake = probe_env
    _fake_shell(monkeypatch, [(0, b"", b"")])
    full = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00", "--json")
    short = _run("ssh-probe", "--backend-config-root", str(root), "--rent", "--max-usd", "1.00")
    assert "attempts" in full["result"]["probe"]
    assert "attempts" not in short["result"]["probe"]
    assert json.dumps(full["result"])  # the whole envelope stays JSON
