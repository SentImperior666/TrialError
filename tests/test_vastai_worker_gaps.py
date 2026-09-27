"""The vast.ai OCR backend through ``run_worker``: the spend guardrails
(charter rule 4), every exit path destroying (charter rule 3), and the RAM
scratch rule (O8) with ``remote_scratch = "shm_or_disk"``.

Driven with L2's fakes (``tests/_vastai_shell_fakes.py``) and L1's
``FakeVast`` knobs. No network, no ssh, no GPU, no real ``marker_single``.
"""

from __future__ import annotations

import copy
import shlex
from typing import Any

import pytest

from tests._vastai_fakes import FAKE_KEY, default_offers, isolated_state, network_tripwire  # noqa: F401 - fixtures
from tests._vastai_shell_fakes import JOB, RecordingTransport, make_env, run_env, ssh_tripwire  # noqa: F401
from tests.test_vastai_worker import _cache, _document_uploads, _pending, _published, _rows
from trialerror.vastai import shell as sh

JOB_A = "JOB-vocr-a"
JOB_B = "JOB-vocr-b"
SMALL_SHM = 64_000_000  # Docker's default /dev/shm: too small for any document here


@pytest.fixture(autouse=True)
def _guarded(network_tripwire, isolated_state, ssh_tripwire):  # noqa: F811
    yield


@pytest.fixture
def state(isolated_state):  # noqa: F811
    return isolated_state


def _refused(summary: dict) -> list[tuple[str, str]]:
    return [(e["job_id"], e["reason_code"]) for e in summary.get("refused", [])]


def _assert_returned_unrun(env: Any, transport: RecordingTransport, job: str = JOB) -> None:
    assert ("return", job) in transport.verbs and job in _pending(env), "the claim went back, no attempt burned"


def _with_egress(env: Any, **egress: Any) -> Any:
    """Rebuild ``env``'s backend with ``[vastai.egress]`` keys added, and
    seal a fresh approval over the changed policy (as ``make_env`` does)."""
    from trialerror.vastai.config import load_vast_config
    from trialerror.vastai.egress import sign_egress_approval, write_egress_approval
    from trialerror.vastai.ocr import VastaiMarkerOcrBackend

    toml = copy.deepcopy(env.toml)
    toml["vastai"]["egress"].update(egress)
    cfg = load_vast_config(toml, config_root=env.root)
    write_egress_approval(cfg, sign_egress_approval(cfg, now=env.world.clock.now(), key_reader=lambda _p: FAKE_KEY))
    env.toml, env.cfg = toml, cfg
    env.backend = VastaiMarkerOcrBackend.from_toml(toml, config_root=env.root, **env.overrides)
    return env


def _hang_bootstrap(env: Any, step: str) -> list[int]:
    """Every host's ``sh bootstrap.sh <step>`` never returns: like the real
    ``SshShell``, the call raises ``RemoteTimeout`` once its timeout has
    passed (the fake clock moves by that timeout). Returns the instance ids
    that hung."""
    hung: list[int] = []
    inner = env.backend.shell_factory

    def factory(**kwargs: Any):
        shell = inner(**kwargs)
        run = shell.run

        def hanging_run(cmd: str, *, stdin_bytes: bytes | None = None, timeout_s: float):
            t = shlex.split(cmd)
            if len(t) >= 3 and t[0] == "sh" and t[1].endswith("/bootstrap.sh") and t[2] == step:
                env.world.commands.append((shell.iid, cmd))
                hung.append(shell.iid)
                env.world.clock.advance(timeout_s)
                raise sh.RemoteTimeout(f"timed out after {timeout_s:.0f} s")
            return run(cmd, stdin_bytes=stdin_bytes, timeout_s=timeout_s)

        shell.run = hanging_run
        return shell

    env.backend.shell_factory = factory
    return hung


def _assert_every_lease_destroyed(env: Any, leases: int) -> list[dict]:
    outcomes = _rows(env, "outcome")
    assert len(outcomes) == leases and all(r["destroyed"] is True for r in outcomes), outcomes
    assert env.world.vast.instances == {}, "destroyed, and the listing confirms it"
    return outcomes


# ---------------------------------------------------------------------------
# P2: spend guardrails
# ---------------------------------------------------------------------------
def test_the_run_cap_publishes_the_first_job_and_refuses_the_second_before_any_create(tmp_path, state):
    # Calibrate on a separate root and ledger: one job's worst case and its settled estimate.
    (tmp_path / "cal").mkdir()
    cal = make_env(tmp_path / "cal", tmp_path / "cal-state", jobs=("JOB-vocr-cal",))
    assert run_env(cal)["published"] == ["JOB-vocr-cal"]
    worst = float(_rows(cal, "intent")[0]["worst_usd"])
    settled = float(_rows(cal, "outcome")[0]["estimated_cost_usd"])
    assert settled > 0 and worst > 0
    # The first worst case fits; the settled first job plus a second worst case does not.
    cap = worst + settled / 2

    (tmp_path / "run").mkdir()
    env = make_env(tmp_path / "run", state, jobs=(JOB_A, JOB_B), vastai={"max_run_usd": cap})
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)

    assert summary["published"] == [JOB_A] and summary["failed"] == []
    assert _refused(summary) == [(JOB_B, "cap-run")]
    assert len(env.world.vast.verbs("PUT")) == 1, "one create, for the first job; zero for the second"
    assert [r["job_id"] for r in _rows(env, "intent")] == [JOB_A]
    _assert_returned_unrun(env, transport, JOB_B)
    assert ("pull", JOB_B) in transport.verbs, "the worst-case cross is found at pricing, after the pull"
    assert _pending(env) == [JOB_B]
    refused = _rows(env, "refused")
    assert [(r["job_id"], r["reason_code"]) for r in refused] == [(JOB_B, "cap-run")]
    assert (refused[0]["sha256"], refused[0]["bytes"]) == (env.sha, len(env.data))
    assert refused[0]["details"]["worst_usd"] == pytest.approx(worst)
    assert "cap-run" in summary["message"]


@pytest.mark.parametrize(
    "code, knob",
    [("api-error", {"search_status": 503}), ("credit-low", {"credit": 0.01})],
    ids=["offer-search-fails", "credit-below-worst-case"],
)
def test_a_pricing_refusal_rents_nothing_and_returns_the_claim(tmp_path, state, code, knob):
    env = make_env(tmp_path, state)
    for name, value in knob.items():
        setattr(env.world.vast, name, value)
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)

    assert _refused(summary) == [(JOB, code)] and summary["published"] == []
    assert env.world.vast.verbs("PUT") == [], "zero create calls"
    assert _rows(env, "intent") == [] and env.world.commands == [] and _document_uploads(env) == []
    _assert_returned_unrun(env, transport)
    row = _rows(env, "refused")[0]
    assert (row["reason_code"], row["sha256"], row["bytes"]) == (code, env.sha, len(env.data))
    assert "Nothing was rented" in row["message"]


# ---------------------------------------------------------------------------
# P3: every exit path destroys
# ---------------------------------------------------------------------------
def test_an_instance_that_never_becomes_ready_is_destroyed_failed_over_then_hosts_exhausted(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.vast.boot_polls = 10**9  # every instance shows "loading" for ever
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)

    assert _refused(summary) == [(JOB, "hosts-exhausted")]
    intents = _rows(env, "intent")
    assert len(intents) == 2 and intents[0]["machine_id"] != intents[1]["machine_id"]
    assert intents[1]["excluded_machine_ids"] == [intents[0]["machine_id"]]
    outcomes = _assert_every_lease_destroyed(env, 2)
    assert all(r["result"] == "failed" and "not running after" in r["error"] for r in outcomes)
    assert _rows(env, "shipped") == [] and _document_uploads(env) == [] and env.world.shells == {}
    _assert_returned_unrun(env, transport)
    assert _rows(env, "refused")[0]["reason_code"] == "hosts-exhausted"


def test_a_bootstrap_that_hangs_past_its_timeout_is_class_f_on_every_host(tmp_path, state):
    env = make_env(tmp_path, state)
    hung = _hang_bootstrap(env, "install")
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)

    assert _refused(summary) == [(JOB, "hosts-exhausted")], "F on each host, then R"
    assert len(hung) == 2 and len(set(hung)) == 2
    intents = _rows(env, "intent")
    assert intents[1]["excluded_machine_ids"] == [intents[0]["machine_id"]]
    outcomes = _assert_every_lease_destroyed(env, 2)
    assert all(r["ended_by"] == "host-failure" and "the bootstrap did not return within" in r["error"]
               for r in outcomes)
    assert _rows(env, "shipped") == [] and _document_uploads(env) == []
    _assert_returned_unrun(env, transport)


def test_no_offer_left_after_the_exclusion_returns_the_claim_as_no_offer(tmp_path, state):
    env = make_env(tmp_path, state, offers=default_offers()[:1])
    env.world.die_on = ["3-5"]
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)

    assert _refused(summary) == [(JOB, "no-offer")]
    assert len(_rows(env, "intent")) == 1 and len(env.world.vast.verbs("PUT")) == 1
    outcomes = _assert_every_lease_destroyed(env, 1)
    assert outcomes[0]["result"] == "failed" and outcomes[0]["ranges_run"] == ["0-2"]
    assert (_cache(env) / "range-000000-000002.md").is_file(), "the finished range is kept"
    _assert_returned_unrun(env, transport)
    row = _rows(env, "refused")[0]
    assert row["reason_code"] == "no-offer" and row["job_id"] == JOB


# ---------------------------------------------------------------------------
# P4: remote_scratch = "shm_or_disk" with a /dev/shm too small
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tier, required", [("open", None), (None, [])], ids=["tier-not-listed", "no-tier-no-list"])
def test_shm_or_disk_puts_a_document_that_may_use_disk_on_the_disk_and_wipes_it(tmp_path, state, tier, required):
    env = make_env(tmp_path, state, tier=tier)
    _with_egress(env, remote_scratch="shm_or_disk", **({} if required is None else {"shm_required_tiers": required}))
    env.world.shm_avail = SMALL_SHM
    summary = run_env(env)

    assert summary["published"] == [JOB] and "refused" not in summary
    shipped, outcome = _rows(env, "shipped")[0], _rows(env, "outcome")[0]
    assert shipped["scratch_kind"] == outcome["scratch_kind"] == "disk"
    disk = f"{sh.REMOTE_ROOT_DISK}/{shipped['lease_id']}"
    uploads = [(iid, path) for iid, path, data in env.world.stdin_sent if data == env.data]
    assert len(uploads) == 1 and uploads[0][1].startswith(disk + "/input")
    result, pages = _published(env)
    assert result["vastai"]["scratch_kind"] == "disk" and len(result["vastai"]["leases"]) == 1
    assert [p["page_number"] for p in pages] == list(range(10))
    shell = env.world.shells[uploads[0][0]]
    assert [p for p in shell.fs if p.startswith(sh.REMOTE_ROOT_DISK)] == [], "the disk scratch is wiped"
    assert any(cmd.startswith("rm -rf -- ") and disk in cmd for _iid, cmd in env.world.commands)
    assert env.world.vast.instances == {}


@pytest.mark.parametrize("tier", ["commercial_restricted", None], ids=["listed-tier", "no-tier"])
def test_shm_or_disk_still_refuses_a_document_that_needs_ram_scratch(tmp_path, state, tier):
    env = make_env(tmp_path, state, tier=tier)
    _with_egress(env, remote_scratch="shm_or_disk")  # shm_required_tiers keeps its default
    assert env.cfg.egress.shm_required_tiers
    env.world.shm_avail = SMALL_SHM
    transport = RecordingTransport(env.queue)
    summary = run_env(env, transport=transport)

    assert _refused(summary) == [(JOB, "shm-too-small")]
    assert _document_uploads(env) == [] and _rows(env, "shipped") == []
    _assert_every_lease_destroyed(env, 2)
    _assert_returned_unrun(env, transport)
