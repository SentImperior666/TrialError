"""Round-3 regression tests: the four BLOCKING findings of the guard
verification (F01-F04), adopted from the verifier's claim tests C01, C09, C30
and C22 with only their imports and fixtures adapted. Each failed on the
branch before its fix. No network, no ssh, no GPU (the tripwires are autouse),
and the only "key" is a FAKE one written into ``tmp_path``."""

from __future__ import annotations

import json

import pytest

from tests import _vastai_shell_fakes as shf
from tests._vastai_fakes import isolated_state, network_tripwire  # noqa: F401 - fixtures
from tests._vastai_shell_fakes import JOB, ledger_rows, make_env, run_env, ssh_tripwire  # noqa: F401 - fixtures
from trialerror.offload import protocol

JOB2 = "JOB-vocr-2"


@pytest.fixture(autouse=True)
def _guards(network_tripwire, isolated_state, ssh_tripwire):  # noqa: F811
    yield


@pytest.fixture
def state(isolated_state):  # noqa: F811
    return isolated_state


def rows(env, kind):
    return [r for r in ledger_rows(env.state_dir) if r["kind"] == kind]


# ---------------------------------------------------------------- F01 (C01)
SEP = "-" * 48


def block(n: int) -> str:
    return f"{{{n}}}{SEP}\n\nbody of page {n}\n\n"


def pages_published(env) -> bool:
    return (protocol.done_dir(env.queue) / JOB / "pages.json").is_file()


def patch_range_md(monkeypatch, flag, fn):
    orig = shf.FakeInstanceShell._markdown

    def md(self, f, first, last):
        text = orig(self, f, first, last)
        return fn(text, first, last) if f == flag else text

    monkeypatch.setattr(shf.FakeInstanceShell, "_markdown", md)


@pytest.mark.parametrize("case", ["empty", "unpaginated", "truncated", "extra"])
def test_f01_a_range_that_comes_back_without_its_pages_is_never_published(tmp_path, state, monkeypatch, case):
    """Pages 3-5 come back exit 0 with an empty markdown, an unpaginated blob,
    only pages 3 and 4, or an extra page. The document must not be published
    as complete, and the bad text never reaches the range cache."""
    fn = {
        "empty": lambda t, a, b: "",
        "unpaginated": lambda t, a, b: "garbled text of a whole range without any page marker\n",
        "truncated": lambda t, a, b: block(a) + block(a + 1),
        "extra": lambda t, a, b: t + block(b + 1),
    }[case]
    patch_range_md(monkeypatch, "3-5", fn)
    env = make_env(tmp_path, state)
    summary = run_env(env)
    published = []
    if pages_published(env):
        pages = json.loads((protocol.done_dir(env.queue) / JOB / "pages.json").read_text(encoding="utf-8"))["pages"]
        published = [p["page_number"] for p in pages]
    assert JOB not in summary["published"] and not pages_published(env), (
        f"{case}: range 3-5 came back {case} and the job was published with pages {published}"
    )
    # The bad range is a host failure (class F): the lease is failed over, never settled as published.
    outcomes = rows(env, "outcome")
    assert outcomes and all(o["result"] != "published" for o in outcomes)
    assert any(o.get("ended_by") == "host-failure" for o in outcomes)


# ---------------------------------------------------------------- F02 (C09)
def test_f02_a_lease_whose_destroy_is_unconfirmed_keeps_counting_at_its_worst_case(tmp_path, state):
    env = make_env(tmp_path, state, vastai={"max_run_usd": 0.40}, jobs=(JOB, JOB2))  # one worst case fits, two do not
    env.world.vast.sticky = {5001}  # the first instance stays listed (billing) after every DELETE
    run_env(env)
    intents, outcomes = rows(env, "intent"), rows(env, "outcome")
    assert 5001 in env.world.vast.instances and outcomes[0]["destroyed"] is False
    assert len(intents) == 1, (
        f"lease 1 (worst ${intents[0]['worst_usd']}) could not be destroyed but was settled at "
        f"${outcomes[0]['estimated_cost_usd']}; a second lease (worst ${intents[-1]['worst_usd']}) was rented "
        "under max_run_usd = $0.40"
    )


def test_f02_the_spend_view_closes_an_unconfirmed_destroy_only_on_reaped_or_a_confirmed_destroy():
    from trialerror.vastai.ledger import lease_spend

    intent = {"kind": "intent", "lease_id": "VOCR-a", "worst_usd": 0.25, "worker_run_id": "R"}
    unconfirmed = {"kind": "outcome", "lease_id": "VOCR-a", "instance_id": 5001, "estimated_cost_usd": 0.09,
                   "destroyed": False, "result": "failed"}
    spend = lease_spend([intent, unconfirmed])["VOCR-a"]
    assert spend.usd == 0.25 and spend.settled is False
    # an estimate above the worst case (it cannot happen, but the max is the rule) stays the max
    assert lease_spend([intent, {**unconfirmed, "estimated_cost_usd": 0.3}])["VOCR-a"].usd == 0.3
    reaped = {"kind": "reaped", "instance_id": 5001, "reason": "run_finished", "run_id": "VOCR-a"}
    closed = lease_spend([intent, unconfirmed, reaped])["VOCR-a"]
    assert closed.usd == 0.09 and closed.settled is True
    by_instance = lease_spend([intent, unconfirmed, {**reaped, "run_id": None}])["VOCR-a"]
    assert by_instance.settled is True
    confirmed = lease_spend([intent, unconfirmed, {**unconfirmed, "destroyed": True}])["VOCR-a"]
    assert confirmed.usd == 0.09 and confirmed.settled is True
    # an older outcome row without the field (not_created) settles as before
    legacy = {"kind": "outcome", "lease_id": "VOCR-a", "instance_id": None, "estimated_cost_usd": 0.0,
              "result": "not_created"}
    assert lease_spend([intent, legacy])["VOCR-a"].settled is True


# ---------------------------------------------------------------- F03 (C30)
def test_f03_a_failover_never_takes_one_job_past_max_job_usd(tmp_path, state):
    env = make_env(tmp_path, state, vastai={"max_job_usd": 0.30})  # the whole job's worst case is $0.26
    env.world.die_on = ["6-8"]

    def slow(first, last):
        if first == 3 and len(env.world.vast.instances) == 1 and 5001 in env.world.vast.instances:
            env.world.clock.advance(2000)  # lease 1 runs long (inside its TTL) before its host dies

    env.world.on_range = slow
    run_env(env)
    intents, outcomes = rows(env, "intent"), rows(env, "outcome")
    spent = sum(o["estimated_cost_usd"] for o in outcomes if o["lease_id"] == intents[0]["lease_id"])
    if len(intents) > 1:
        booked = spent + intents[1]["worst_usd"]
        assert booked <= 0.30, (
            f"job booked ${booked:.4f} (lease 1 cost ${spent:.4f} + lease 2 worst ${intents[1]['worst_usd']}) "
            "over max_job_usd = $0.30"
        )
    # the failover was refused by name, and the claim went back unrun
    refused = rows(env, "refused")
    assert [r["reason_code"] for r in refused] == ["cap-job"]
    assert "this job has spent" in refused[0]["message"]
    assert JOB in shf.RecordingTransport(env.queue).list_jobs()


# ---------------------------------------------------------------- F04 (C22)
KEY = "vk_FAKE_" + "0123456789abcdef" * 3


def test_f04_a_key_echoed_by_the_api_never_reaches_any_output(tmp_path, state, capsys, caplog):
    from trialerror.vastai.egress import sign_egress_approval, write_egress_approval

    env = make_env(tmp_path, state)
    (env.root / "keys" / "vastai.key").write_text(KEY + "\n", encoding="utf-8")
    env.world.vast.key = KEY
    write_egress_approval(env.cfg, sign_egress_approval(env.cfg, now=env.world.clock.now(), key_reader=lambda _p: KEY))
    inner = env.world.vast.http

    def echo(method, url, headers, body, timeout):
        if method == "POST":
            return 401, {"error": "unauthorized", "msg": f"rejected {headers['Authorization']} for {url}"}
        return inner(method, url, headers, body, timeout)

    env.backend.client._http = echo
    summary = run_env(env)
    out = capsys.readouterr()
    texts = {"summary": json.dumps(summary, default=str), "log": "\n".join(env.world.log), "stdout": out.out,
             "stderr": out.err, "caplog": caplog.text}
    for base in (env.state_dir, env.queue, env.tmp / "work"):
        for p in base.rglob("*") if base.exists() else []:
            if p.is_file():
                texts[str(p.relative_to(env.tmp))] = p.read_text(encoding="utf-8", errors="replace")
    leaks = sorted(name for name, text in texts.items() if KEY in text)
    assert leaks == [], f"the fake key reached: {leaks}"
    assert rows(env, "refused"), "the refusal row is still written"


def test_f04_the_client_redacts_its_key_bearer_and_api_key_from_every_error(tmp_path):
    from trialerror.vastai.api import VastClient
    from trialerror.vastai.errors import VastApiError

    key_file = tmp_path / "vastai.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")

    def echo(method, url, headers, body, timeout):
        return 500, {"msg": f"{KEY} / {headers['Authorization']} / api_key=abc123secret&x=1 / " + "x" * 400}

    with pytest.raises(VastApiError) as info:
        VastClient(key_file, http=echo).search_offers({"q": 1})
    text = str(info.value)
    assert KEY not in text and KEY[:20] not in text and "abc123secret" not in text
    assert "Bearer [redacted]" in text and "api_key=[redacted]" in text

    def raising(method, url, headers, body, timeout):
        raise VastApiError(f"vast.ai {method} {url}?api_key={KEY} failed: OSError")

    with pytest.raises(VastApiError) as info:
        VastClient(key_file, http=raising).list_instances()
    assert KEY not in str(info.value)

    def odd_create(method, url, headers, body, timeout):
        return 200, {"success": False, "echo": KEY}

    with pytest.raises(VastApiError) as info:
        VastClient(key_file, http=odd_create).create_instance(1, image="i", disk_gb=8, label="l", onstart="")
    assert KEY not in str(info.value)


def test_f04_the_ledger_redacts_rather_than_refuses_so_the_row_is_still_written(tmp_path):
    from trialerror.vastai.errors import VastPlanRefused
    from trialerror.vastai.ledger import Ledger

    led = Ledger(tmp_path / "state", secrets=lambda: [KEY])
    row = led.append("refused", sha256="0" * 64, bytes=1, reason_code="api-error",
                     message=f"rejected Bearer {KEY}; raw {KEY}", details={"echo": [f"api_key={KEY}"]})
    written = led.path.read_text(encoding="utf-8")
    assert KEY not in written and KEY not in json.dumps(row)
    assert led.read().rows[0]["reason_code"] == "api-error"
    # the refusal texts, too, never carry a bearer token or an api_key= value
    refusal = VastPlanRefused("api-error", f"the vast.ai offer search failed: Bearer {KEY} api_key={KEY}")
    assert KEY not in str(refusal)
