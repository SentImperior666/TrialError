"""The sidecar loop, end to end against a real local server.

A manifest goes into ``pending/``; a result and its bytes come out of
``done/`` or ``failed/``; an audit line lands in two places. These tests
drive that whole path with the real fetcher, the real policy loader and a
real ``http.server``, and they hold the loop to the promise its docstring
makes: **every attempt ends in a record**. There is no input here — a bot
wall, an unapproved host, a manifest that does not even parse — that leaves
nothing behind for an operator to find.
"""

from __future__ import annotations

import json
import shutil
import socket

import pytest

from tests._webfetch_fixtures import LocalSite, Route, load_policy, loopback_netguard, write_policy
from tests.test_webfetch_gitfetch import build_repo
from trialerror.cli import main
from trialerror.webfetch.gitfetch import GitFetcher
from trialerror.webfetch.netguard import NetGuard
from trialerror.webfetch.policy import PolicyError
from trialerror.webfetch.protocol import (
    BODY_FILENAME,
    REPO_FILENAME,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    Manifest,
    Queue,
)
from trialerror.webfetch.sidecar import Sidecar, SidecarPaths

PAGE = b"<html><head><title>A page</title></head><body>the text</body></html>"
ALLOW_ROBOTS = Route(body=b"User-agent: *\nDisallow:\n", content_type="text/plain")
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")


def paths_for(tmp_path, policy_dir) -> SidecarPaths:
    return SidecarPaths(
        queue=tmp_path / "queue",
        policy=policy_dir,
        audit=tmp_path / "audit",
        state=tmp_path / "state",
        work=tmp_path / "work",
    )


def manifest(url: str, *, job: str = "JOB-webfetch-WF-01", **overrides) -> Manifest:
    fields = {
        "job_id": job,
        "fetch_id": job.rsplit("-", 1)[-1],
        "launch_id": "LNCH-01M1R3J6TZ2HFQM95AEFEW1GY1",
        "url": url,
        "kind": "page",
        "origin": "operator_list",
    }
    fields.update(overrides)
    return Manifest.build(**fields)


def run_one(sidecar: Sidecar, *jobs: Manifest):
    queue = sidecar.queue.ensure_layout()
    for job in jobs:
        queue.submit(job)
    return sidecar.run(max_jobs=len(jobs) or 1, max_idle_polls=1)


def sidecar_for(tmp_path, site: LocalSite, *, policy_kwargs=None, **kwargs) -> Sidecar:
    policy_dir = write_policy(tmp_path, **(policy_kwargs or {}))
    return Sidecar(
        paths_for(tmp_path, policy_dir),
        worker_id="worker-1",
        netguard=loopback_netguard(site.port),
        poll_interval_s=0,
        **kwargs,
    )


def _no_sockets(address, port, timeout):  # pragma: no cover - asserted not to run
    raise AssertionError(f"a socket was opened to {address}:{port}")


def audit_lines(paths: SidecarPaths) -> list[dict]:
    lines: list[dict] = []
    for path in sorted(paths.audit.glob("webfetch-*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            lines.append(json.loads(raw))
    return lines


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def test_the_five_mounts_come_from_the_environment() -> None:
    env = {
        "TE_WEBFETCH_QUEUE": "/queue",
        "TE_WEBFETCH_POLICY": "/policy",
        "TE_WEBFETCH_AUDIT": "/audit",
        "TE_WEBFETCH_STATE": "/state",
        "TE_WEBFETCH_WORK": "/work",
    }
    paths = SidecarPaths.from_env(env)
    assert str(paths.queue).replace("\\", "/").endswith("/queue")
    assert str(paths.work).replace("\\", "/").endswith("/work")


def test_explicit_paths_win_over_the_environment() -> None:
    paths = SidecarPaths.from_env({"TE_WEBFETCH_QUEUE": "/queue"}, queue="./local-queue")
    assert str(paths.queue).replace("\\", "/") == "local-queue"


# --------------------------------------------------------------------------
# an empty queue
# --------------------------------------------------------------------------


def test_an_idle_loop_still_says_it_is_alive(tmp_path) -> None:
    """The heartbeat is what doctor and ``te-status.sh`` read; a sidecar with
    nothing to do must still write it."""
    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site)
        stats = sidecar.run(max_idle_polls=1)
    assert stats.claimed == 0 and stats.polls == 1
    record = json.loads(sidecar.queue.heartbeat_path.read_text(encoding="utf-8"))
    assert record["worker_id"] == "worker-1"
    assert record["version"] == "webfetch-sidecar/1"
    assert sidecar.queue.sidecar_heartbeat_age_s() is not None


def test_a_stop_callback_ends_the_loop(tmp_path) -> None:
    with LocalSite() as site:
        stats = sidecar_for(tmp_path, site).run(stop=lambda: True)
    assert stats.polls == 0


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_a_page_job_lands_in_done_with_its_bytes(tmp_path) -> None:
    routes = {"/robots.txt": ALLOW_ROBOTS, "/article": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        stats = run_one(sidecar, manifest("http://test.example/article"))

    assert (stats.claimed, stats.fetched, stats.refused) == (1, 1, 0)
    queue = sidecar.queue
    assert queue.state_of("JOB-webfetch-WF-01") == STATE_DONE
    state, result = queue.read_result("JOB-webfetch-WF-01")
    assert state == STATE_DONE
    assert result["outcome"] == "fetched"
    assert result["content_class"] == "html"
    assert result["robots"] == {"fetched": True, "verdict": "allow", "crawl_delay_s": 0.0}
    assert result["policy"]["verdict"] == "allow"
    assert result["policy"]["host_rule"].startswith("allowed-hosts.conf:")
    assert queue.payload_path("JOB-webfetch-WF-01", BODY_FILENAME).read_bytes() == PAGE
    assert queue.verify_payload("JOB-webfetch-WF-01", result) is not None


def test_robots_is_consulted_before_the_page(tmp_path) -> None:
    routes = {"/robots.txt": ALLOW_ROBOTS, "/article": Route(body=PAGE)}
    with LocalSite(routes) as site:
        run_one(sidecar_for(tmp_path, site), manifest("http://test.example/article"))
        assert site.paths == ["/robots.txt", "/article"]


def test_every_attempt_writes_an_audit_line_in_both_places(tmp_path) -> None:
    """The copy under ``/audit`` is authoritative and invisible to the
    research container; the copy in the queue exists so the in-container
    doctor check has something to read."""
    routes = {"/robots.txt": ALLOW_ROBOTS, "/article": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("http://test.example/article"))

    lines = audit_lines(sidecar.paths)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["job_id"] == "JOB-webfetch-WF-01"
    assert entry["launch_id"] == "LNCH-01M1R3J6TZ2HFQM95AEFEW1GY1"
    assert entry["worker_id"] == "worker-1"
    assert entry["outcome"] == "fetched"
    assert entry["host"] == "test.example"

    queue_copy = json.loads(sidecar.queue.audit_copy_path.read_text(encoding="utf-8").strip())
    assert queue_copy["job_id"] == entry["job_id"]


def test_no_audit_line_ever_carries_page_text(tmp_path) -> None:
    """C-0007: ids and counts leave the disk-to-disk path, never content."""
    routes = {"/robots.txt": ALLOW_ROBOTS, "/article": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("http://test.example/article"))
    blob = json.dumps(audit_lines(sidecar.paths))
    assert "the text" not in blob


def test_the_daily_counters_are_updated(tmp_path) -> None:
    routes = {"/robots.txt": ALLOW_ROBOTS, "/article": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("http://test.example/article"))
    counters = json.loads((sidecar.paths.state / "counters.json").read_text(encoding="utf-8"))
    assert counters["hosts"]["test.example"] == 1
    assert counters["bytes"] >= len(PAGE)


def test_a_304_settles_as_unchanged_with_no_payload(tmp_path) -> None:
    """A conditional the sidecar itself recorded is replayed and honoured.

    lane a fix pass (CONT-4): the manifest's ``etag`` is no longer taken on
    trust, so this fetches once to let the sidecar learn the validator, then
    re-fetches with the same manifest value.
    """
    routes = {"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE, etag='"v1"')}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("http://test.example/a"))
        stats = run_one(
            sidecar,
            manifest("http://test.example/a", job="JOB-webfetch-WF-02", etag='"v1"'),
        )

    assert stats.unchanged == 1
    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-02")
    assert result["outcome"] == "unchanged"
    assert result["bytes"] == 0 and result["content_sha256"] is None
    assert not sidecar.queue.payload_path("JOB-webfetch-WF-02", BODY_FILENAME).exists()


def test_a_conditional_the_sidecar_never_saw_is_not_put_on_the_wire(tmp_path) -> None:
    """Design §4 T2: "conditional headers only from the stored etag/
    last-modified of the same host".

    ``conditional`` is a manifest field and the manifest is written by the
    untrusted half of the boundary, so an attacker-chosen 256-byte
    ``If-None-Match`` used to reach the wire verbatim. Now a value this
    sidecar has never seen from this URL is dropped and the request goes out
    unconditional: the 304 that the server would have returned for its own
    etag does not happen, and no smuggled bytes leave the container.
    """
    routes = {"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE, etag='"v1"')}
    smuggled = "SECRET-" + "A" * 240
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        stats = run_one(sidecar, manifest("http://test.example/a", etag=smuggled))

    assert stats.fetched == 1, "no conditional was sent, so the body came back in full"
    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["outcome"] == "fetched"
    assert result["http_status"] == 200
    assert smuggled not in json.dumps(result)
    assert smuggled not in json.dumps(audit_lines(sidecar.paths))

    # And what the sidecar DID learn is the server's own validator, keyed on
    # the normalized URL — the only thing a later conditional may replay.
    seen = json.loads((sidecar.paths.state / "conditional.json").read_text(encoding="utf-8"))
    assert seen["http://test.example/a"]["etag"] == '"v1"'


def test_a_last_modified_is_matched_independently_of_the_etag(tmp_path) -> None:
    """Half a forged conditional is still dropped by half."""
    routes = {"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE, etag='"v1"')}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("http://test.example/a"))
        job = manifest(
            "http://test.example/a",
            job="JOB-webfetch-WF-02",
            etag='"v1"',
            last_modified="Mon, 01 Jan 2035 00:00:00 GMT",
        )
        etag, modified = sidecar._conditional_for(job, "http://test.example/a")

    assert etag == '"v1"', "recorded by the first fetch, so it may be replayed"
    assert modified is None, "never recorded, so it never reaches the wire"


def test_two_jobs_are_settled_in_one_run(tmp_path) -> None:
    routes = {"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE), "/b": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        stats = run_one(
            sidecar,
            manifest("http://test.example/a", job="JOB-a"),
            manifest("http://test.example/b", job="JOB-b"),
        )
    assert stats.claimed == 2 and stats.fetched == 2
    assert sidecar.queue.state_of("JOB-a") == STATE_DONE
    assert sidecar.queue.state_of("JOB-b") == STATE_DONE


# --------------------------------------------------------------------------
# refusals are results
# --------------------------------------------------------------------------


def test_a_bot_wall_is_settled_not_retried(tmp_path) -> None:
    wall = Route(status=403, body=b"<html><title>Just a moment...</title></html>")
    with LocalSite({"/robots.txt": ALLOW_ROBOTS, "/a": wall}) as site:
        sidecar = sidecar_for(tmp_path, site)
        stats = run_one(sidecar, manifest("http://test.example/a"))
        assert site.paths.count("/a") == 1, "no second attempt by another route — ever"

    assert stats.refused == 1
    state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert state == STATE_FAILED
    assert result["outcome"] == "refused" and result["reason"] == "bot_challenge"
    assert result["http_status"] == 403
    assert audit_lines(sidecar.paths)[0]["reason"] == "bot_challenge"


def test_a_robots_disallow_is_recorded_with_its_verdict(tmp_path) -> None:
    robots = Route(body=b"User-agent: *\nDisallow: /\n", content_type="text/plain")
    with LocalSite({"/robots.txt": robots, "/a": Route(body=PAGE)}) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("http://test.example/a"))
        assert "/a" not in site.paths, "the page was never requested"

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["reason"] == "robots_disallow"
    assert result["robots"]["verdict"] == "disallow"


def test_an_unapproved_host_produces_a_proposal_for_a_human(tmp_path) -> None:
    """Agents propose; a human approves on the host machine. Writing the
    proposal is the *only* thing the fetch path may do about it."""
    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("https://unknown.example/page"))

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["reason"] == "host_not_allowed"
    proposal = json.loads(sidecar.queue.proposals_path.read_text(encoding="utf-8").strip())
    assert proposal["host"] == "unknown.example"
    assert proposal["launch_id"] == "LNCH-01M1R3J6TZ2HFQM95AEFEW1GY1"
    assert proposal["example_url"] == "https://unknown.example/page"


def test_an_ssrf_shaped_url_is_refused_before_anything_happens(tmp_path) -> None:
    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("https://169.254.169.254/latest/meta-data/"))
        assert site.paths == []

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["reason"] == "ip_literal"
    assert result["resolved_ips"] == []
    assert result["bytes_out"] == 0


def test_a_manifest_that_does_not_parse_is_settled_as_unattributed(tmp_path) -> None:
    """A job whose manifest could not be read is by definition unattributed,
    and the record says so rather than carrying an id this loop invented."""
    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site)
        queue = sidecar.queue.ensure_layout()
        (queue.pending_dir / "JOB-bad.json").write_text(
            '{"schema": 1, "smuggled": true}', encoding="utf-8"
        )
        stats = sidecar.run(max_jobs=1, max_idle_polls=1)

    assert stats.refused == 1
    state, result = sidecar.queue.read_result("JOB-bad")
    assert state == STATE_FAILED
    assert result["reason"] == "manifest_invalid"
    assert result["launch_id"] == "unknown"
    assert audit_lines(sidecar.paths)[0]["launch_id"] == "unknown"


def test_a_hand_written_poison_manifest_settles_instead_of_looping(tmp_path) -> None:
    """lane a fix pass (CONT-2): one URL used to be a permanent kill switch.

    ``urlsplit`` raises ``ValueError('Invalid IPv6 URL')`` on an unbalanced
    bracket, ``validate_manifest`` accepts any non-empty URL under 8 KiB, and
    the exception escaped every refusal path. The job went back to
    ``pending/``, nothing was written to ``failed/``, no audit line existed —
    and ``restart: unless-stopped`` re-claimed the same manifest forever. One
    line an injected agent could write, and the fetch subsystem was gone with
    nothing to investigate.
    """
    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site)
        queue = sidecar.queue.ensure_layout()
        queue.submit(manifest("https://[::1/", job="JOB-webfetch-WF-POISON"))
        stats = sidecar.run(max_jobs=1, max_idle_polls=1)

    assert stats.refused == 1
    state, result = sidecar.queue.read_result("JOB-webfetch-WF-POISON")
    assert state == STATE_FAILED
    assert result["reason"] == "host_syntax"
    assert [line["reason"] for line in audit_lines(sidecar.paths)] == ["host_syntax"]
    assert sidecar.queue.pending_job_ids() == [], "nothing left to replay on restart"


def test_a_claimed_origin_without_a_list_behind_it_is_treated_as_an_agents(
    tmp_path,
) -> None:
    """lane a fix pass (CONT-1): ``origin`` is written by the untrusted side.

    ``operator_list`` keeps the query string and skips the ``agent_daily``
    cap. A manifest that simply *says* ``operator_list`` — which is all a
    hand-written one has to do — used to get both. The sidecar now derives
    the origin it acts on: no ``list_ref``, no privilege.
    """
    routes = {"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(
            sidecar,
            manifest("http://test.example/a?leak=the-corpus", origin="operator_list"),
        )

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["url_norm"] == "http://test.example/a", "the query was stripped"
    assert result["policy"]["query_stripped"] is True
    counters = json.loads((sidecar.paths.state / "counters.json").read_text(encoding="utf-8"))
    assert counters["agent"] == 1, "and it counted against the agent cap"


def test_a_list_ref_restores_the_operator_origin(tmp_path) -> None:
    """The privilege is not removed, only made to cost something: a manifest
    that names the delivered list it came off keeps its query."""
    routes = {"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(
            sidecar,
            manifest(
                "http://test.example/a?id=42",
                origin="operator_list",
                list_ref="sha256:" + "0" * 64 + "/deliveries/wave_0/links.md",
            ),
        )

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["url_norm"] == "http://test.example/a?id=42"
    counters = json.loads((sidecar.paths.state / "counters.json").read_text(encoding="utf-8"))
    assert counters["agent"] == 0


def test_a_clone_host_that_resolves_into_the_lan_never_reaches_git(tmp_path) -> None:
    """lane a fix pass (CONT-5): the git path had one SSRF layer, not two.

    ``git clone`` does its own DNS, so ``NetGuard``'s resolve-and-validate
    step never ran on this branch — in the sandbox the kernel firewall caught
    it, but DEV mode (``webfetch sidecar --foreground``) has no firewall at
    all. The clone host is now resolved and validated first, and the answers
    are recorded so the audit can say where the bytes came from.
    """
    exploded: list[str] = []

    def never_called(policy, work):  # pragma: no cover - asserted not to run
        exploded.append("git")
        raise AssertionError("git must not be invoked for a private clone host")

    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site, gitfetcher_factory=never_called)
        sidecar.netguard = NetGuard(
            resolver=lambda host, port: [(socket.AF_INET, "10.1.2.3")],
            socket_factory=_no_sockets,
        )
        run_one(sidecar, manifest("https://repos.example/o/r", kind="git"))

    assert exploded == []
    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["reason"] == "ip_private"


@requires_git
def test_a_git_result_records_where_the_bytes_came_from(tmp_path) -> None:
    """``resolved_ips`` was empty on every git result, so the provenance
    record could not say which address answered."""
    repo = build_repo(tmp_path)
    with LocalSite() as site:
        sidecar = sidecar_for(
            tmp_path,
            site,
            gitfetcher_factory=lambda policy, work: GitFetcher(
                policy.caps, work_dir=work, _clone_url_fn=lambda _spec: repo.as_uri()
            ),
        )
        run_one(sidecar, manifest("https://repos.example/owner/project", kind="git"))

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["resolved_ips"] == ["127.0.0.1"]


def test_a_tdm_opt_out_is_recorded_but_not_obeyed_by_default(tmp_path) -> None:
    """Ruling L-A3: recorded on every fetch, enforced only when the operator
    turns the knob on."""
    page = Route(body=PAGE, headers={"X-Robots-Tag": "noai, noimageai"})
    with LocalSite({"/robots.txt": ALLOW_ROBOTS, "/a": page}) as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("http://test.example/a"))

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["outcome"] == "fetched"
    assert "noai" in result["headers_subset"]["x-robots-tag"]


def test_the_tdm_knob_turns_the_signal_into_a_refusal(tmp_path) -> None:
    page = Route(body=PAGE, headers={"X-Robots-Tag": "noai"})
    policy_kwargs = {
        "toml": 'contact_mailto = "ops@example.com"\nmin_host_interval_s = 0\nhonor_tdm_optout = true\n'
    }
    with LocalSite({"/robots.txt": ALLOW_ROBOTS, "/a": page}) as site:
        sidecar = sidecar_for(tmp_path, site, policy_kwargs=policy_kwargs)
        run_one(sidecar, manifest("http://test.example/a"))

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["outcome"] == "refused" and result["reason"] == "tdm_optout"
    assert result["robots"]["verdict"] == "allow", "robots said yes; the TDM knob said no"


def test_a_full_queue_refuses_rather_than_filling_the_disk(tmp_path) -> None:
    policy_kwargs = {
        "toml": 'contact_mailto = "ops@example.com"\nmin_host_interval_s = 0\nqueue_disk_cap = 1\n'
    }
    with LocalSite({"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE)}) as site:
        sidecar = sidecar_for(tmp_path, site, policy_kwargs=policy_kwargs)
        run_one(sidecar, manifest("http://test.example/a"))

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["reason"] == "disk_cap"


# --------------------------------------------------------------------------
# fail-closed and recovery
# --------------------------------------------------------------------------


def test_an_unreadable_policy_stops_the_loop_and_returns_the_job(tmp_path) -> None:
    """The sidecar does not know what it may do. Degrading to "fetch
    anything" is the one response nobody could defend; the job goes back and
    the failure is loud."""
    policy_dir = write_policy(tmp_path, toml="this is not [[ toml\n")
    with LocalSite() as site:
        sidecar = Sidecar(
            paths_for(tmp_path, policy_dir),
            worker_id="worker-1",
            netguard=loopback_netguard(site.port),
            poll_interval_s=0,
        )
        sidecar.queue.ensure_layout().submit(manifest("http://test.example/a"))
        with pytest.raises(PolicyError):
            sidecar.run(max_jobs=1, max_idle_polls=1)

    assert sidecar.queue.state_of("JOB-webfetch-WF-01") == STATE_PENDING


def test_an_unmodelled_crash_is_recorded_before_the_container_dies(tmp_path) -> None:
    """lane a fix pass (CONT-2): a crash still kills the loop, but it settles.

    The job used to go back to ``pending/``. Under ``restart:
    unless-stopped`` that made one crashing manifest a permanent, silent kill
    of the fetch subsystem: claim, crash, restart, claim the same job again,
    forever, with nothing in ``failed/`` and no audit line to investigate.
    Now the attempt ends in a record like every other, and the exception
    still propagates so the container dies visibly.
    """
    with LocalSite({"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE)}) as site:
        sidecar = sidecar_for(tmp_path, site)

        def exploding_factory(policy, work):
            raise RuntimeError("a bug nobody modelled")

        sidecar._gitfetcher_factory = exploding_factory
        sidecar.queue.ensure_layout().submit(
            manifest("https://repos.example/o/r", kind="git")
        )
        with pytest.raises(RuntimeError, match="nobody modelled"):
            sidecar.run(max_jobs=1, max_idle_polls=1)

    assert sidecar.queue.state_of("JOB-webfetch-WF-01") == STATE_FAILED
    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["outcome"] == "refused"
    assert result["reason"] == "manifest_invalid"
    lines = audit_lines(sidecar.paths)
    assert [line["reason"] for line in lines] == ["manifest_invalid"]
    assert "RuntimeError" in lines[0]["detail"], "the audit says what actually broke"
    assert not list(sidecar.queue.claimed_dir.glob("*/*.json"))
    assert not sidecar.queue.claim_lock_path("JOB-webfetch-WF-01").exists()

    # The restart the container will do next finds nothing to claim: the
    # crash loop is bounded at one.
    assert sidecar.queue.claim_next("worker-2") is None


def test_a_keyboard_interrupt_still_gives_the_job_back(tmp_path) -> None:
    """Stopping the loop is not a defect in the job it happened to hold."""
    with LocalSite({"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE)}) as site:
        sidecar = sidecar_for(tmp_path, site)

        def interrupting_factory(policy, work):
            raise KeyboardInterrupt

        sidecar._gitfetcher_factory = interrupting_factory
        sidecar.queue.ensure_layout().submit(
            manifest("https://repos.example/o/r", kind="git")
        )
        with pytest.raises(KeyboardInterrupt):
            sidecar.run(max_jobs=1, max_idle_polls=1)

    assert sidecar.queue.state_of("JOB-webfetch-WF-01") == STATE_PENDING
    assert audit_lines(sidecar.paths) == []


def test_a_denylist_policy_is_refused_by_default(tmp_path) -> None:
    policy_dir = write_policy(tmp_path, toml='mode = "denylist"\n')
    with LocalSite() as site:
        sidecar = Sidecar(
            paths_for(tmp_path, policy_dir),
            netguard=loopback_netguard(site.port),
            poll_interval_s=0,
        )
        sidecar.queue.ensure_layout().submit(manifest("http://test.example/a"))
        with pytest.raises(PolicyError, match="fail-closed"):
            sidecar.run(max_jobs=1, max_idle_polls=1)


def test_a_stale_claim_is_reclaimed_by_the_loop(tmp_path) -> None:
    routes = {"/robots.txt": ALLOW_ROBOTS, "/a": Route(body=PAGE)}
    with LocalSite(routes) as site:
        sidecar = sidecar_for(tmp_path, site, reclaim_after_s=0.0)
        queue: Queue = sidecar.queue.ensure_layout()
        queue.submit(manifest("http://test.example/a"))
        # A worker that died holding the claim.
        queue.claim("JOB-webfetch-WF-01", "worker-dead")
        stats = sidecar.run(max_jobs=1, max_idle_polls=2)

    assert stats.reclaimed >= 1
    assert sidecar.queue.state_of("JOB-webfetch-WF-01") == STATE_DONE


# --------------------------------------------------------------------------
# git jobs
# --------------------------------------------------------------------------


@requires_git
def test_a_git_job_publishes_a_tar(tmp_path) -> None:
    repo = build_repo(tmp_path)
    with LocalSite() as site:
        sidecar = sidecar_for(
            tmp_path,
            site,
            gitfetcher_factory=lambda policy, work: GitFetcher(
                policy.caps, work_dir=work, _clone_url_fn=lambda _spec: repo.as_uri()
            ),
        )
        stats = run_one(
            sidecar, manifest("https://repos.example/owner/project", kind="git")
        )

    assert stats.fetched == 1
    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["content_class"] == "git"
    assert result["robots"] == {"fetched": False, "verdict": "n/a", "crawl_delay_s": 0.0}
    assert len(result["git"]["head"]) == 40
    assert sidecar.queue.payload_path("JOB-webfetch-WF-01", REPO_FILENAME).stat().st_size > 0
    assert sidecar.queue.verify_payload("JOB-webfetch-WF-01", result) is not None
    assert site.paths == [], "a clone never touches the web UI"


@requires_git
def test_the_clone_scratch_is_emptied_after_the_job(tmp_path) -> None:
    repo = build_repo(tmp_path)
    with LocalSite() as site:
        sidecar = sidecar_for(
            tmp_path,
            site,
            gitfetcher_factory=lambda policy, work: GitFetcher(
                policy.caps, work_dir=work, _clone_url_fn=lambda _spec: repo.as_uri()
            ),
        )
        run_one(sidecar, manifest("https://repos.example/owner/project", kind="git"))
    assert not (sidecar.paths.work / "JOB-webfetch-WF-01").exists()


def test_a_host_approved_for_fetching_is_not_approved_for_cloning(tmp_path) -> None:
    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("https://test.example/owner/project", kind="git"))

    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["reason"] == "host_not_allowed"
    lines = sidecar.queue.proposals_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "one refusal is one human decision, so one row"
    proposal = json.loads(lines[0])
    assert proposal["flags"] == ["git"]
    assert proposal["host"] == "test.example"


def test_a_git_url_of_the_wrong_shape_is_refused(tmp_path) -> None:
    with LocalSite() as site:
        sidecar = sidecar_for(tmp_path, site)
        run_one(sidecar, manifest("https://repos.example/owner/project/issues/1", kind="git"))
    _state, result = sidecar.queue.read_result("JOB-webfetch-WF-01")
    assert result["reason"] == "git_url_shape"


# --------------------------------------------------------------------------
# the CLI verb
# --------------------------------------------------------------------------


def test_the_group_is_auto_discovered() -> None:
    from trialerror.cli import discover_groups

    assert "webfetch" in {getattr(mod, "GROUP_NAME", None) for mod in discover_groups()}


def test_only_the_sidecar_verb_exists_so_far() -> None:
    """Build order: the enqueue verbs land with the research-side handlers.
    A group advertising ``add`` before that exists would be a promise the
    harness cannot keep."""
    import argparse

    from trialerror.cli import webfetch as group

    parser = argparse.ArgumentParser()
    group.register(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(["webfetch", "add", "--url", "https://example.com"])


def test_the_cli_runs_one_pass_and_reports_ids_only(tmp_path, capsys) -> None:
    policy_dir = write_policy(tmp_path)
    queue_dir = tmp_path / "queue"
    Queue(queue_dir).ensure_layout()

    rc = main(
        [
            "webfetch",
            "sidecar",
            "--foreground",
            "--once",
            "--queue",
            str(queue_dir),
            "--policy",
            str(policy_dir),
            "--audit",
            str(tmp_path / "audit"),
            "--state",
            str(tmp_path / "state"),
            "--work",
            str(tmp_path / "work"),
        ]
    )
    envelope = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert envelope["ok"] is True
    assert envelope["command"] == "webfetch sidecar"
    assert envelope["result"]["claimed"] == 0
    assert envelope["result"]["sidecarVersion"] == "webfetch-sidecar/1"
    assert (queue_dir / "sidecar.heartbeat").is_file()


def test_the_cli_refuses_a_missing_policy_directory(tmp_path, capsys) -> None:
    rc = main(
        [
            "webfetch",
            "sidecar",
            "--queue",
            str(tmp_path / "queue"),
            "--policy",
            str(tmp_path / "nope"),
        ]
    )
    envelope = json.loads(capsys.readouterr().out.strip())
    assert rc == 1
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "policy_missing"


def test_the_cli_reports_an_invalid_policy_as_an_error_not_a_crash(tmp_path, capsys) -> None:
    """The policy is read per job, so this needs a job to read it for. A
    wildcard line is refused at load, and the CLI has to surface that as a
    structured error rather than a traceback."""
    policy_dir = write_policy(tmp_path, hosts="*.example.com\n")
    queue_dir = tmp_path / "queue"
    Queue(queue_dir).ensure_layout().submit(manifest("https://test.example/a"))

    rc = main(
        [
            "webfetch",
            "sidecar",
            "--once",
            "--queue",
            str(queue_dir),
            "--policy",
            str(policy_dir),
            "--audit",
            str(tmp_path / "audit"),
            "--state",
            str(tmp_path / "state"),
            "--work",
            str(tmp_path / "work"),
        ]
    )
    envelope = json.loads(capsys.readouterr().out.strip())
    assert rc == 1
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "policy_invalid"
    assert "wildcard" in envelope["error"]["message"]
    assert Queue(queue_dir).state_of("JOB-webfetch-WF-01") == STATE_PENDING


def test_the_cli_has_no_flag_that_could_name_a_host_or_a_header() -> None:
    """Everything the verb may do comes from the policy directory, which on
    the real deployment is mounted read-only from the host and is not
    visible in the research container at all."""
    import argparse

    from trialerror.cli import webfetch as group

    parser = argparse.ArgumentParser()
    group.register(parser.add_subparsers())
    args = parser.parse_args(["webfetch", "sidecar"])
    assert not any(
        name in vars(args) for name in ("url", "host", "header", "method", "timeout")
    )
    for flag in ("--url", "--host", "--header", "--method"):
        with pytest.raises(SystemExit):
            parser.parse_args(["webfetch", "sidecar", flag, "x"])


def test_the_denylist_escape_hatch_is_off_unless_asked_for(tmp_path) -> None:
    import argparse

    from trialerror.cli import webfetch as group

    parser = argparse.ArgumentParser()
    group.register(parser.add_subparsers())
    assert parser.parse_args(["webfetch", "sidecar"]).allow_denylist_mode is False


def test_load_policy_helper_matches_the_sidecars_own_loader(tmp_path) -> None:
    """A guard on the fixture itself: if the two ever diverge, every test in
    this file is testing something the sidecar does not do."""
    policy = load_policy(tmp_path)
    assert policy.rule_for("test.example").allow_http is True
    assert policy.contact_mailto == "ops@example.com"
