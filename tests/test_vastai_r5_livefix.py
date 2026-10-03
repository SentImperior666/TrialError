"""Round 5: the two false assumptions the first live canary found.

**C4, the ssh identity.** Neither this lane nor the public embedding lane it
was ported from sends a public key to vast.ai -- both create calls carry
``client_id/image/disk/label/onstart/runtype`` and nothing else -- so both
depend on vast.ai putting the ACCOUNT's registered keys on a new instance. On
2026-09-19 the account refused the configured identity, and the answer arrived
313 s and $0.0309 INTO a rental. These tests pin the free pre-flight that now
asks the same question before any create, and pin that only a positive absence
refuses.

**C1, the datacentre field.** The API-side filter ``datacenter: {eq: True}``
returned 8 offers whose objects carry no truthy ``datacenter`` field, so the
post-check refused every one of them. These tests pin the ONE constant that
now maps the field, each shape it supports, and the refusal that names what it
read -- and the ``plan --offer-keys`` aid that will settle the mapping on one
free read.

**C4's window B.** The renting worker printed nothing at all until it exited.
These tests pin a log sink that writes each line as it happens.

Everything runs on fakes: no vast.ai call, no ssh, no GPU, no key material.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest

import trialerror.cli.offload as cli_offload
import trialerror.cli.vastai as cli_vastai
from tests._vastai_fakes import (  # noqa: F401 - fixtures
    FakeVast,
    dev_toml,
    isolated_state,
    make_offer,
    network_tripwire,
    toml_text,
    write_key,
)
from tests._vastai_shell_fakes import make_env, run_env, ssh_tripwire  # noqa: F401 - fixture
from trialerror.cli import build_parser
from trialerror.vastai import sshkeys
from trialerror.vastai.api import VastClient
from trialerror.vastai.errors import VastPlanRefused
from trialerror.vastai.pricing import (
    DATACENTER_FIELDS,
    OFFER_DEBUG_FIELDS,
    datacenter_signal,
    offer_debug_rows,
    offer_refusals,
)

A4_PT = (595.0, 842.0)


def _pub_line(seed: bytes, comment: str = "vastai test") -> str:
    """A well-formed openssh public-key line over a synthetic blob. No real
    key material is read or written anywhere in this file."""
    key = hashlib.sha256(seed).digest()
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + key
    return f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')} {comment}"


MINE = _pub_line(b"the configured identity")
OTHER = _pub_line(b"some other key on the account")


# ---------------------------------------------------------------------------
# C4: the fingerprint, the payload shapes, the pre-flight
# ---------------------------------------------------------------------------
def test_the_fingerprint_is_openssh_shaped_and_ignores_the_comment():
    fp = sshkeys.fingerprint(MINE)
    blob = base64.b64decode(MINE.split()[1])
    assert fp == "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    assert not fp.endswith("=") and len(fp) == len("SHA256:") + 43
    # the same key under another label is the same key
    assert sshkeys.fingerprint(MINE.rsplit(" ", 1)[0] + " renamed-in-the-console") == fp
    assert sshkeys.fingerprint("not a key") is None
    assert sshkeys.fingerprint(None) is None


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([{"id": 1, "ssh_key": MINE}], [MINE]),                     # a list of key objects
        ({"ssh_keys": [{"ssh_key": MINE}, {"ssh_key": OTHER}]}, [MINE, OTHER]),
        ({"results": [MINE]}, [MINE]),                              # a paged list of lines
        ({"ssh_key": MINE}, [MINE]),                                # the user-object shape
        ([], []),                                                   # an account with NO key: an answer
        ({"credit": 24.78}, None),                                  # not a shape we read
        ("nonsense", None),
        (None, None),
    ],
)
def test_every_account_key_shape_this_version_reads(payload, expected):
    assert sshkeys.public_keys_in_payload(payload) == expected


class _Client:
    def __init__(self, keys):
        self._keys = keys

    def ssh_keys(self):
        if isinstance(self._keys, Exception):
            raise self._keys
        return self._keys


def test_the_preflight_passes_when_the_identity_is_registered(tmp_path):
    identity = tmp_path / "vastai_ed25519"
    sshkeys.public_half_path(identity).write_text(MINE + "\n", encoding="utf-8")
    check = sshkeys.check_ssh_key(_Client([OTHER, MINE]), identity)
    assert check.state == "registered" and check.refuses is False
    assert check.fingerprint == sshkeys.fingerprint(MINE) and check.registered_count == 2


def test_the_preflight_refuses_a_key_the_account_does_not_carry(tmp_path):
    """The C4 case, for $0: the account answered, and our fingerprint is not
    in it. The message names the fingerprint and the console page, never a
    key."""
    identity = tmp_path / "vastai_ed25519"
    sshkeys.public_half_path(identity).write_text(MINE + "\n", encoding="utf-8")
    check = sshkeys.check_ssh_key(_Client([OTHER]), identity)
    assert check.state == "not-registered" and check.refuses is True
    assert check.fingerprint in check.message() and sshkeys.CONSOLE_PAGE in check.message()
    assert MINE.split()[1] not in check.message()
    assert any("register" in a for a in check.next_actions())


@pytest.mark.parametrize(
    "keys, pub, why",
    [
        (None, MINE, "shape"),                       # the endpoint answered nothing we read
        (RuntimeError("HTTP 500"), MINE, "read"),    # the endpoint failed
        ([MINE], None, "public half"),               # no .pub beside the identity
        ([MINE], "not a key line", "openssh"),       # a .pub that is not one
    ],
)
def test_what_cannot_be_judged_never_refuses(tmp_path, keys, pub, why):
    """A guess about an endpoint must not be able to stop a run that would
    otherwise work: only a positive absence refuses."""
    identity = tmp_path / "vastai_ed25519"
    if pub is not None:
        sshkeys.public_half_path(identity).write_text(pub, encoding="utf-8")
    check = sshkeys.check_ssh_key(_Client(keys), identity)
    assert check.state == "unknown" and check.refuses is False
    assert check.detail


def test_the_client_reads_the_key_list_then_falls_back_to_the_user_object():
    seen: list[str] = []

    def http(method, url, headers, body, timeout):
        path = url.split("/api/v0", 1)[1]
        seen.append(f"{method} {path}")
        if path.startswith("/ssh"):
            return 404, {"msg": "no route"}
        return 200, {"ssh_key": MINE}

    client = VastClient(None, http=http, key_reader=lambda _p: "k" * 24)
    assert client.ssh_keys() == [MINE]
    assert seen == [f"GET {sshkeys.SSH_KEYS_PATH}", f"GET {sshkeys.SSH_KEYS_FALLBACK_PATH}"]


def test_neither_endpoint_is_an_unknown_not_an_empty_account():
    client = VastClient(None, http=lambda *a: (404, {"msg": "no route"}), key_reader=lambda _p: "k" * 24)
    assert client.ssh_keys() is None


# ---------------------------------------------------------------------------
# C4: the executor rents NOTHING when the account does not carry the key
# ---------------------------------------------------------------------------
def test_the_executor_refuses_before_it_creates_an_instance(tmp_path, isolated_state, ssh_tripwire):  # noqa: F811
    env = make_env(tmp_path, isolated_state, jobs=("JOB-vocr-1", "JOB-vocr-2"))
    sshkeys.public_half_path(env.cfg.ssh_identity_path).write_text(MINE + "\n", encoding="utf-8")
    env.world.vast.account_ssh_keys = [OTHER]
    summary = run_env(env)
    assert [e["reason_code"] for e in summary["refused"]] == ["key-missing", "vastai-disabled-for-run"]
    # the point of the round: NOTHING was rented for that answer
    assert env.world.vast.instances == {}
    assert [v for v in env.world.vast.calls if v[0] == "PUT"] == []
    said = summary["refused"][0]["message"]
    assert sshkeys.CONSOLE_PAGE in said and "Nothing was sent." in said


def test_a_registered_key_leaves_the_run_exactly_as_it_was(tmp_path, isolated_state, ssh_tripwire):  # noqa: F811
    env = make_env(tmp_path, isolated_state)
    sshkeys.public_half_path(env.cfg.ssh_identity_path).write_text(MINE + "\n", encoding="utf-8")
    env.world.vast.account_ssh_keys = [MINE, OTHER]
    summary = run_env(env)
    assert summary["published"] and not summary.get("refused")


def test_an_account_whose_key_list_cannot_be_read_still_rents(tmp_path, isolated_state, ssh_tripwire):  # noqa: F811
    """The default of every existing test: no ``.pub``, no endpoint. The
    pre-flight says so once and the run goes on."""
    env = make_env(tmp_path, isolated_state)
    summary = run_env(env)
    assert summary["published"] and not summary.get("refused")
    assert any("pre-flight" in line for line in env.world.log)


# ---------------------------------------------------------------------------
# C1: the datacentre field is ONE constant, and the refusal names it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "offer, verdict",
    [
        ({"datacenter": True}, True),
        ({"datacenter": 1}, True),
        ({"datacenter": False}, False),
        ({"datacenter": 0}, False),
        ({"is_datacenter": True}, True),
        ({"hosting_type": 1}, True),               # the live shape, recorded 2026-09-20 (round 6)
        ({"hosting_type": "datacenter"}, None),    # a string is NOT the recorded shape: refuse
        ({"gpu_name": "RTX 3090"}, None),          # the live shape of 2026-09-19: no field at all
        ({"datacenter": None}, None),              # present but unmapped
        ({"datacenter": 2}, None),
    ],
)
def test_the_datacentre_signal_maps_each_shape_it_supports(offer, verdict):
    got, read = datacenter_signal(offer)
    assert got is verdict
    assert read and (any(f in read for f in DATACENTER_FIELDS) or "none of" in read)


def test_the_refusal_names_the_field_it_looked_at(tmp_path, isolated_state):  # noqa: F811
    from trialerror.util.config import load_config
    from trialerror.vastai.config import load_vast_config

    root = tmp_path / "devroot"
    write_key(root / "keys")
    (root / "trialerror.toml").write_text(
        toml_text(dev_toml(egress={"require_datacenter": True, "allow_documents": ["ab" * 32]})), encoding="utf-8"
    )
    cfg = load_vast_config(load_config(root / "trialerror.toml").raw, config_root=root)
    assert cfg.egress.require_datacenter is True
    live_shape = make_offer(1)
    live_shape.pop("datacenter", None)
    reasons = offer_refusals(live_shape, cfg, min_cpu_ram_gb=1.0)
    named = [r for r in reasons if "datacenter" in r]
    assert named and "none of " + "/".join(DATACENTER_FIELDS) in named[0]
    # a host that says it IS one is admitted through the same one constant
    assert not [r for r in offer_refusals({**live_shape, "hosting_type": 1}, cfg, min_cpu_ram_gb=1.0)
                if "datacenter host" in r]


def test_the_debug_rows_print_key_names_and_the_allow_list_only():
    offers = [{**make_offer(1), "secret_field": "nothing of the account", "hosting_type": 2} for _ in range(5)]
    rows = offer_debug_rows(offers, 2)
    assert len(rows) == 2
    assert "secret_field" in rows[0]["keys"], "key NAMES are the point of the aid"
    assert set(rows[0]["values"]) <= set(OFFER_DEBUG_FIELDS)
    assert "secret_field" not in rows[0]["values"]
    assert offer_debug_rows(offers, 0) == []


# ---------------------------------------------------------------------------
# C1/C4 through the CLI: plan is free, says both answers, rents nothing
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _cli_quiet(monkeypatch):
    monkeypatch.setattr(cli_vastai, "_out", io.StringIO())


def _plan_root(tmp_path: Path, **kwargs) -> Path:
    root = tmp_path / "devroot"
    root.mkdir(exist_ok=True)
    write_key(root / "keys")
    kwargs.setdefault("egress", {"allow_documents": ["cd" * 32]})
    (root / "trialerror.toml").write_text(toml_text(dev_toml(**kwargs)), encoding="utf-8")
    return root


def _pdf(path: Path, pages: int = 4) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=A4_PT[0], height=A4_PT[1])
    with open(path, "wb") as f:
        writer.write(f)
    return path


def _run(monkeypatch, fake: FakeVast, *argv: str) -> dict:
    monkeypatch.setattr(cli_vastai, "_client_factory", lambda key_path: VastClient(key_path, http=fake.http))
    args = build_parser().parse_args(["vastai", *argv])
    return args.handler(args)


def test_plan_says_the_identity_is_not_on_the_account_and_would_refuse(tmp_path, isolated_state, monkeypatch, network_tripwire):  # noqa: F811
    root = _plan_root(tmp_path)
    (root / "keys" / "vastai_ed25519.pub").write_text(MINE + "\n", encoding="utf-8")
    fake = FakeVast()
    fake.account_ssh_keys = [OTHER]
    env = _run(monkeypatch, fake, "plan", "--backend-config-root", str(root), "--input", str(_pdf(tmp_path / "d.pdf")))
    result = env["result"]
    assert result["ssh_key"]["state"] == "not-registered"
    assert result["caps"]["ssh_key"]["pass"] is False
    assert result["verdict"] == "would-refuse" and result["reason_code"] == "key-missing"
    assert result["rented"] is False and fake.instances == {}
    assert any(sshkeys.CONSOLE_PAGE in n for n in result["notes"])


def test_plan_with_the_key_registered_does_not_stop_on_it(tmp_path, isolated_state, monkeypatch, network_tripwire):  # noqa: F811
    root = _plan_root(tmp_path)
    (root / "keys" / "vastai_ed25519.pub").write_text(MINE + "\n", encoding="utf-8")
    fake = FakeVast()
    fake.account_ssh_keys = [MINE]
    result = _run(monkeypatch, fake, "plan", "--backend-config-root", str(root),
                  "--input", str(_pdf(tmp_path / "d.pdf")))["result"]
    assert result["ssh_key"]["state"] == "registered"
    assert result["caps"]["ssh_key"]["pass"] is True and result["reason_code"] != "key-missing"


def test_plan_offer_keys_prints_the_raw_shape_for_the_custodians_one_free_read(tmp_path, isolated_state, monkeypatch, network_tripwire):  # noqa: F811
    root = _plan_root(tmp_path)
    fake = FakeVast()
    result = _run(monkeypatch, fake, "plan", "--backend-config-root", str(root),
                  "--input", str(_pdf(tmp_path / "d.pdf")), "--offer-keys")["result"]
    rows = result["offer_keys"]
    assert 1 <= len(rows) <= 3
    assert rows[0]["keys"] == sorted(rows[0]["keys"]) and "gpu_name" in rows[0]["keys"]
    assert set(rows[0]["values"]) <= set(OFFER_DEBUG_FIELDS)
    assert len(fake.search_bodies()) == 1, "the aid is part of the ONE free search, not a second one"
    # and it is opt-in: without the flag the plan carries no raw offer shape
    plain = _run(monkeypatch, fake, "plan", "--backend-config-root", str(root),
                 "--input", str(_pdf(tmp_path / "d.pdf")))["result"]
    assert "offer_keys" not in plain


def test_offer_keys_takes_a_count(tmp_path, isolated_state, monkeypatch, network_tripwire):  # noqa: F811
    root = _plan_root(tmp_path)
    result = _run(monkeypatch, FakeVast(ignore_filters=True), "plan", "--backend-config-root", str(root),
                  "--input", str(_pdf(tmp_path / "d.pdf")), "--offer-keys", "1")["result"]
    assert len(result["offer_keys"]) == 1


# ---------------------------------------------------------------------------
# C4's window B: the worker's log, while it runs
# ---------------------------------------------------------------------------
def test_the_worker_log_file_holds_each_line_before_the_run_ends(tmp_path):
    path = tmp_path / "logs" / "worker.log"
    path.parent.mkdir()
    lines: list[str] = []
    log = cli_offload.worker_log_sink(lines, path)
    log("= vast.ai lease VOCR-abc: instance 1 created, deadline ...")
    mid_run = path.read_text(encoding="utf-8")
    log("! vast.ai stops for the rest of this worker run")
    assert "instance 1 created" in mid_run, "a five-minute lease must be watchable while it bills"
    assert mid_run.count("\n") == 1
    end = path.read_text(encoding="utf-8").splitlines()
    assert len(end) == 2 and end[0].endswith("instance 1 created, deadline ...")
    assert end[0].startswith("20") and end[0][10] == "T" and end[0][19] == "Z"
    assert lines == ["= vast.ai lease VOCR-abc: instance 1 created, deadline ...",
                     "! vast.ai stops for the rest of this worker run"]


def test_without_the_flag_nothing_is_written_and_the_envelope_still_has_the_log(tmp_path):
    lines: list[str] = []
    cli_offload.worker_log_sink(lines, None)("a line")
    assert lines == ["a line"] and list(tmp_path.iterdir()) == []


def test_the_worker_verb_offers_the_flag_and_names_what_it_is_for():
    parser = build_parser()
    help_text = parser._subparsers._group_actions[0].choices["offload"]._subparsers._group_actions[0].choices[
        "worker"
    ].format_help()
    assert "--log-file" in help_text and "as it happens" in help_text
    args = parser.parse_args(["offload", "worker", "--queue-root", "q", "--log-file", "w.log"])
    assert args.log_file == "w.log"


def test_a_log_file_that_cannot_be_opened_is_a_named_refusal_not_a_traceback(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    args = build_parser().parse_args(
        ["offload", "worker", "--queue-root", str(tmp_path / "q"), "--backend-config-root", str(tmp_path),
         "--log-file", str(blocker / "sub" / "w.log")]
    )
    env = args.handler(args)
    assert env["ok"] is False and env["error"]["code"] == "bad_arguments"
    assert "--log-file" in env["error"]["message"]


def test_the_sink_never_fails_a_run_it_cannot_write(tmp_path):
    lines: list[str] = []
    log = cli_offload.worker_log_sink(lines, tmp_path / "gone" / "deeper" / "w.log")
    log("a line")  # the directory does not exist: the run goes on
    assert lines == ["a line"]


def test_the_round_5_changes_carry_no_key_material():
    """Rule 2: nothing in the new code opens a private half or prints a key."""
    text = Path("trialerror/vastai/sshkeys.py").read_text(encoding="utf-8")
    assert ".pub" in text and "public_half_path" in text
    body = json.dumps(sshkeys.check_ssh_key(_Client([OTHER]), "no-such-identity").as_dict())
    assert "ssh-ed25519" not in body and "AAAA" not in body
